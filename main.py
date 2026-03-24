from __future__ import annotations

import os
import json
import hashlib
import time
import threading
import random
import math
from datetime import datetime, timezone
from typing import Optional

from flask import Flask, render_template, request, jsonify, session
from sqlmodel import Field, Session, SQLModel, create_engine, select

# ── 앱 설정 ──────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "velox-dev-secret-key-change-in-prod")

# ── DB 설정 (Supabase PostgreSQL) ─────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("❌ DATABASE_URL 환경변수가 설정되지 않았습니다!")

# Supabase/Render 에서 postgres:// 로 오는 경우 postgresql:// 로 변환
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

print(f"[VELOX] DB 연결 중...")
engine = create_engine(DATABASE_URL, echo=False)

# ── 관리자 설정 ───────────────────────────────────────
ADMIN_USERNAME = "alwaystwosteps"
ADMIN_BTC_QTY = int(40_000_000 * 0.001)  # 바이트코인 0.1% = 40,000개
ADMIN_BTC_PRICE = 50_000_000  # 초기 원가 기준

# ══════════════════════════════════════════════════════
#  서버 사이드 가격 엔진 (모든 유저 공유)
# ══════════════════════════════════════════════════════

INITIAL_PRICES = {
    'os': 250000,
    'mr': 200000,
    'nv': 500000,
    'bn': 300000,
    'btc': 50_000_000,
    'bth': 7_000_000,
    'shi': 2_000,
    'dge': 20_000,
    'jio': 100000,
}

ASSET_SHARES = {
    'os': 2_680_000_000,
    'mr': 425_000_000,
    'nv': 13_400_000_000,
    'bn': 19_166_666_667,
    'btc': 40_000_000,
    'bth': 95_714_286,
    'shi': 11_250_000_000,
    'dge': 1_650_000_000,
    'jio': 999_999_999,
}

ASSET_TYPES = {
    'os': 'stock', 'mr': 'stock', 'nv': 'stock', 'bn': 'stock',
    'btc': 'coin', 'bth': 'coin', 'shi': 'coin', 'dge': 'coin',
    'jio': 'index',
}

ASSET_NAMES = {
    'os': '오성전자', 'mr': '미래자동차', 'nv': 'AND비디아', 'bn': 'bAnana',
    'btc': '바이트코인', 'bth': 'Both코인', 'shi': '시발이누', 'dge': '닷지코인', 'jio': 'JIODAQ',
}

SMALL_COINS = {'shi', 'dge', 'bth'}

STOCK_IDS = ['os', 'mr', 'nv', 'bn', 'jio']
COIN_IDS = ['btc', 'bth', 'shi', 'dge']

# 전역 가격 상태 (모든 유저 공유)
_price_lock = threading.Lock()
prices: dict[str, int] = dict(INITIAL_PRICES)
price_history: dict[str, list] = {k: [v] for k, v in INITIAL_PRICES.items()}
trade_impact: dict[str, float] = {k: 0.0 for k in INITIAL_PRICES}
_last_stock_tick = 0.0
_last_coin_tick = 0.0


def _tick_stocks():
    global _last_stock_tick
    with _price_lock:
        for aid in STOCK_IDS:
            pct = (random.random() - 0.49) * 0.025
            if random.random() < 0.01:
                if random.random() < 0.5:
                    pct += 0.10 + random.random() * 0.30
                else:
                    pct -= 0.10 + random.random() * 0.30
            if trade_impact.get(aid):
                pct += trade_impact[aid]
                trade_impact[aid] *= 0.4
                if abs(trade_impact[aid]) < 0.0001:
                    trade_impact[aid] = 0.0
            pct = max(-0.40, pct)
            prices[aid] = max(1, round(prices[aid] * (1 + pct)))
            price_history[aid].append(prices[aid])
            if len(price_history[aid]) > 60:
                price_history[aid].pop(0)
        _last_stock_tick = time.time()


def _tick_coins():
    global _last_coin_tick
    with _price_lock:
        for aid in COIN_IDS:
            pct = (random.random() - 0.49) * 0.06
            if random.random() < 0.02:
                is_small = aid in SMALL_COINS
                if random.random() < 0.5:
                    pct += (0.20 + random.random() * 0.60) if is_small else (0.10 + random.random() * 0.30)
                else:
                    pct -= (0.20 + random.random() * 0.40) if is_small else (0.10 + random.random() * 0.25)
            if trade_impact.get(aid):
                pct += trade_impact[aid]
                trade_impact[aid] *= 0.25
                if abs(trade_impact[aid]) < 0.0001:
                    trade_impact[aid] = 0.0
            is_small = aid in SMALL_COINS
            pct = max(-0.60 if is_small else -0.35, pct)
            prices[aid] = max(1, round(prices[aid] * (1 + pct)))
            price_history[aid].append(prices[aid])
            if len(price_history[aid]) > 60:
                price_history[aid].pop(0)
        _last_coin_tick = time.time()


def _price_engine_loop():
    """백그라운드 쓰레드: 주식 10초, 코인 5초마다 가격 갱신"""
    global _last_stock_tick, _last_coin_tick
    _last_stock_tick = time.time()
    _last_coin_tick = time.time()
    while True:
        now = time.time()
        if now - _last_coin_tick >= 5:
            _tick_coins()
        if now - _last_stock_tick >= 10:
            _tick_stocks()
        time.sleep(1)


_engine_thread = threading.Thread(target=_price_engine_loop, daemon=True)
print("[VELOX] 가격 엔진 시작")


# ── 모델 ──────────────────────────────────────────────
class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    is_admin: bool = Field(default=False)

    balance: float = Field(default=100_000.0)
    tier_idx: int = Field(default=0)
    portfolio_json: str = Field(default="{}")
    cost_basis_json: str = Field(default="{}")
    loans_json: str = Field(default="[]")
    history_json: str = Field(default="[]")
    transfers_json: str = Field(default="[]")
    today_transferred: float = Field(default=0.0)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ── 공지 저장 (메모리) ────────────────────────────────
announcements: list = []

# ── DB 초기화 ─────────────────────────────────────────
SQLModel.metadata.create_all(engine)
print("[VELOX] DB 준비 완료")


# ── 헬퍼 ──────────────────────────────────────────────
def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def user_to_dict(u: User) -> dict:
    return {
        "name": u.username,
        "isAdmin": u.is_admin,
        "balance": u.balance,
        "tierIdx": u.tier_idx,
        "portfolio": json.loads(u.portfolio_json),
        "costBasis": json.loads(u.cost_basis_json),
        "loans": json.loads(u.loans_json),
        "history": json.loads(u.history_json),
        "transfers": json.loads(u.transfers_json),
        "todayTransferred": u.today_transferred,
    }


def get_current_user() -> Optional[User]:
    uid = session.get("user_id")
    if not uid:
        return None
    with Session(engine) as db:
        return db.get(User, uid)


def require_admin(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user or not user.is_admin:
            return jsonify({"ok": False, "error": "관리자 권한이 필요합니다."}), 403
        return f(*args, **kwargs)

    return decorated


# ══════════════════════════════════════════════════════
#  Routes
# ══════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("main.html")


@app.route("/api/me")
def api_me():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"loggedIn": False})
    with Session(engine) as db:
        u = db.get(User, uid)
        if not u:
            return jsonify({"loggedIn": False})
        user_data = user_to_dict(u)
    return jsonify({"loggedIn": True, "user": user_data})


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if not username or not password:
        return jsonify({"ok": False, "error": "아이디와 비밀번호를 입력하세요."})
    with Session(engine) as db:
        user = db.exec(select(User).where(User.username == username)).first()
        if not user or user.password_hash != hash_pw(password):
            return jsonify({"ok": False, "error": "아이디 또는 비밀번호가 올바르지 않습니다."})
        session["user_id"] = user.id
        user_data = user_to_dict(user)
    return jsonify({"ok": True, "user": user_data})


@app.route("/api/signup", methods=["POST"])
def api_signup():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"ok": False, "error": "아이디와 비밀번호를 입력하세요."})
    if not (4 <= len(username) <= 20):
        return jsonify({"ok": False, "error": "아이디는 4~20자입니다."})
    if len(password) < 8:
        return jsonify({"ok": False, "error": "비밀번호는 8자 이상이어야 합니다."})

    with Session(engine) as db:
        if db.exec(select(User).where(User.username == username)).first():
            return jsonify({"ok": False, "error": "이미 사용 중인 아이디입니다."})

        is_admin = (username == ADMIN_USERNAME)
        portfolio = {}
        cost_basis = {}

        if is_admin:
            portfolio["btc"] = ADMIN_BTC_QTY
            cost_basis["btc"] = ADMIN_BTC_QTY * ADMIN_BTC_PRICE

        user = User(
            username=username,
            password_hash=hash_pw(password),
            is_admin=is_admin,
            balance=100_000.0,
            tier_idx=0,
            portfolio_json=json.dumps(portfolio),
            cost_basis_json=json.dumps(cost_basis),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        session["user_id"] = user.id
        user_data = user_to_dict(user)

    print(f"[가입완료] {username} / admin={is_admin}")
    return jsonify({"ok": True, "user": user_data})


@app.route("/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/save", methods=["POST"])
def api_save():
    user = get_current_user()
    if not user:
        return jsonify({"ok": False}), 401
    data = request.get_json(silent=True) or {}
    with Session(engine) as db:
        u = db.get(User, user.id)
        if not u:
            return jsonify({"ok": False}), 404
        if "balance" in data: u.balance = float(data["balance"])
        if "tierIdx" in data: u.tier_idx = int(data["tierIdx"])
        if "portfolio" in data: u.portfolio_json = json.dumps(data["portfolio"])
        if "costBasis" in data: u.cost_basis_json = json.dumps(data["costBasis"])
        if "loans" in data: u.loans_json = json.dumps(data["loans"])
        if "history" in data: u.history_json = json.dumps(data["history"][-100:])
        if "transfers" in data: u.transfers_json = json.dumps(data["transfers"][-50:])
        if "todayTransferred" in data: u.today_transferred = float(data["todayTransferred"])
        u.updated_at = datetime.now(timezone.utc)
        db.add(u)
        db.commit()
    return jsonify({"ok": True})


# ── 가격 동기화 API ────────────────────────────────────

@app.route("/api/prices")
def api_get_prices():
    global _last_stock_tick, _last_coin_tick

    now = time.time()

    if now - _last_coin_tick >= 5:
        _tick_coins()

    if now - _last_stock_tick >= 10:
        _tick_stocks()

    with _price_lock:
        snap_prices = dict(prices)
        snap_history = {k: list(v) for k, v in price_history.items()}

    return jsonify({
        "ok": True,
        "prices": snap_prices,
        "history": snap_history,
        "ts": int(time.time() * 1000),
    })


# ── 거래 API (서버 가격 기준) ─────────────────────────

@app.route("/api/trade", methods=["POST"])
def api_trade():
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "로그인 필요"}), 401

    data = request.get_json(silent=True) or {}
    aid = data.get("assetId", "")
    action = data.get("action", "")
    qty = int(data.get("qty", 0))

    if aid not in prices:
        return jsonify({"ok": False, "error": "알 수 없는 종목"})
    if action not in ("buy", "sell"):
        return jsonify({"ok": False, "error": "action 오류"})
    if qty <= 0:
        return jsonify({"ok": False, "error": "수량 오류"})

    with Session(engine) as db:
        u = db.get(User, user.id)
        if not u:
            return jsonify({"ok": False, "error": "유저 없음"}), 404

        portfolio = json.loads(u.portfolio_json or "{}")
        cost_basis = json.loads(u.cost_basis_json or "{}")

        with _price_lock:
            current_price = prices[aid]
            shares = ASSET_SHARES.get(aid, 1_000_000_000)
            mkt_cap = current_price * shares
            trade_value = qty * current_price
            raw_ratio = trade_value / mkt_cap if mkt_cap > 0 else 0

            asset_type = ASSET_TYPES.get(aid, "stock")
            if asset_type == "coin":
                liq = 8 if shares < 1_000_000_000 else 3
            else:
                liq = 5 if shares < 500_000_000 else 2

            impact_pct = math.sqrt(raw_ratio) * liq * (1 if action == "buy" else -1)
            is_small = aid in SMALL_COINS
            max_drop = -0.60 if is_small else (-0.35 if asset_type == "coin" else -0.40)
            clamped = max(max_drop, min(0.15, impact_pct))
            old_price = prices[aid]
            prices[aid] = max(1, round(old_price * (1 + clamped)))
            trade_impact[aid] = trade_impact.get(aid, 0.0) + impact_pct * 0.4
            price_history[aid].append(prices[aid])
            if len(price_history[aid]) > 60:
                price_history[aid].pop(0)
            exec_price = old_price

        total = qty * exec_price

        if action == "buy":
            if u.balance < total:
                with _price_lock:
                    prices[aid] = old_price
                    trade_impact[aid] -= impact_pct * 0.4
                return jsonify({"ok": False, "error": "잔액이 부족합니다"})
            u.balance -= total
            portfolio[aid] = (portfolio.get(aid) or 0) + qty
            cost_basis[aid] = (cost_basis.get(aid) or 0) + total
        else:
            holding = portfolio.get(aid, 0)
            if holding < qty:
                with _price_lock:
                    prices[aid] = old_price
                    trade_impact[aid] -= impact_pct * 0.4
                return jsonify({"ok": False, "error": "보유량이 부족합니다"})
            u.balance += total
            sell_ratio = qty / holding
            cost_basis[aid] = (cost_basis.get(aid) or 0) * (1 - sell_ratio)
            portfolio[aid] = holding - qty
            if portfolio[aid] <= 0:
                portfolio[aid] = 0
                cost_basis[aid] = 0

        history = json.loads(u.history_json or "[]")
        history.append({"name": ASSET_NAMES.get(aid, aid), "type": action, "amount": total})
        history = history[-100:]

        u.portfolio_json = json.dumps(portfolio)
        u.cost_basis_json = json.dumps(cost_basis)
        u.history_json = json.dumps(history)
        u.updated_at = datetime.now(timezone.utc)
        db.add(u)
        db.commit()
        final_balance = u.balance

    return jsonify({
        "ok": True,
        "balance": final_balance,
        "portfolio": portfolio,
        "costBasis": cost_basis,
        "history": history,
        "newPrice": prices[aid],
        "execPrice": exec_price,
        "impactPct": round(clamped * 100, 2),
    })


# ── 송금 API (서버 검증) ───────────────────────────────

@app.route("/api/transfer", methods=["POST"])
def api_transfer():
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "로그인 필요"}), 401

    data = request.get_json(silent=True) or {}
    to_id = data.get("to", "").strip()
    amount = float(data.get("amount", 0))
    memo = data.get("memo", "")

    if not to_id:
        return jsonify({"ok": False, "error": "받는 사람 ID를 입력하세요"})
    if amount <= 0:
        return jsonify({"ok": False, "error": "금액을 입력하세요"})

    TIER_TRANSFER_LIMITS = {
        0: 10_000_000, 1: 50_000_000, 2: 100_000_000,
        3: 1_000_000_000, 4: float('inf'),
    }

    with Session(engine) as db:
        sender = db.get(User, user.id)
        if not sender:
            return jsonify({"ok": False, "error": "유저 없음"}), 404

        limit = TIER_TRANSFER_LIMITS.get(sender.tier_idx, 10_000_000)
        if limit != float('inf') and sender.today_transferred + amount > limit:
            return jsonify({"ok": False, "error": "일일 한도 초과"})

        if sender.balance < amount:
            return jsonify({"ok": False, "error": "잔액이 부족합니다"})

        recipient = db.exec(select(User).where(User.username == to_id)).first()
        if not recipient:
            return jsonify({"ok": False, "error": "존재하지 않는 유저입니다"})
        if recipient.id == sender.id:
            return jsonify({"ok": False, "error": "자신에게 송금할 수 없습니다"})

        sender.balance -= amount
        sender.today_transferred += amount
        recipient.balance += amount

        now_str = datetime.now(timezone.utc).strftime("%H:%M")
        s_transfers = json.loads(sender.transfers_json or "[]")
        s_transfers.insert(0, {"to": to_id, "amount": amount, "time": now_str, "memo": memo})
        sender.transfers_json = json.dumps(s_transfers[:50])

        sender.updated_at = datetime.now(timezone.utc)
        recipient.updated_at = datetime.now(timezone.utc)
        db.add(sender)
        db.add(recipient)
        db.commit()
        final_balance = sender.balance
        final_transferred = sender.today_transferred

    return jsonify({
        "ok": True,
        "balance": final_balance,
        "todayTransferred": final_transferred,
        "transfers": s_transfers,
    })


# ── 관리자 API ─────────────────────────────────────────

@app.route("/api/admin/users")
@require_admin
def admin_get_users():
    with Session(engine) as db:
        users = db.exec(select(User)).all()
        user_list = [
            {"id": u.id, "username": u.username, "isAdmin": u.is_admin,
             "balance": u.balance, "tierIdx": u.tier_idx,
             "createdAt": u.created_at.strftime("%Y-%m-%d %H:%M")}
            for u in users
        ]
    return jsonify({"ok": True, "users": user_list})


@app.route("/api/admin/user/<int:uid>/balance", methods=["POST"])
@require_admin
def admin_set_balance(uid: int):
    data = request.get_json(silent=True) or {}
    amount = data.get("amount")
    mode = data.get("mode", "set")
    if amount is None:
        return jsonify({"ok": False, "error": "amount 필요"})
    with Session(engine) as db:
        u = db.get(User, uid)
        if not u: return jsonify({"ok": False, "error": "유저 없음"}), 404
        if mode == "set":
            u.balance = float(amount)
        elif mode == "add":
            u.balance += float(amount)
        elif mode == "subtract":
            u.balance = max(0, u.balance - float(amount))
        db.add(u);
        db.commit()
        final_balance = u.balance
    return jsonify({"ok": True, "balance": final_balance})


@app.route("/api/admin/user/<int:uid>/tier", methods=["POST"])
@require_admin
def admin_set_tier(uid: int):
    data = request.get_json(silent=True) or {}
    tier = data.get("tier")
    if tier is None or not (0 <= int(tier) <= 4):
        return jsonify({"ok": False, "error": "tier 0~4 필요"})
    with Session(engine) as db:
        u = db.get(User, uid)
        if not u: return jsonify({"ok": False, "error": "유저 없음"}), 404
        u.tier_idx = int(tier);
        db.add(u);
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/user/<int:uid>/ban", methods=["POST"])
@require_admin
def admin_ban_user(uid: int):
    with Session(engine) as db:
        u = db.get(User, uid)
        if not u or u.is_admin: return jsonify({"ok": False, "error": "불가"}), 400
        u.balance = 0;
        u.tier_idx = 0;
        u.loans_json = "[]"
        db.add(u);
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/user/<int:uid>/reset", methods=["POST"])
@require_admin
def admin_reset_user(uid: int):
    with Session(engine) as db:
        u = db.get(User, uid)
        if not u or u.is_admin: return jsonify({"ok": False, "error": "불가"}), 400
        u.balance = 100_000.0;
        u.tier_idx = 0
        u.portfolio_json = "{}";
        u.cost_basis_json = "{}"
        u.loans_json = "[]";
        u.history_json = "[]"
        u.transfers_json = "[]";
        u.today_transferred = 0.0
        db.add(u);
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/announce", methods=["POST"])
@require_admin
def admin_announce():
    data = request.get_json(silent=True) or {}
    msg = data.get("message", "").strip()
    if not msg: return jsonify({"ok": False, "error": "메시지 필요"})
    announcements.append({"message": msg, "time": datetime.now(timezone.utc).strftime("%H:%M")})
    if len(announcements) > 20: announcements.pop(0)
    return jsonify({"ok": True})


@app.route("/api/announcements")
def get_announcements():
    return jsonify({"ok": True, "list": announcements})


@app.route("/api/admin/grant-btc", methods=["POST"])
@require_admin
def admin_grant_btc():
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "로그인 필요"}), 401

    with Session(engine) as db:
        u = db.get(User, user.id)
        if not u:
            return jsonify({"ok": False, "error": "유저 없음"}), 404

        try:
            portfolio = json.loads(u.portfolio_json or "{}")
            cost_basis = json.loads(u.cost_basis_json or "{}")
        except Exception:
            portfolio = {};
            cost_basis = {}

        if "btc" in portfolio:
            return jsonify({"ok": False, "error": "이미 지급됨"})

        portfolio["btc"] = ADMIN_BTC_QTY
        cost_basis["btc"] = ADMIN_BTC_QTY * ADMIN_BTC_PRICE
        u.portfolio_json = json.dumps(portfolio)
        u.cost_basis_json = json.dumps(cost_basis)
        db.add(u)
        db.commit()

    return jsonify({"ok": True, "qty": ADMIN_BTC_QTY})


# ── 실행 ───────────────────────────────────────────────
_engine_thread.start()

if __name__ == "__main__":
    print(f"👑 관리자: {ADMIN_USERNAME}")
    print("🚀 http://localhost:5000\n")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
