"""TTS 엔진(사용자별 목소리) 관리 모듈 (로컬 저장 버전)"""
import json
import os
import tempfile
from typing import Dict

# 봇 소스 위치(EGG-BOT/) 기준 절대경로로 고정 -> 실행 시 cwd가 달라져도 항상 같은 파일을 봄
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../EGG-BOT
_DATA_DIR = os.path.join(_BASE_DIR, "data")
DATA_FILE = os.path.join(_DATA_DIR, "tts_engines.json")

DEFAULT_ENGINE = "engine1"
VALID_ENGINE_IDS = {"engine1", "engine2", "engine3", "engine4", "engine5", "engine7", "engine9"}


class TTSEngineManager:
    def __init__(self):
        self.user_engines: Dict[int, str] = {}  # {user_id: engine_id}

        os.makedirs(_DATA_DIR, exist_ok=True)
        self._load()

    # =========================
    # 파일 로드 / 저장
    # =========================
    def _load(self):
        if not os.path.exists(DATA_FILE):
            return

        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.user_engines = {
                    int(k): str(v) for k, v in data.items()
                }
            print(f"TTS 엔진 설정 로드 완료: {len(self.user_engines)}명 (경로: {DATA_FILE})")
        except Exception as e:
            print(f"TTS 엔진 설정 로드 실패: {e}")

    def _save(self):
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=_DATA_DIR)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(self.user_engines, f, indent=2)
            os.replace(tmp_path, DATA_FILE)
        except Exception as e:
            print(f"TTS 엔진 설정 저장 실패: {e}")
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    # =========================
    # 엔진 관리
    # =========================
    def get_engine(self, user_id: int) -> str:
        """사용자의 엔진을 조회. 설정된 적 없으면 기본 엔진 반환."""
        return self.user_engines.get(user_id, DEFAULT_ENGINE)

    def set_engine(self, user_id: int, engine_id: str) -> bool:
        """사용자의 엔진을 설정."""
        if engine_id not in VALID_ENGINE_IDS:
            raise ValueError(f"invalid engine_id: {engine_id}")
        self.user_engines[user_id] = engine_id
        self._save()
        return True

    def remove_engine(self, user_id: int) -> bool:
        """사용자의 엔진 설정을 삭제(기본값으로 되돌림)."""
        self.user_engines.pop(user_id, None)
        self._save()
        return True


# 전역 인스턴스
tts_engine_manager = TTSEngineManager()