from __future__ import annotations

import os
import json
import hashlib
from datetime import datetime, timezone
from typing import Optional

from flask import Flask, render_template, request, jsonify, session
from sqlmodel import Field, Session, SQLModel, create_engine, select
import sqlite3 as _sqlite3

# ── 앱 설정 ──────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "velox-dev-secret-key-change-in-prod")

# ── DB 경로 설정 ──────────────────────────────────────
# 로컬: main.py 옆에 database.db 생성
# Render: /data/database.db (Disk 마운트 필요)
DB_PATH = os.environ.get("DB_PATH", "database.db")
_db_dir = os.path.dirname(os.path.abspath(DB_PATH)) if os.path.dirname(DB_PATH) else None
if _db_dir and not os.path.exists(_db_dir):
    os.makedirs(_db_dir, exist_ok=True)
print(f"[VELOX] DB 경로: {DB_PATH}")

engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)

# ── 관리자 설정 ───────────────────────────────────────
ADMIN_USERNAME    = "alwaystwosteps"
ADMIN_BTC_QTY   = int(40_000_000 * 0.001)   # 바이트코인 0.1% = 40,000개
ADMIN_BTC_PRICE = 50_000_000                  # 초기 원가 기준


# ── 모델 ──────────────────────────────────────────────
class User(SQLModel, table=True):
    id: Optional[int]        = Field(default=None, primary_key=True)
    username: str             = Field(unique=True, index=True)
    password_hash: str
    is_admin: bool            = Field(default=False)

    balance: float            = Field(default=100_000.0)
    tier_idx: int             = Field(default=0)
    portfolio_json: str       = Field(default="{}")
    cost_basis_json: str      = Field(default="{}")
    loans_json: str           = Field(default="[]")
    history_json: str         = Field(default="[]")
    transfers_json: str       = Field(default="[]")
    today_transferred: float  = Field(default=0.0)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ── 공지 저장 (메모리) ────────────────────────────────
announcements: list = []


# ── DB 초기화 + 마이그레이션 ─────────────────────────
def _migrate():
    """기존 DB에 새 컬럼 자동 추가"""
    try:
        con = _sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("PRAGMA table_info(user)")
        existing = [row[1] for row in cur.fetchall()]
        new_cols = [
            ("is_admin",        "INTEGER NOT NULL DEFAULT 0"),
            ("cost_basis_json", "TEXT    NOT NULL DEFAULT '{}'"),
        ]
        for col, typedef in new_cols:
            if col not in existing:
                cur.execute(f"ALTER TABLE user ADD COLUMN {col} {typedef}")
                print(f"[VELOX] 컬럼 추가: {col}")
        con.commit()
        con.close()
    except Exception as e:
        print(f"[VELOX] 마이그레이션 오류: {e}")

SQLModel.metadata.create_all(engine)
_migrate()
print("[VELOX] DB 준비 완료")


# ── 헬퍼 ──────────────────────────────────────────────
def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def user_to_dict(u: User) -> dict:
    return {
        "name":             u.username,
        "isAdmin":          u.is_admin,
        "balance":          u.balance,
        "tierIdx":          u.tier_idx,
        "portfolio":        json.loads(u.portfolio_json),
        "costBasis":        json.loads(u.cost_basis_json),
        "loans":            json.loads(u.loans_json),
        "history":          json.loads(u.history_json),
        "transfers":        json.loads(u.transfers_json),
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
    user = get_current_user()
    if not user:
        return jsonify({"loggedIn": False})
    return jsonify({"loggedIn": True, "user": user_to_dict(user)})


@app.route("/api/login", methods=["POST"])
def api_login():
    data     = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if not username or not password:
        return jsonify({"ok": False, "error": "아이디와 비밀번호를 입력하세요."})
    with Session(engine) as db:
        user = db.exec(select(User).where(User.username == username)).first()
    if not user or user.password_hash != hash_pw(password):
        return jsonify({"ok": False, "error": "아이디 또는 비밀번호가 올바르지 않습니다."})
    session["user_id"] = user.id
    return jsonify({"ok": True, "user": user_to_dict(user)})


@app.route("/api/signup", methods=["POST"])
def api_signup():
    data     = request.get_json(silent=True) or {}
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

        # 🔥 관리자 판별
        is_admin = (username == ADMIN_USERNAME)

        portfolio  = {}
        cost_basis = {}

        # 🔥 관리자면 BTC 0.1% 지급
        if is_admin:
            portfolio["btc"]  = ADMIN_BTC_QTY
            cost_basis["btc"] = ADMIN_BTC_QTY * ADMIN_BTC_PRICE

        user = User(
            username        = username,
            password_hash   = hash_pw(password),
            is_admin        = is_admin,
            balance         = 100_000.0,
            tier_idx        = 0,
            portfolio_json  = json.dumps(portfolio),
            cost_basis_json = json.dumps(cost_basis),
        )

        db.add(user)
        db.commit()
        db.refresh(user)
        session["user_id"] = user.id

    print(f"[가입완료] {username} / admin={is_admin}")
    return jsonify({"ok": True, "user": user_to_dict(user)})

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
        if "balance"          in data: u.balance          = float(data["balance"])
        if "tierIdx"          in data: u.tier_idx         = int(data["tierIdx"])
        if "portfolio"        in data: u.portfolio_json    = json.dumps(data["portfolio"])
        if "costBasis"        in data: u.cost_basis_json   = json.dumps(data["costBasis"])
        if "loans"            in data: u.loans_json        = json.dumps(data["loans"])
        if "history"          in data: u.history_json      = json.dumps(data["history"][-100:])
        if "transfers"        in data: u.transfers_json    = json.dumps(data["transfers"][-50:])
        if "todayTransferred" in data: u.today_transferred = float(data["todayTransferred"])
        u.updated_at = datetime.now(timezone.utc)
        db.add(u)
        db.commit()
    return jsonify({"ok": True})


# ── 관리자 API ─────────────────────────────────────────

@app.route("/api/admin/users")
@require_admin
def admin_get_users():
    with Session(engine) as db:
        users = db.exec(select(User)).all()
    return jsonify({"ok": True, "users": [
        {"id": u.id, "username": u.username, "isAdmin": u.is_admin,
         "balance": u.balance, "tierIdx": u.tier_idx,
         "createdAt": u.created_at.strftime("%Y-%m-%d %H:%M")}
        for u in users
    ]})


@app.route("/api/admin/user/<int:uid>/balance", methods=["POST"])
@require_admin
def admin_set_balance(uid: int):
    data   = request.get_json(silent=True) or {}
    amount = data.get("amount")
    mode   = data.get("mode", "set")
    if amount is None:
        return jsonify({"ok": False, "error": "amount 필요"})
    with Session(engine) as db:
        u = db.get(User, uid)
        if not u: return jsonify({"ok": False, "error": "유저 없음"}), 404
        if mode == "set":        u.balance  = float(amount)
        elif mode == "add":      u.balance += float(amount)
        elif mode == "subtract": u.balance  = max(0, u.balance - float(amount))
        db.add(u); db.commit()
    return jsonify({"ok": True, "balance": u.balance})


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
        u.tier_idx = int(tier); db.add(u); db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/user/<int:uid>/ban", methods=["POST"])
@require_admin
def admin_ban_user(uid: int):
    with Session(engine) as db:
        u = db.get(User, uid)
        if not u or u.is_admin: return jsonify({"ok": False, "error": "불가"}), 400
        u.balance = 0; u.tier_idx = 0; u.loans_json = "[]"
        db.add(u); db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/user/<int:uid>/reset", methods=["POST"])
@require_admin
def admin_reset_user(uid: int):
    with Session(engine) as db:
        u = db.get(User, uid)
        if not u or u.is_admin: return jsonify({"ok": False, "error": "불가"}), 400
        u.balance = 100_000.0; u.tier_idx = 0
        u.portfolio_json = "{}"; u.cost_basis_json = "{}"
        u.loans_json = "[]"; u.history_json = "[]"
        u.transfers_json = "[]"; u.today_transferred = 0.0
        db.add(u); db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/announce", methods=["POST"])
@require_admin
def admin_announce():
    data = request.get_json(silent=True) or {}
    msg  = data.get("message", "").strip()
    if not msg: return jsonify({"ok": False, "error": "메시지 필요"})
    announcements.append({"message": msg, "time": datetime.now(timezone.utc).strftime("%H:%M")})
    if len(announcements) > 20: announcements.pop(0)
    return jsonify({"ok": True})


@app.route("/api/announcements")
def get_announcements():
    return jsonify({"ok": True, "list": announcements})


# ── 관리자 지분 지급 API (관리자 전용) ─────────────────
@app.route("/api/admin/grant-btc", methods=["POST"])
@require_admin
def admin_grant_btc():
    user = get_current_user()

    # ✅ 1. None 체크
    if not user:
        return jsonify({"ok": False, "error": "로그인 필요"}), 401

    with Session(engine) as db:
        u = db.get(User, user.id)

        # ✅ 2. DB 유저 체크
        if not u:
            return jsonify({"ok": False, "error": "유저 없음"}), 404

        # ✅ 3. JSON 안전 처리
        try:
            portfolio = json.loads(u.portfolio_json or "{}")
            cost_basis = json.loads(u.cost_basis_json or "{}")
        except Exception:
            portfolio = {}
            cost_basis = {}

        # ✅ 4. 중복 지급 방지
        if "btc" in portfolio:
            return jsonify({"ok": False, "error": "이미 지급됨"})

        # ✅ 5. 지급
        portfolio["btc"] = ADMIN_BTC_QTY
        cost_basis["btc"] = ADMIN_BTC_QTY * ADMIN_BTC_PRICE

        u.portfolio_json = json.dumps(portfolio)
        u.cost_basis_json = json.dumps(cost_basis)

        db.add(u)
        db.commit()

    return jsonify({"ok": True, "qty": ADMIN_BTC_QTY})


# ── 실행 ───────────────────────────────────────────────
if __name__ == "__main__":
    print(f"👑 관리자: {ADMIN_USERNAME}")
    print("🚀 http://localhost:5000\n")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
