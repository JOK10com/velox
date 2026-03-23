# VELOX — 가상 자산 증식 플랫폼

## 로컬 실행

```bash
pip install -r requirements.txt
python main.py
```

## Render 배포 방법

### 1. GitHub에 올리기

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/유저명/velox.git
git push -u origin main
```

### 2. Render 설정

1. [render.com](https://render.com) 접속 → New → Web Service
2. GitHub 저장소 연결
3. 아래 설정 입력:

| 항목 | 값 |
|------|-----|
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn main:app --bind 0.0.0.0:$PORT` |
| Instance Type | Free |

4. **Environment Variables** 추가:

| Key | Value |
|-----|-------|
| `SECRET_KEY` | (랜덤 문자열 — Render가 자동 생성) |
| `DB_PATH` | `/data/database.db` |

5. **Disks** 탭 → Add Disk:

| 항목 | 값 |
|------|-----|
| Name | `velox-db` |
| Mount Path | `/data` |
| Size | 1 GB |

6. Deploy 클릭

### 프로젝트 구조

```
velox/
├── main.py              ← Flask 서버
├── requirements.txt     ← 패키지 목록
├── render.yaml          ← Render 자동 설정
├── Procfile             ← 실행 명령
├── .gitignore
└── templates/
    └── main.html        ← 프론트엔드
```

### 관리자

- 이메일 `flyingkjo@dgsw.hs.kr` 로 회원가입 시 자동으로 관리자 지정
- 초기 지급: 바이트코인 0.1% (40,000개)
- 관리자 패널: 사이드바 → 👑 관리자 패널
