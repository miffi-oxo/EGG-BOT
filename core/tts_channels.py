"""TTS 채널 관리 모듈 (로컬 저장 버전)"""
import json
import os
import tempfile
from typing import Optional, Dict

# 봇 소스 위치(EGG-BOT/) 기준 절대경로로 고정 -> 실행 시 cwd가 달라져도 항상 같은 파일을 봄
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../EGG-BOT
_DATA_DIR = os.path.join(_BASE_DIR, "data")
DATA_FILE = os.path.join(_DATA_DIR, "tts_channels.json")


class TTSChannelManager:
    def __init__(self):
        self.tts_channels: Dict[int, int] = {}  # {guild_id: channel_id}
        self.override_channels: Dict[int, int] = {}
        self.override_meta: Dict[int, Dict] = {}

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
                self.tts_channels = {
                    int(k): int(v) for k, v in data.items()
                }
            print(f"TTS 채널 로드 완료: {len(self.tts_channels)}개 서버 (경로: {DATA_FILE})")
        except Exception as e:
            print(f"TTS 채널 로드 실패: {e}")

    def _save(self):
        # 원자적 쓰기: 저장 도중 프로세스가 죽어도 기존 파일이 깨지지 않음
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=_DATA_DIR)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(self.tts_channels, f, indent=2)
            os.replace(tmp_path, DATA_FILE)
        except Exception as e:
            print(f"TTS 채널 저장 실패: {e}")
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    # =========================
    # 채널 관리
    # =========================
    async def load_all_channels(self):
        """호환용 (실제는 init에서 로드됨)"""
        return

    async def set_channel(self, guild_id: int, channel_id: int):
        """TTS 채널 설정"""
        try:
            self.tts_channels[guild_id] = channel_id
            self._save()
            return True
        except Exception as e:
            print(f"TTS 채널 설정 오류: {e}")
            return False

    async def remove_channel(self, guild_id: int):
        """TTS 채널 삭제"""
        try:
            self.tts_channels.pop(guild_id, None)
            self._save()
            return True
        except Exception as e:
            print(f"TTS 채널 삭제 오류: {e}")
            return False

    # =========================
    # 임시 채널 (그대로 유지)
    # =========================
    def set_override(self, guild_id: int, channel_id: int, by_user_id: Optional[int] = None) -> None:
        self.override_channels[guild_id] = channel_id
        if by_user_id:
            self.override_meta[guild_id] = {"by": by_user_id}

    def clear_override(self, guild_id: int) -> None:
        self.override_channels.pop(guild_id, None)
        self.override_meta.pop(guild_id, None)

    def clear_all_overrides(self) -> None:
        self.override_channels.clear()
        self.override_meta.clear()

    def get_effective_channel(self, guild_id: int) -> Optional[int]:
        return self.override_channels.get(guild_id) or self.tts_channels.get(guild_id)

    def get_channel(self, guild_id: int):
        return self.tts_channels.get(guild_id)

    def is_tts_channel(self, guild_id: int, channel_id: int) -> bool:
        override = self.override_channels.get(guild_id)
        if override is not None:
            return override == channel_id
        return self.tts_channels.get(guild_id) == channel_id


# 전역 인스턴스
tts_channel_manager = TTSChannelManager()