from __future__ import annotations

import os
import json
import hashlib
from datetime import datetime, timezone
from typing import Optional

from flask import Flask, render_template, request, jsonify, session
from sqlmodel import Field, Session, SQLModel, create_engine, select
import sqlite3 as _sqlite3

# ── 앱 설정 ──────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "velox-dev-secret-key-change-in-prod")

# ── DB 설정 (Render 안전 경로) ──────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, "data")  # data 폴더
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "database.db")
print(f"[VELOX] DB 경로: {DB_PATH}")

engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)

# ── 관리자 이메일 / 초기 지분 ────────────────
ADMIN_EMAIL = "flyingkjo@dgsw.hs.kr"
ADMIN_BTC_QTY   = int(40_000_000 * 0.001)
ADMIN_BTC_PRICE = 50_000_000

# ── 모델 ──────────────────────────────
class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    email: str = Field(unique=True)
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

announcements: list = []

# ── DB 테이블 생성 + 마이그레이션 ──────────────
def _migrate():
    try:
        con = _sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("PRAGMA table_info(user)")
        existing = [row[1] for row in cur.fetchall()]
        cols = [
            ("is_admin", "INTEGER NOT NULL DEFAULT 0"),
            ("cost_basis_json", "TEXT NOT NULL DEFAULT '{}'"),
        ]
        for col, typedef in cols:
            if col not in existing:
                cur.execute(f"ALTER TABLE user ADD COLUMN {col} {typedef}")
                print(f"[VELOX] 컬럼 추가: {col}")
        con.commit()
        con.close()
    except Exception as e:
        print(f"[VELOX] 마이그레이션 오류: {e}")

SQLModel.metadata.create_all(engine)
_migrate()
print(f"[VELOX] DB 준비 완료")

# ── 헬퍼 ──────────────────────────────
def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def user_to_dict(u: User) -> dict:
    return {
        "name": u.username,
        "email": u.email,
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
            return jsonify({"ok": False, "error": "관리자 권한 필요"}), 403
        return f(*args, **kwargs)
    return decorated

# ── Routes ──────────────────────────────
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
    return jsonify({"ok": True, "user": user_to_dict(user)})

# [수정됨] 이메일 인증코드 로직이 삭제되고 즉시 가입되도록 변경
@app.route("/api/signup", methods=["POST"])
def api_signup():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    email    = data.get("email", "").strip().lower()

    if not username or not password or not email:
        return jsonify({"ok": False, "error": "모든 항목을 입력해주세요."})

    is_admin = (email == ADMIN_EMAIL)
    portfolio  = {"btc": ADMIN_BTC_QTY} if is_admin else {}
    cost_basis = {"btc": ADMIN_BTC_QTY*ADMIN_BTC_PRICE} if is_admin else {}
    
    with Session(engine) as db:
        if db.exec(select(User).where(User.username == username)).first():
            return jsonify({"ok": False, "error": "이미 사용 중인 아이디입니다."})
        if db.exec(select(User).where(User.email == email)).first():
            return jsonify({"ok": False, "error": "이미 가입된 이메일입니다."})
        
        user = User(
            username=username,
            email=email,
            password_hash=hash_pw(password),
            is_admin=is_admin,
            balance=100_000.0,
            tier_idx=0,
            portfolio_json=json.dumps(portfolio),
            cost_basis_json=json.dumps(cost_basis)
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        session["user_id"] = user.id

    print(f"[가입완료] {username} ({email}) {'[관리자]' if is_admin else ''}")
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
        if "portfolio"        in data: u.portfolio_json   = json.dumps(data["portfolio"])
        if "loans"            in data: u.loans_json       = json.dumps(data["loans"])
        if "history"          in data: u.history_json     = json.dumps(data["history"][-100:])
        if "transfers"        in data: u.transfers_json   = json.dumps(data["transfers"][-50:])
        if "todayTransferred" in data: u.today_transferred = float(data["todayTransferred"])
        u.updated_at = datetime.now(timezone.utc)
        db.add(u)
        db.commit()
    return jsonify({"ok": True})

# ── 기타 필요한 라우트가 있다면 이 부분에 추가 ──

# ── 실행 ──────────────────────────────
if __name__ == "__main__":
    print(f"👑 관리자 이메일: {ADMIN_EMAIL}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
