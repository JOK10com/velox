# ── SMTP 테스트용 라우트 ─────────────────────────
@app.route("/api/test-email", methods=["POST"])
def test_email():
    data = request.get_json(silent=True) or {}
    to_email = data.get("to")
    if not to_email:
        return jsonify({"ok": False, "error": "수신 이메일 필요"})
    code = str(random.randint(100000, 999999))
    try:
        send_email_bg(to_email, code)
        return jsonify({"ok": True, "message": f"테스트 이메일 발송 완료 → {to_email}", "code": code})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
