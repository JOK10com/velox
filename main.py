from __future__ import annotations

import os
import json
import random
import hashlib
import smtplib
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# .env 파일 로드 (로컬 개발용 — Render에서는 환경변수로 자동 주입)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, render_template, request, jsonify, session
from sqlmodel import Field, Session, SQLModel, create_engine, select

# ── 앱 설정 ────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "velox-dev-secret-key-change-in-prod")

# ── DB 경로 (로컬: database.db / Render: /data/database.db) ─
DB_PATH = os.environ.get("DB_PATH", "database.db")
engine  = create_engine(f"sqlite:///{DB_PATH}", echo=False)

# ── SMTP 설정 (환경변수 우선, 없으면 빈 값) ──────────────────
smtp_config = {
    "host":     os.environ.get("SMTP_HOST", "smtp.gmail.com"),
    "port":     int(os.environ.get("SMTP_PORT", "465")),
    "email":    os.environ.get("SMTP_EMAIL", ""),
    "password": os.environ.get("SMTP_PASSWORD", ""),
}

# ── 관리자 이메일 ──────────────────────────────────────────
ADMIN_EMAIL = "flyingkjo@dgsw.hs.kr"

# ── 관리자 초기 지분 (바이트코인 0.1% = 40,000개) ───────────
ADMIN_BTC_QTY   = int(40_000_000 * 0.001)
ADMIN_BTC_PRICE = 50_000_000


# ── 모델 ──────────────────────────────────────────────────
class User(SQLModel, table=True):
    id: Optional[int]        = Field(default=None, primary_key=True)
    username: str             = Field(unique=True, index=True)
    email: str                = Field(unique=True)
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


# 인증번호 임시 저장
_pending: dict = {}

# 공지 저장
announcements: list = []


# ── 헬퍼 ──────────────────────────────────────────────────
def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def user_to_dict(u: User) -> dict:
    return {
        "name":             u.username,
        "email":            u.email,
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


def send_email_bg(to_email: str, code: str):
    cfg = smtp_config
    if not cfg["email"] or not cfg["password"]:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[VELOX] 인증번호: {code}"
        msg["From"]    = f"VELOX <{cfg['email']}>"
        msg["To"]      = to_email
        html = f"""<div style="font-family:sans-serif;max-width:400px;margin:0 auto;
  padding:32px 24px;background:#f7f7f5;border-radius:12px;">
  <h2 style="letter-spacing:3px;color:#111;">VELOX</h2>
  <p style="color:#555;font-size:.9rem;margin-bottom:20px;">회원가입 이메일 인증</p>
  <div style="background:#1a1a18;border-radius:10px;padding:24px;text-align:center;">
    <div style="font-size:2rem;font-family:monospace;font-weight:700;
      letter-spacing:12px;color:#f0d080;">{code}</div>
  </div>
  <p style="color:#999;font-size:.8rem;margin-top:16px;">5분 내에 입력해주세요.</p>
</div>"""
        msg.attach(MIMEText(f"VELOX 인증번호: {code}\n5분 내에 입력해주세요.", "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=10) as smtp:
            smtp.login(cfg["email"], cfg["password"])
            smtp.sendmail(cfg["email"], to_email, msg.as_string())
        print(f"[VELOX] 이메일 발송 완료 → {to_email}")
    except Exception as e:
        print(f"[SMTP 오류] {e}")


# ══════════════════════════════════════════════════════════
#  Routes
# ══════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("main.html")


@app.route("/api/me")
def api_me():
    user = get_current_user()
    if not user:
        return jsonify({"loggedIn": False})
    return jsonify({"loggedIn": True, "user": user_to_dict(user)})


# ── 인증 ──────────────────────────────────────────────────

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


@app.route("/api/send-code", methods=["POST"])
def api_send_code():
    data     = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    email    = data.get("email", "").strip().lower()

    if not username or not email:
        return jsonify({"ok": False, "error": "모든 항목을 입력해주세요."})
    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"ok": False, "error": "올바른 이메일 주소를 입력하세요."})

    with Session(engine) as db:
        if db.exec(select(User).where(User.username == username)).first():
            return jsonify({"ok": False, "error": "이미 사용 중인 아이디입니다."})
        if db.exec(select(User).where(User.email == email)).first():
            return jsonify({"ok": False, "error": "이미 가입된 이메일입니다."})

    code = str(random.randint(100000, 999999))
    _pending[email] = {
        "code":    code,
        "expires": datetime.now(timezone.utc) + timedelta(minutes=5),
    }

    print(f"\n{'='*40}\n  이메일  : {email}\n  인증번호: {code}\n{'='*40}\n")

    # SMTP 설정 있으면 이메일 발송
    if smtp_config["email"] and smtp_config["password"]:
        threading.Thread(target=send_email_bg, args=(email, code), daemon=True).start()

    return jsonify({"ok": True, "code": code})


@app.route("/api/signup", methods=["POST"])
def api_signup():
    data     = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    email    = data.get("email", "").strip().lower()
    code     = data.get("code", "").strip()

    pending = _pending.get(email)
    if not pending:
        return jsonify({"ok": False, "error": "인증번호를 먼저 전송해주세요."})
    if datetime.now(timezone.utc) > pending["expires"]:
        del _pending[email]
        return jsonify({"ok": False, "error": "인증번호가 만료됐습니다. 다시 전송해주세요."})
    if pending["code"] != code:
        return jsonify({"ok": False, "error": "인증번호가 틀렸습니다."})

    is_admin = (email == ADMIN_EMAIL)

    if is_admin:
        portfolio  = {"btc": ADMIN_BTC_QTY}
        cost_basis = {"btc": ADMIN_BTC_QTY * ADMIN_BTC_PRICE}
        start_balance = 100_000.0   # 일반 유저와 동일 잔고
        tier_idx = 0                # 일반 등급에서 시작
    else:
        portfolio  = {}
        cost_basis = {}
        start_balance = 100_000.0
        tier_idx = 0

    with Session(engine) as db:
        if db.exec(select(User).where(User.username == username)).first():
            return jsonify({"ok": False, "error": "이미 사용 중인 아이디입니다."})
        if db.exec(select(User).where(User.email == email)).first():
            return jsonify({"ok": False, "error": "이미 가입된 이메일입니다."})

        user = User(
            username        = username,
            email           = email,
            password_hash   = hash_pw(password),
            is_admin        = is_admin,
            balance         = start_balance,
            tier_idx        = tier_idx,
            portfolio_json  = json.dumps(portfolio),
            cost_basis_json = json.dumps(cost_basis),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        session["user_id"] = user.id

    del _pending[email]
    print(f"[가입완료] {username} ({email}) {'[관리자]' if is_admin else ''}")
    return jsonify({"ok": True, "user": user_to_dict(user)})


@app.route("/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


# ── 게임 상태 저장 ─────────────────────────────────────────

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


# ── 관리자 API ─────────────────────────────────────────────

@app.route("/api/admin/users")
@require_admin
def admin_get_users():
    with Session(engine) as db:
        users = db.exec(select(User)).all()
    return jsonify({"ok": True, "users": [
        {
            "id":        u.id,
            "username":  u.username,
            "email":     u.email,
            "isAdmin":   u.is_admin,
            "balance":   u.balance,
            "tierIdx":   u.tier_idx,
            "createdAt": u.created_at.strftime("%Y-%m-%d %H:%M"),
        }
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
        if not u:
            return jsonify({"ok": False, "error": "유저 없음"}), 404
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
        if not u:
            return jsonify({"ok": False, "error": "유저 없음"}), 404
        u.tier_idx = int(tier)
        db.add(u); db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/user/<int:uid>/ban", methods=["POST"])
@require_admin
def admin_ban_user(uid: int):
    with Session(engine) as db:
        u = db.get(User, uid)
        if not u or u.is_admin:
            return jsonify({"ok": False, "error": "불가"}), 400
        u.balance    = 0
        u.tier_idx   = 0
        u.loans_json = "[]"
        db.add(u); db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/user/<int:uid>/reset", methods=["POST"])
@require_admin
def admin_reset_user(uid: int):
    with Session(engine) as db:
        u = db.get(User, uid)
        if not u or u.is_admin:
            return jsonify({"ok": False, "error": "불가"}), 400
        u.balance           = 100_000.0
        u.tier_idx          = 0
        u.portfolio_json    = "{}"
        u.cost_basis_json   = "{}"
        u.loans_json        = "[]"
        u.history_json      = "[]"
        u.transfers_json    = "[]"
        u.today_transferred = 0.0
        db.add(u); db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/announce", methods=["POST"])
@require_admin
def admin_announce():
    data = request.get_json(silent=True) or {}
    msg  = data.get("message", "").strip()
    if not msg:
        return jsonify({"ok": False, "error": "메시지 필요"})
    announcements.append({
        "message": msg,
        "time":    datetime.now(timezone.utc).strftime("%H:%M"),
    })
    if len(announcements) > 20:
        announcements.pop(0)
    print(f"[공지] {msg}")
    return jsonify({"ok": True})


@app.route("/api/announcements")
def get_announcements():
    return jsonify({"ok": True, "list": announcements})


# ── SMTP 설정 ──────────────────────────────────────────────

@app.route("/api/smtp-config", methods=["GET"])
def get_smtp_config():
    return jsonify({
        "host":        smtp_config["host"],
        "port":        smtp_config["port"],
        "email":       smtp_config["email"],
        "hasPassword": bool(smtp_config["password"]),
    })


@app.route("/api/smtp-config", methods=["POST"])
def set_smtp_config():
    data = request.get_json(silent=True) or {}
    if "host"     in data: smtp_config["host"]     = data["host"].strip()
    if "port"     in data: smtp_config["port"]     = int(data["port"])
    if "email"    in data: smtp_config["email"]    = data["email"].strip()
    if "password" in data: smtp_config["password"] = data["password"]
    return jsonify({"ok": True})


@app.route("/api/smtp-test", methods=["POST"])
def test_smtp():
    cfg = smtp_config
    if not cfg["email"] or not cfg["password"]:
        return jsonify({"ok": False, "error": "이메일과 비밀번호를 먼저 설정하세요."})
    try:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=8) as smtp:
            smtp.login(cfg["email"], cfg["password"])
        return jsonify({"ok": True})
    except smtplib.SMTPAuthenticationError:
        return jsonify({"ok": False, "error": "인증 실패 — 이메일/비밀번호를 확인하세요."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── 실행 ───────────────────────────────────────────────────
if __name__ == "__main__":
    SQLModel.metadata.create_all(engine)
    print("\n✅ database.db 준비 완료")
    print(f"👑 관리자 이메일: {ADMIN_EMAIL}")
    print("🚀 서버: http://localhost:5000\n")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
