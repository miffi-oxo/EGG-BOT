"""레이드 포럼 채널 관리 모듈 (로컬 저장 버전).

core/tts_channels.py 와 동일한 패턴: 서버(guild)당 채널 하나만 저장합니다.
"""
import json
import os
import tempfile
from typing import Dict, Optional

# 봇 소스 위치(EGG-BOT/) 기준 절대경로로 고정
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../EGG-BOT
_DATA_DIR = os.path.join(_BASE_DIR, "data")
DATA_FILE = os.path.join(_DATA_DIR, "raid_channel.json")


class RaidChannelManager:
    def __init__(self):
        self.raid_channels: Dict[int, int] = {}  # {guild_id: channel_id}
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
                self.raid_channels = {int(k): int(v) for k, v in data.items()}
            print(f"[레이드채널] 로드 완료: {len(self.raid_channels)}개 서버 (경로: {DATA_FILE})")
        except Exception as e:
            print(f"[레이드채널] 로드 실패: {e}")

    def _save(self):
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=_DATA_DIR)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(self.raid_channels, f, indent=2)
            os.replace(tmp_path, DATA_FILE)
        except Exception as e:
            print(f"[레이드채널] 저장 실패: {e}")
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    # =========================
    # 채널 관리 (서버당 1개만 저장 - set 하면 기존 값을 덮어씀)
    # =========================
    def set_channel(self, guild_id: int, channel_id: int) -> None:
        self.raid_channels[guild_id] = channel_id
        self._save()

    def remove_channel(self, guild_id: int) -> None:
        self.raid_channels.pop(guild_id, None)
        self._save()

    def get_channel(self, guild_id: int) -> Optional[int]:
        return self.raid_channels.get(guild_id)


# 전역 인스턴스
raid_channel_manager = RaidChannelManager()