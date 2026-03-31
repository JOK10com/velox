from __future__ import annotations

import os
import re
import json
import hashlib
import time
import threading
import random
import math
from datetime import datetime, timezone
from typing import Optional

from flask import Flask, render_template, request, jsonify, session, send_from_directory, redirect, url_for, make_response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from authlib.integrations.flask_client import OAuth
from sqlmodel import Field, Session, SQLModel, create_engine, select
from sqlalchemy import text

# ── 앱 설정 ──────────────────────────────────────────
app = Flask(__name__, template_folder='.')

# Render/Nginx 리버스 프록시 뒤에서 https:// URL을 올바르게 생성하기 위해 필수
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

_secret_key = os.environ.get("SECRET_KEY")
if not _secret_key:
    raise RuntimeError("❌ SECRET_KEY 환경변수가 설정되지 않았습니다!")
app.secret_key = _secret_key

# HTTPS 환경에서 세션 쿠키가 정상 전달되도록 설정
app.config["SESSION_COOKIE_SECURE"]   = True   # HTTPS 전용 쿠키
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # OAuth 리디렉트 허용
app.config["SESSION_COOKIE_HTTPONLY"] = True    # JS 접근 차단

# ── Google OAuth 설정 ────────────────────────────────
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
ADMIN_GOOGLE_EMAIL   = os.environ.get("ADMIN_GOOGLE_EMAIL", "")  # 관리자 구글 이메일

if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
    raise RuntimeError("❌ GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET 환경변수가 설정되지 않았습니다!")

oauth = OAuth(app)
google_oauth = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
)

# ── DB 설정 (Supabase PostgreSQL) ─────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("❌ DATABASE_URL 환경변수가 설정되지 않았습니다!")

# Supabase/Render 에서 postgres:// 로 오는 경우 postgresql:// 로 변환
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

print(f"[VELOX] DB 연결 중...")
engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,    # 쿼리 전 연결 유효성 검사 → 끊긴 연결 자동 재접속
    pool_recycle=300,      # 5분마다 커넥션 갱신 (Supabase idle timeout 대비)
    pool_size=5,
    max_overflow=10,
)

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

# ── 종목별 최저가 하한 ────────────────────────────────
# 각 종목의 성격(시총·변동성·자산유형)을 반영한 개별 비율 적용
PRICE_FLOORS: dict[str, int] = {
    'os':  50_000,       # 오성전자   — 블루칩 대기업        초기가 20%
    'mr':  30_000,       # 미래자동차 — 경기민감 산업주       초기가 15%
    'nv':  75_000,       # AND비디아  — AI·반도체 고성장주    초기가 15%
    'bn':  45_000,       # bAnana    — 빅테크 고변동         초기가 15%
    'btc': 12_500_000,   # 바이트코인 — 대장코인 대형         초기가 25%
    'bth': 700_000,      # Both코인  — 중형 알트코인         초기가 10%
    'shi': 60,           # 시발이누  — 극고변동 밈코인        초기가  3%
    'dge': 1_000,        # 닷지코인  — 극고변동 밈코인        초기가  5%
    'jio': 20_000,       # JIODAQ   — 분산 지수, 안정적      초기가 20%
}

# 전역 가격 상태 (모든 유저 공유)
_price_lock = threading.Lock()
prices: dict[str, int] = dict(INITIAL_PRICES)
price_history: dict[str, list] = {k: [v] for k, v in INITIAL_PRICES.items()}
trade_impact: dict[str, float] = {k: 0.0 for k in INITIAL_PRICES}
_last_stock_tick = 0.0
_last_coin_tick = 0.0


def _register_listed_asset(asset_id: str, name: str, supply: int, init_price: int):
    """가격 엔진·메타데이터에 상장 종목을 등록 (서버 재시작 포함)"""
    with _price_lock:
        if asset_id not in prices:
            prices[asset_id] = init_price
            price_history[asset_id] = [init_price]
            trade_impact[asset_id] = 0.0
        ASSET_SHARES[asset_id] = supply
        ASSET_TYPES[asset_id]  = 'stock'
        ASSET_NAMES[asset_id]  = name
        # 유저 상장 종목: 초기가의 5%를 하한으로 설정
        PRICE_FLOORS[asset_id] = max(1, round(init_price * 0.05))
        if asset_id not in STOCK_IDS:
            STOCK_IDS.append(asset_id)


def _load_listed_assets():
    """서버 시작 시 DB 상장 종목을 가격 엔진에 복원"""
    try:
        with Session(engine) as db:
            listed = db.exec(select(ListedAsset)).all()
        for a in listed:
            _register_listed_asset(a.asset_id, a.name, a.supply, a.init_price)
        if listed:
            print(f"[VELOX] 상장 종목 {len(listed)}개 복원")
    except Exception as e:
        print(f"[VELOX] 상장 종목 복원 오류: {e}")


def _save_market_state():
    """현재 가격·히스토리를 DB에 영속 저장 (틱마다 호출)"""
    with _price_lock:
        p = dict(prices)
        h = {k: list(v) for k, v in price_history.items()}
    try:
        with Session(engine) as db:
            state = db.get(MarketState, 1)
            if state is None:
                state = MarketState(id=1)
                db.add(state)
            state.prices_json  = json.dumps(p)
            state.history_json = json.dumps(h)
            db.commit()
    except Exception as e:
        print(f"[VELOX] 가격 저장 오류: {e}")


def _load_market_state():
    """서버 시작 시 DB에서 마지막 가격 복원"""
    global prices, price_history
    try:
        with Session(engine) as db:
            state = db.get(MarketState, 1)
        if state:
            loaded_p = json.loads(state.prices_json)
            loaded_h = json.loads(state.history_json)
            with _price_lock:
                for k in INITIAL_PRICES:
                    if k in loaded_p:
                        prices[k] = loaded_p[k]
                    if k in loaded_h and loaded_h[k]:
                        price_history[k] = loaded_h[k]
            print(f"[VELOX] 가격 복원 완료: {loaded_p}")
        else:
            print("[VELOX] 저장된 가격 없음 → 초기값 사용")
    except Exception as e:
        print(f"[VELOX] 가격 복원 오류: {e}")


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
            pct = max(-0.50, min(0.50, pct))
            floor = PRICE_FLOORS.get(aid, 1)
            prices[aid] = max(floor, round(prices[aid] * (1 + pct)))
            price_history[aid].append(prices[aid])
            if len(price_history[aid]) > 60:
                price_history[aid].pop(0)
        _last_stock_tick = time.time()
    _save_market_state()


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
            floor = PRICE_FLOORS.get(aid, 1)
            prices[aid] = max(floor, round(prices[aid] * (1 + pct)))
            price_history[aid].append(prices[aid])
            if len(price_history[aid]) > 60:
                price_history[aid].pop(0)
        _last_coin_tick = time.time()
    _save_market_state()


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

class MarketState(SQLModel, table=True):
    """가격 엔진 상태를 DB에 영속화 — 항상 id=1 인 단일 행만 사용"""
    id:           int = Field(default=1, primary_key=True)
    prices_json:  str = Field(default="{}")
    history_json: str = Field(default="{}")


class ListedAsset(SQLModel, table=True):
    """플래티넘 유저가 직접 상장한 종목"""
    id:         Optional[int] = Field(default=None, primary_key=True)
    asset_id:   str  = Field(default="", unique=True, index=True)  # "u{id}" 형식
    name:       str
    owner_id:   int  = Field(foreign_key="user.id")
    capital:    float
    supply:     int
    init_price: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str = Field(default="")   # Google 로그인 유저는 빈 문자열
    is_admin: bool = Field(default=False)

    # Google OAuth 필드
    google_id:      str = Field(default="", index=True)
    google_email:   str = Field(default="")
    google_picture: str = Field(default="")

    balance: float = Field(default=100_000.0)
    tier_idx: int = Field(default=0)
    portfolio_json: str = Field(default="{}")
    cost_basis_json: str = Field(default="{}")
    loans_json: str = Field(default="[]")
    history_json: str = Field(default="[]")
    transfers_json: str = Field(default="[]")
    today_transferred: float = Field(default=0.0)
    last_transfer_date: str = Field(default="")  # "YYYY-MM-DD" 형식
    ban_until: str = Field(default="")            # "" = 정상 | "permanent" = 영구정지 | ISO datetime = 시간제 정지

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ── 공지 저장 (메모리) ────────────────────────────────
announcements: list = []

# ── DB 초기화 ─────────────────────────────────────────
SQLModel.metadata.create_all(engine)
print("[VELOX] DB 준비 완료")


def _run_migrations():
    """create_all은 기존 테이블의 새 컬럼을 추가하지 않으므로
    ALTER TABLE IF NOT EXISTS로 안전하게 컬럼을 추가한다."""
    stmts = [
        # user 테이블 — Google OAuth 컬럼
        "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS google_id          VARCHAR DEFAULT ''",
        "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS google_email       VARCHAR DEFAULT ''",
        "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS google_picture     VARCHAR DEFAULT ''",
        "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS last_transfer_date VARCHAR DEFAULT ''",
        "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS ban_until          VARCHAR DEFAULT ''",
    ]
    try:
        with engine.connect() as conn:
            for sql in stmts:
                conn.execute(text(sql))
            conn.commit()
        print("[VELOX] 마이그레이션 완료")
    except Exception as e:
        print(f"[VELOX] 마이그레이션 오류: {e}")


_run_migrations()

# ── 가격 복원 (재시작 후에도 시세 유지) ──────────────
_load_listed_assets()   # 상장 종목 먼저 등록 (가격 복원 시 히스토리 포함)
_load_market_state()    # 마지막 가격 덮어쓰기


# ── 헬퍼 ──────────────────────────────────────────────
def hash_pw(pw: str) -> str:
    return generate_password_hash(pw)


def verify_pw(pw: str, hashed: str) -> bool:
    # 기존 SHA-256 해시(64자리 hex) 호환 처리
    if len(hashed) == 64 and all(c in '0123456789abcdef' for c in hashed):
        import hashlib
        return hashlib.sha256(pw.encode()).hexdigest() == hashed
    return check_password_hash(hashed, pw)


def check_ban(u: User) -> tuple[bool, str]:
    """(정지여부, 메시지) 반환. 만료된 정지는 자동 해제."""
    if not u.ban_until:
        return False, ""
    if u.ban_until == "permanent":
        return True, "영구 정지된 계정입니다."
    try:
        ban_dt = datetime.fromisoformat(u.ban_until)
        if datetime.now(timezone.utc) < ban_dt:
            kst = ban_dt.strftime("%Y-%m-%d %H:%M")
            return True, f"{kst} UTC까지 정지된 계정입니다."
        return False, ""  # 정지 기간 만료
    except Exception:
        return False, ""


def user_to_dict(u: User) -> dict:
    return {
        "name": u.username,
        "isAdmin": u.is_admin,
        "googleEmail":   u.google_email,
        "googlePicture": u.google_picture,
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
    res = make_response(render_template("main.html"))
    res.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    res.headers['Pragma']        = 'no-cache'
    res.headers['Expires']       = '0'
    return res


@app.route("/api/me")
def api_me():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"loggedIn": False})
    with Session(engine) as db:
        u = db.get(User, uid)
        if not u:
            return jsonify({"loggedIn": False})
        banned, ban_msg = check_ban(u)
        if banned:
            return jsonify({"loggedIn": True, "banned": True, "banMessage": ban_msg})
        user_data = user_to_dict(u)
    return jsonify({"loggedIn": True, "banned": False, "user": user_data})


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if not username or not password:
        return jsonify({"ok": False, "error": "아이디와 비밀번호를 입력하세요."})
    with Session(engine) as db:
        user = db.exec(select(User).where(User.username == username)).first()
        if not user or not verify_pw(password, user.password_hash):
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
    if not re.fullmatch(r'[a-zA-Z0-9]+', username):
        return jsonify({"ok": False, "error": "아이디는 영문·숫자만 사용 가능합니다."})
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


@app.route("/auth/google")
def auth_google_start():
    """Google OAuth 시작 — 구글 로그인 페이지로 리다이렉트"""
    redirect_uri = url_for("auth_google_callback", _external=True)
    return google_oauth.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
def auth_google_callback():
    """Google OAuth 콜백 — 유저 생성/로그인 처리 후 메인 페이지로 이동"""
    try:
        token = google_oauth.authorize_access_token()
    except Exception as e:
        print(f"[OAuth 오류] {e}")
        return redirect("/?error=oauth_failed")

    info = token.get("userinfo")
    if not info:
        return redirect("/?error=no_userinfo")

    google_id  = info["sub"]
    email      = info.get("email", "")
    name       = info.get("name", email.split("@")[0])
    picture    = info.get("picture", "")

    with Session(engine) as db:
        # 1) 기존 Google 유저 조회
        user = db.exec(select(User).where(User.google_id == google_id)).first()

        if user:
            # 프로필 사진 최신화
            if user.google_picture != picture:
                user.google_picture = picture
                db.add(user)
                db.commit()
        else:
            # 2) 신규 유저 — 중복 없는 username 생성
            base = re.sub(r"[^a-zA-Z0-9]", "", name)[:16] or "user"
            if len(base) < 4:
                base = (base + "user")[:8]
            username = base
            n = 1
            while db.exec(select(User).where(User.username == username)).first():
                username = f"{base}{n}"; n += 1

            is_admin = bool(ADMIN_GOOGLE_EMAIL and email == ADMIN_GOOGLE_EMAIL)
            portfolio, cost_basis = {}, {}
            if is_admin:
                portfolio["btc"]  = ADMIN_BTC_QTY
                cost_basis["btc"] = ADMIN_BTC_QTY * ADMIN_BTC_PRICE

            user = User(
                username=username,
                google_id=google_id,
                google_email=email,
                google_picture=picture,
                is_admin=is_admin,
                balance=100_000.0,
                portfolio_json=json.dumps(portfolio),
                cost_basis_json=json.dumps(cost_basis),
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            print(f"[가입완료] {username} / email={email} / admin={is_admin}")

        session["user_id"] = user.id

    return redirect("/")


@app.route("/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/save", methods=["POST"])
def api_save():
    """클라이언트 전용 UI 상태 저장.
    잔액·포트폴리오·티어는 각 전용 API(/api/trade, /api/transfer,
    /api/gambling, /api/loan/*, /api/tier/upgrade)를 통해서만 변경됩니다.
    """
    user = get_current_user()
    if not user:
        return jsonify({"ok": False}), 401
    data = request.get_json(silent=True) or {}
    with Session(engine) as db:
        u = db.get(User, user.id)
        if not u:
            return jsonify({"ok": False}), 404
        # ⚠️  balance / tierIdx / portfolio / costBasis 는 여기서 절대 변경하지 않음
        if "history" in data:
            u.history_json = json.dumps(data["history"][-100:])
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


# ── 종목 상장 API ─────────────────────────────────────

@app.route("/api/listing", methods=["GET"])
def api_listing_get():
    """현재 상장된 모든 커스텀 종목 목록 반환"""
    try:
        with Session(engine) as db:
            listed = db.exec(select(ListedAsset)).all()
        return jsonify({
            "ok": True,
            "assets": [
                {
                    "id":        a.asset_id,
                    "name":      a.name,
                    "supply":    a.supply,
                    "initPrice": a.init_price,
                    "capital":   a.capital,
                }
                for a in listed
            ],
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/listing", methods=["POST"])
def api_listing_post():
    """플래티넘 유저 전용 종목 상장"""
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "로그인 필요"}), 401
    if user.tier_idx < 4:
        return jsonify({"ok": False, "error": "플래티넘 등급 전용입니다."})

    data    = request.get_json(silent=True) or {}
    name    = data.get("name", "").strip()
    capital = int(data.get("capital", 0))
    supply  = int(data.get("supply", 0))

    if not name or len(name) > 20:
        return jsonify({"ok": False, "error": "종목명은 1~20자입니다."})
    if capital < 1_000_000:
        return jsonify({"ok": False, "error": "자본금은 최소 100만원입니다."})
    if supply < 1000:
        return jsonify({"ok": False, "error": "발행량은 최소 1,000개입니다."})

    with Session(engine) as db:
        u = db.get(User, user.id)
        if not u:
            return jsonify({"ok": False, "error": "유저 없음"}), 404
        if u.balance < capital:
            return jsonify({"ok": False, "error": "잔액이 부족합니다."})

        # 중복 이름 검사
        if db.exec(select(ListedAsset).where(ListedAsset.name == name)).first():
            return jsonify({"ok": False, "error": "이미 존재하는 종목명입니다."})

        init_price = max(1, round(capital / supply))

        # 1차 insert (PK 확보)
        new_asset = ListedAsset(
            asset_id="",
            name=name,
            owner_id=u.id,
            capital=float(capital),
            supply=supply,
            init_price=init_price,
        )
        db.add(new_asset)
        db.commit()
        db.refresh(new_asset)

        # asset_id = "u{pk}"
        new_asset.asset_id = f"u{new_asset.id}"
        u.balance -= capital
        u.updated_at = datetime.now(timezone.utc)
        db.add(new_asset)
        db.add(u)
        db.commit()
        db.refresh(new_asset)

        asset_id = new_asset.asset_id
        new_balance = u.balance

    # 가격 엔진에 실시간 등록
    _register_listed_asset(asset_id, name, supply, init_price)
    _save_market_state()

    print(f"[상장] {name} ({asset_id}) 발행량={supply:,} 초기가=₩{init_price:,} by uid={user.id}")
    return jsonify({
        "ok":      True,
        "asset":   {"id": asset_id, "name": name, "supply": supply, "initPrice": init_price},
        "balance": new_balance,
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
            floor = PRICE_FLOORS.get(aid, 1)
            prices[aid] = max(floor, round(old_price * (1 + clamped)))
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

        # 날짜가 바뀌었으면 오늘 송금액 초기화
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if sender.last_transfer_date != today_str:
            sender.today_transferred = 0.0
            sender.last_transfer_date = today_str

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


# ══════════════════════════════════════════════════════
#  도박 API (서버에서 결과 계산)
# ══════════════════════════════════════════════════════

TIER_GAMBLING_LIMITS = {0: 3_000_000, 1: 5_000_000, 2: 10_000_000, 3: 30_000_000, 4: 50_000_000}

def _make_deck():
    suits = ['♠', '♥', '♦', '♣']
    ranks = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']
    deck = [{'suit': s, 'rank': r} for s in suits for r in ranks]
    random.shuffle(deck)
    return deck

def _card_value(c):
    if c['rank'] in ('J', 'Q', 'K'): return 10
    if c['rank'] == 'A': return 11
    return int(c['rank'])

def _hand_score(hand):
    s = sum(_card_value(c) for c in hand)
    aces = sum(1 for c in hand if c['rank'] == 'A')
    while s > 21 and aces > 0:
        s -= 10; aces -= 1
    return s


@app.route("/api/gambling/bet", methods=["POST"])
def api_gambling_bet():
    """경마 / 바카라 — 한 번의 요청으로 결과 반환"""
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "로그인 필요"}), 401

    data   = request.get_json(silent=True) or {}
    game   = data.get("game", "")
    bet    = int(data.get("bet", 0))
    sel    = data.get("selection", "")

    if game not in ("horse", "baccarat"):
        return jsonify({"ok": False, "error": "지원하지 않는 게임"})
    if bet <= 0:
        return jsonify({"ok": False, "error": "베팅 금액 오류"})

    with Session(engine) as db:
        u = db.get(User, user.id)
        if not u:
            return jsonify({"ok": False, "error": "유저 없음"}), 404

        limit = TIER_GAMBLING_LIMITS.get(u.tier_idx, 3_000_000)
        if bet > limit:
            return jsonify({"ok": False, "error": f"베팅 한도 초과 (최대 ₩{limit:,})"})
        if u.balance < bet:
            return jsonify({"ok": False, "error": "잔액이 부족합니다"})

        u.balance -= bet
        payout = 0

        if game == "horse":
            horses = ['A', 'B', 'C', 'D', 'E']
            shuffled = horses[:]
            random.shuffle(shuffled)
            horse_ranks = {h: i + 1 for i, h in enumerate(shuffled)}
            sel_rank = horse_ranks.get(sel, 5)
            if sel_rank == 1:   payout = round(bet * 5)
            elif sel_rank == 2: payout = round(bet * 3)
            elif sel_rank == 3: payout = round(bet * 1.8)
            result_data = {"horseRanks": horse_ranks, "selRank": sel_rank}

        else:  # baccarat
            r = random.random()
            outcome = 'tie' if r < 0.09 else ('player' if r < 0.54 else 'banker')
            mult = {'tie': 10, 'player': 2.3, 'banker': 1.8}
            if sel == outcome:
                payout = round(bet * mult[outcome])
            result_data = {"outcome": outcome}

        u.balance += payout
        net = payout - bet

        history = json.loads(u.history_json or "[]")
        history.append({"name": "경마" if game == "horse" else "바카라",
                         "type": "sell" if net > 0 else "buy", "amount": abs(net)})
        u.history_json = json.dumps(history[-100:])
        u.updated_at = datetime.now(timezone.utc)
        db.add(u)
        db.commit()
        final_balance = u.balance

    return jsonify({"ok": True, "payout": payout, "net": net,
                    "balance": final_balance, "result": result_data})


@app.route("/api/gambling/blackjack/deal", methods=["POST"])
def api_bj_deal():
    """블랙잭 — 초기 딜"""
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "로그인 필요"}), 401

    data = request.get_json(silent=True) or {}
    bet  = int(data.get("bet", 0))

    with Session(engine) as db:
        u = db.get(User, user.id)
        if not u:
            return jsonify({"ok": False, "error": "유저 없음"}), 404

        limit = TIER_GAMBLING_LIMITS.get(u.tier_idx, 3_000_000)
        if bet <= 0 or bet > limit:
            return jsonify({"ok": False, "error": "베팅 금액 오류"})
        if u.balance < bet:
            return jsonify({"ok": False, "error": "잔액이 부족합니다"})

        u.balance -= bet
        db.add(u)
        db.commit()
        final_balance = u.balance

    deck = _make_deck()
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    session['bj'] = {'deck': deck, 'player': player, 'dealer': dealer, 'bet': bet}

    ps = _hand_score(player)
    if ps == 21:
        # 블랙잭 즉시 처리
        payout = round(bet * 2.5)
        with Session(engine) as db:
            u = db.get(User, user.id)
            u.balance += payout
            net = payout - bet
            history = json.loads(u.history_json or "[]")
            history.append({"name": "블랙잭", "type": "sell" if net > 0 else "buy", "amount": abs(net)})
            u.history_json = json.dumps(history[-100:])
            u.updated_at = datetime.now(timezone.utc)
            db.add(u); db.commit()
            final_balance = u.balance
        session.pop('bj', None)
        return jsonify({"ok": True, "result": "blackjack", "payout": payout, "net": net,
                        "balance": final_balance, "player": player, "dealer": dealer})

    return jsonify({"ok": True, "result": "continue", "balance": final_balance,
                    "player": player, "dealerVisible": dealer[0], "playerScore": ps})


@app.route("/api/gambling/blackjack/action", methods=["POST"])
def api_bj_action():
    """블랙잭 — 히트 / 스탠드"""
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "로그인 필요"}), 401

    bj = session.get('bj')
    if not bj:
        return jsonify({"ok": False, "error": "진행 중인 게임 없음"})

    data   = request.get_json(silent=True) or {}
    action = data.get("action", "")
    deck   = bj['deck']
    player = bj['player']
    dealer = bj['dealer']
    bet    = bj['bet']

    if action == "hit":
        player.append(deck.pop())
        bj['player'] = player
        session['bj'] = bj
        ps = _hand_score(player)
        if ps > 21:
            # 버스트
            with Session(engine) as db:
                u = db.get(User, user.id)
                net = -bet
                history = json.loads(u.history_json or "[]")
                history.append({"name": "블랙잭", "type": "buy", "amount": abs(net)})
                u.history_json = json.dumps(history[-100:])
                u.updated_at = datetime.now(timezone.utc)
                db.add(u); db.commit()
                final_balance = u.balance
            session.pop('bj', None)
            return jsonify({"ok": True, "result": "bust", "payout": 0, "net": net,
                            "balance": final_balance, "player": player, "dealer": dealer, "playerScore": ps})
        return jsonify({"ok": True, "result": "continue",
                        "player": player, "dealerVisible": dealer[0], "playerScore": ps})

    elif action == "stand":
        while _hand_score(dealer) < 17:
            dealer.append(deck.pop())
        ps = _hand_score(player)
        ds = _hand_score(dealer)

        if ds > 21 or ps > ds:   result, mult = "win", 2
        elif ps == ds:             result, mult = "draw", 1
        else:                      result, mult = "lose", 0

        payout = round(bet * mult)
        net    = payout - bet

        with Session(engine) as db:
            u = db.get(User, user.id)
            u.balance += payout
            history = json.loads(u.history_json or "[]")
            history.append({"name": "블랙잭", "type": "sell" if net > 0 else "buy", "amount": abs(net)})
            u.history_json = json.dumps(history[-100:])
            u.updated_at = datetime.now(timezone.utc)
            db.add(u); db.commit()
            final_balance = u.balance

        session.pop('bj', None)
        return jsonify({"ok": True, "result": result, "payout": payout, "net": net,
                        "balance": final_balance, "player": player, "dealer": dealer,
                        "playerScore": ps, "dealerScore": ds})

    return jsonify({"ok": False, "error": "action 오류"})


# ══════════════════════════════════════════════════════
#  대출 API
# ══════════════════════════════════════════════════════

MAX_LOAN = 100_000_000


@app.route("/api/loan/apply", methods=["POST"])
def api_loan_apply():
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "로그인 필요"}), 401

    data = request.get_json(silent=True) or {}
    amount = int(data.get("amount", 0))

    if amount <= 0 or amount > MAX_LOAN:
        return jsonify({"ok": False, "error": f"대출 금액은 1 ~ {MAX_LOAN:,}원 이내여야 합니다."})

    with Session(engine) as db:
        u = db.get(User, user.id)
        if not u:
            return jsonify({"ok": False, "error": "유저 없음"}), 404

        loans = json.loads(u.loans_json or "[]")
        if loans:
            return jsonify({"ok": False, "error": "기존 대출을 먼저 상환하세요."})

        hourly = round(amount * 0.008)
        loans.append({
            "name": "일반 대출", "rate": "0.8%/시간",
            "original": amount, "remaining": amount,
            "daily": hourly, "hourly": hourly,
            "issuedAt": datetime.now(timezone.utc).isoformat(),
        })
        u.loans_json = json.dumps(loans)
        u.balance += amount
        u.updated_at = datetime.now(timezone.utc)
        db.add(u); db.commit()
        final_balance = u.balance

    return jsonify({"ok": True, "balance": final_balance, "loans": loans})


@app.route("/api/loan/repay", methods=["POST"])
def api_loan_repay():
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "로그인 필요"}), 401

    data = request.get_json(silent=True) or {}
    idx  = int(data.get("idx", 0))

    with Session(engine) as db:
        u = db.get(User, user.id)
        if not u:
            return jsonify({"ok": False, "error": "유저 없음"}), 404

        loans = json.loads(u.loans_json or "[]")
        if idx < 0 or idx >= len(loans):
            return jsonify({"ok": False, "error": "대출 항목 없음"})

        loan = loans[idx]

        # 대출 발행 후 경과 시간에 비례해 이자 업데이트
        issued_at_str = loan.get("issuedAt")
        if issued_at_str:
            issued_at = datetime.fromisoformat(issued_at_str)
            now = datetime.now(timezone.utc)
            hours_elapsed = (now - issued_at).total_seconds() / 3600
            accrued = round(loan["original"] * 0.008 * hours_elapsed)
            loan["remaining"] = loan["original"] + accrued

        remaining = loan["remaining"]
        if u.balance < remaining:
            return jsonify({"ok": False, "error": f"잔액이 부족합니다 (필요: ₩{remaining:,})"})

        u.balance -= remaining
        loans.pop(idx)
        u.loans_json = json.dumps(loans)
        u.updated_at = datetime.now(timezone.utc)
        db.add(u); db.commit()
        final_balance = u.balance

    return jsonify({"ok": True, "balance": final_balance, "loans": loans})


@app.route("/api/loan/interest", methods=["POST"])
def api_loan_interest():
    """클라이언트가 주기적으로 호출 → 서버에서 이자 계산 후 잔액 차감"""
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "로그인 필요"}), 401

    with Session(engine) as db:
        u = db.get(User, user.id)
        if not u:
            return jsonify({"ok": False, "error": "유저 없음"}), 404

        loans = json.loads(u.loans_json or "[]")
        if not loans:
            return jsonify({"ok": True, "balance": u.balance, "loans": loans, "charged": 0})

        total_interest = 0
        now = datetime.now(timezone.utc)
        for loan in loans:
            last_tick_str = loan.get("lastTick") or loan.get("issuedAt")
            if last_tick_str:
                last_tick = datetime.fromisoformat(last_tick_str)
                hours = (now - last_tick).total_seconds() / 3600
                interest = round(loan["original"] * 0.008 * hours)
                loan["remaining"] += interest
                total_interest += interest
            loan["lastTick"] = now.isoformat()

        u.balance = max(0, u.balance - total_interest)
        u.loans_json = json.dumps(loans)
        u.updated_at = now
        db.add(u); db.commit()
        final_balance = u.balance

    return jsonify({"ok": True, "balance": final_balance, "loans": loans, "charged": total_interest})


# ══════════════════════════════════════════════════════
#  티어 승급 API
# ══════════════════════════════════════════════════════

TIER_COSTS = {1: 100_000_000, 2: 1_000_000_000, 3: 5_000_000_000, 4: 10_000_000_000}
TIER_NAMES = {0: "IRON", 1: "BRONZE", 2: "SILVER", 3: "GOLD", 4: "PLATINUM"}


@app.route("/api/tier/upgrade", methods=["POST"])
def api_tier_upgrade():
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "로그인 필요"}), 401

    data     = request.get_json(silent=True) or {}
    target   = int(data.get("tier", -1))

    if target not in TIER_COSTS:
        return jsonify({"ok": False, "error": "유효하지 않은 티어"})

    with Session(engine) as db:
        u = db.get(User, user.id)
        if not u:
            return jsonify({"ok": False, "error": "유저 없음"}), 404

        if u.tier_idx >= target:
            return jsonify({"ok": False, "error": "이미 해당 등급 이상입니다."})
        if target != u.tier_idx + 1:
            return jsonify({"ok": False, "error": "한 단계씩만 승급 가능합니다."})

        cost = TIER_COSTS[target]
        # 총 자산(현금) 기준으로 체크 — 포트폴리오 평가액은 클라이언트에서 확인
        if u.balance < cost:
            return jsonify({"ok": False, "error": f"현금이 부족합니다 (필요: ₩{cost:,})"})

        u.balance  -= cost
        u.tier_idx  = target
        u.updated_at = datetime.now(timezone.utc)
        db.add(u); db.commit()
        final_balance = u.balance

    return jsonify({"ok": True, "tierIdx": target,
                    "tierName": TIER_NAMES[target], "balance": final_balance})


# ── 관리자 API ─────────────────────────────────────────

@app.route("/api/admin/users")
@require_admin
def admin_get_users():
    with Session(engine) as db:
        users = db.exec(select(User)).all()
        user_list = []
        for u in users:
            try:
                portfolio  = json.loads(u.portfolio_json  or "{}")
                cost_basis = json.loads(u.cost_basis_json or "{}")
            except Exception:
                portfolio  = {}
                cost_basis = {}
            portfolio_detail = []
            for aid, qty in portfolio.items():
                if qty <= 0:
                    continue
                cost      = cost_basis.get(aid, 0)
                avg_price = round(cost / qty) if qty > 0 else 0
                cur_price = prices.get(aid, avg_price)
                cur_val   = cur_price * qty
                pnl       = cur_val - cost
                roe       = round(pnl / cost * 100, 2) if cost > 0 else 0
                portfolio_detail.append({
                    "id": aid, "name": ASSET_NAMES.get(aid, aid),
                    "qty": qty, "avgPrice": avg_price,
                    "curPrice": cur_price, "curVal": round(cur_val),
                    "pnl": round(pnl), "roe": roe,
                })
            try:
                loans = json.loads(u.loans_json or "[]")
            except Exception:
                loans = []
            total_loan = sum(l.get("remaining", 0) for l in loans)
            banned, ban_msg = check_ban(u)
            user_list.append({
                "id": u.id, "username": u.username, "isAdmin": u.is_admin,
                "googleEmail": u.google_email or "",
                "googlePicture": u.google_picture or "",
                "balance": u.balance, "tierIdx": u.tier_idx,
                "totalLoan": total_loan,
                "portfolioDetail": portfolio_detail,
                "createdAt": u.created_at.strftime("%Y-%m-%d %H:%M"),
                "isBanned": banned,
                "banUntil": u.ban_until or "",
                "banMessage": ban_msg,
            })
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


@app.route("/api/admin/user/<int:uid>/suspend", methods=["POST"])
@require_admin
def admin_suspend_user(uid: int):
    """정지: hours=0 → 영구, hours>0 → 시간제"""
    data  = request.get_json(silent=True) or {}
    hours = int(data.get("hours", 0))
    with Session(engine) as db:
        u = db.get(User, uid)
        if not u or u.is_admin:
            return jsonify({"ok": False, "error": "불가"}), 400
        if hours == 0:
            u.ban_until = "permanent"
        else:
            from datetime import timedelta
            until = datetime.now(timezone.utc) + timedelta(hours=hours)
            u.ban_until = until.isoformat()
        u.updated_at = datetime.now(timezone.utc)
        db.add(u)
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/user/<int:uid>/unsuspend", methods=["POST"])
@require_admin
def admin_unsuspend_user(uid: int):
    """정지 해제"""
    with Session(engine) as db:
        u = db.get(User, uid)
        if not u: return jsonify({"ok": False, "error": "유저 없음"}), 404
        u.ban_until  = ""
        u.updated_at = datetime.now(timezone.utc)
        db.add(u)
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/user/<int:uid>/rename", methods=["POST"])
@require_admin
def admin_rename_user(uid: int):
    """유저 이름 변경"""
    data     = request.get_json(silent=True) or {}
    new_name = data.get("username", "").strip()
    if not new_name or not re.fullmatch(r'[a-zA-Z0-9가-힣]{2,20}', new_name):
        return jsonify({"ok": False, "error": "이름은 2~20자 (영문·숫자·한글)"})
    with Session(engine) as db:
        # 중복 확인
        if db.exec(select(User).where(User.username == new_name)).first():
            return jsonify({"ok": False, "error": "이미 사용 중인 이름입니다."})
        u = db.get(User, uid)
        if not u: return jsonify({"ok": False, "error": "유저 없음"}), 404
        u.username   = new_name
        u.updated_at = datetime.now(timezone.utc)
        db.add(u)
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/user/<int:uid>/ban", methods=["POST"])
@require_admin
def admin_ban_user(uid: int):
    """레거시 — 단순 자산 초기화 (정지 아님)"""
    with Session(engine) as db:
        u = db.get(User, uid)
        if not u or u.is_admin: return jsonify({"ok": False, "error": "불가"}), 400
        u.balance           = 0
        u.tier_idx          = 0
        u.portfolio_json    = "{}"
        u.cost_basis_json   = "{}"
        u.loans_json        = "[]"
        u.history_json      = "[]"
        u.transfers_json    = "[]"
        u.today_transferred = 0.0
        u.updated_at        = datetime.now(timezone.utc)
        db.add(u)
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/user/<int:uid>/delete", methods=["POST"])
@require_admin
def admin_delete_user(uid: int):
    with Session(engine) as db:
        u = db.get(User, uid)
        if not u:      return jsonify({"ok": False, "error": "유저 없음"}), 404
        if u.is_admin: return jsonify({"ok": False, "error": "관리자는 삭제 불가"}), 400
        db.delete(u)
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/user/<int:uid>/reset", methods=["POST"])
@require_admin
def admin_reset_user(uid: int):
    with Session(engine) as db:
        u = db.get(User, uid)
        if not u or u.is_admin: return jsonify({"ok": False, "error": "불가"}), 400
        u.balance           = 100_000.0
        u.tier_idx          = 0
        u.portfolio_json    = "{}"
        u.cost_basis_json   = "{}"
        u.loans_json        = "[]"
        u.history_json      = "[]"
        u.transfers_json    = "[]"
        u.today_transferred = 0.0
        u.updated_at        = datetime.now(timezone.utc)
        db.add(u)
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


# ── ads.txt (AdSense 인증용) ──────────────────────────
@app.route("/ads.txt")
def ads_txt():
    return "google.com, pub-7799486292050157, DIRECT, f08c47fec0942fa0", 200, {"Content-Type": "text/plain"}


# ── 실행 ───────────────────────────────────────────────
_engine_thread.start()

if __name__ == "__main__":
    print(f"👑 관리자: {ADMIN_USERNAME}")
    print("🚀 http://localhost:5000\n")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)