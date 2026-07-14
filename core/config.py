import os
from pathlib import Path


def _load_dotenv():
    current = Path(__file__).resolve()

    for parent in current.parents:
        env_path = parent / ".env"

        if not env_path.exists():
            continue 

        for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip()

        return

    print("⚠️ .env not found")

_load_dotenv()

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
API_KEY = os.getenv("API_KEY", "")
ADMIN_2FA_SECRET = os.getenv("ADMIN_2FA_SECRET", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
LOSTARK_API_KEY = os.getenv("LOSTARK_API_KEY", "")
LOSTARK_API_SUB1_KEY = os.getenv("LOSTARK_API_SUB1_KEY", "")
LOSTARK_API_SUB2_KEY = os.getenv("LOSTARK_API_SUB2_KEY", "")
SUPPORTER_CLASSES = ["바드", "도화가", "홀리나이트", "발키리"]

SERVER_LIST = [
    "카단", "카제로스", "니나브", "루페온",
    "실리안", "아만", "아브렐슈드", "카마인",
]

# Sticker / Webhook settings
STICKER_WEBHOOK_NAME = "Egg Sticker Relay"
STICKER_USERNAME_SUFFIX = " · Egg"
NO_RESIZE_KEYS = set()

# 봇 소유자(계란) 디스코드 사용자 ID. .env의 EGG_ID 값을 읽어옴.
_egg_id_raw = os.getenv("EGG_ID", "").strip()
try:
    EGG_ID = int(_egg_id_raw) if _egg_id_raw else 0
except ValueError:
    print(f"⚠️ EGG_ID 값이 올바른 숫자가 아니에요: {_egg_id_raw!r} -> 0으로 처리합니다.")
    EGG_ID = 0

if EGG_ID == 0:
    print("⚠️ .env에 EGG_ID가 설정되어 있지 않아요. 계란 전용 명령어가 아무에게도 허용되지 않습니다.")