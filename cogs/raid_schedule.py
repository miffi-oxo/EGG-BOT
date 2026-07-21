"""레이드 일정 관리.

구현된 것: 채널 지정(서버당 1개), 레이드 생성/일정수정/제목수정/삭제,
참가신청/참가변경/참가취소, 대기열(자동 승격 포함), 관리자 강제참여/강제변경/강제취소,
레이드 시작 10분 전 참가자 태그 알림, 시작 30분 후 포스트 자동 닫기+잠그기,
로스트아크 API 캐릭터 조회(레벨/직업/전투력, 입장레벨 검증), "기타" 레이드(비레이드 컨텐츠 모집).
"""
import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import tempfile
from datetime import date, datetime, time, timedelta, timezone

from core.raid_data import (
    load_raid_data,
    DIFF_ORDER,
    DEFAULT_DEALER_SLOTS,
    DEFAULT_SUPPORT_SLOTS,
    get_schedulable_raid_data,
    get_party_slots,
)
from core.raid_channel import raid_channel_manager
from core.config import SUPPORTER_CLASSES
from core.lostark_api import get_character_basic, LostArkAPIError, LostArkCharacterNotFoundError

# 봇 소스 위치(EGG-BOT/) 기준 절대경로로 고정
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../EGG-BOT
_DATA_DIR = os.path.join(_BASE_DIR, "data")
RAIDS_FILE = os.path.join(_DATA_DIR, "raids.json")
FIXED_PARTIES_FILE = os.path.join(_DATA_DIR, "fixed_parties.json")

KST = timezone(timedelta(hours=9))
WEEKDAYS_KO = ["월", "화", "수", "목", "금", "토", "일"]

DATE_RANGE_DAYS = 15  # 오늘 포함 오늘~14일 후

ROLE_LABEL = {"dealer": "딜러", "support": "서포터"}


def role_label(role: str | None) -> str:
    return ROLE_LABEL.get(role, role or "?")


# =========================
# 저장 / 로드 (레이드 모집 게시물 데이터, message_id를 키로 사용)
# =========================
def _load_raids() -> dict:
    os.makedirs(_DATA_DIR, exist_ok=True)
    if not os.path.exists(RAIDS_FILE):
        return {}
    try:
        with open(RAIDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        print(f"[레이드일정] 로드 완료: {len(data)}개 게시물 (경로: {RAIDS_FILE})")
        return data
    except Exception as e:
        print(f"[레이드일정] 로드 실패: {e}")
        return {}


def _save_raids(data: dict) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=_DATA_DIR)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, RAIDS_FILE)
    except Exception as e:
        print(f"[레이드일정] 저장 실패: {e}")
        try:
            os.remove(tmp_path)
        except Exception:
            pass


# =========================
# 저장 / 로드 (고정공격대 설정, guild_id는 각 항목 안에 들어있음 -> 서버별로 필터링해서 사용)
# =========================
def _load_fixed_parties() -> dict:
    os.makedirs(_DATA_DIR, exist_ok=True)
    if not os.path.exists(FIXED_PARTIES_FILE):
        return {}
    try:
        with open(FIXED_PARTIES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        print(f"[고정공격대] 로드 완료: {len(data)}개 (경로: {FIXED_PARTIES_FILE})")
        return data
    except Exception as e:
        print(f"[고정공격대] 로드 실패: {e}")
        return {}


def _save_fixed_parties(data: dict) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=_DATA_DIR)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, FIXED_PARTIES_FILE)
    except Exception as e:
        print(f"[고정공격대] 저장 실패: {e}")
        try:
            os.remove(tmp_path)
        except Exception:
            pass


def _find_application(entry: dict, user_id: int) -> dict | None:
    """user_id가 이 레이드에 신청한 내역을 찾는다. (용병은 user_id가 없어서 대상 아님)"""
    for role in ("dealer", "support"):
        for i, p in enumerate(entry["participants"][role]):
            if not p.get("is_mercenary") and p.get("user_id") == user_id:
                return {"where": "participant", "role": role, "index": i}
    for i, p in enumerate(entry.get("queue", [])):
        if p.get("user_id") == user_id:
            return {"where": "queue", "role": p.get("role"), "index": i}
    return None


def _thread_title(entry: dict) -> str:
    d = date.fromisoformat(entry["date"])
    diff_part = f" {entry['diff']}" if entry.get("diff") else ""
    return (
        f"{entry['date']}({WEEKDAYS_KO[d.weekday()]}) {entry['hour']:02d}:{entry['minute']:02d} "
        f"{entry['raid']}{diff_part} - {entry['title']}"
    )


# =========================
# 선택지(옵션) 빌더
# =========================
def _build_date_options(default_value: str | None = None) -> list[discord.SelectOption]:
    now = datetime.now(KST)
    options = []
    for i in range(DATE_RANGE_DAYS):
        d = (now + timedelta(days=i)).date()
        value = d.isoformat()
        label = f"{value} ({WEEKDAYS_KO[d.weekday()]})"
        options.append(discord.SelectOption(label=label, value=value, default=(value == default_value)))
    return options


def _build_hour_options(default_value: int | None = None) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            label=f"{h:02d}시",
            value=str(h),
            default=(default_value is not None and int(default_value) == h),
        )
        for h in range(24)
    ]


def _build_minute_options(default_value: int | None = None) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            label=f"{m:02d}분",
            value=str(m),
            default=(default_value is not None and int(default_value) == m),
        )
        for m in (0, 10, 20, 30, 40, 50)
    ]


OTHER_RAID_LABEL = "기타"  # /레이드 전용 특수 항목. core/raid_data.py에는 없고 여기서만 취급함 (/클골에는 안 나옴)
OTHER_DEALER_SLOTS = 12
OTHER_SUPPORT_SLOTS = 4
NO_DIFF_VALUE = "__none__"  # 난이도 선택 안 함(더미) 표시용. SelectOption.value는 빈 문자열을 못 써서 이걸로 대체


def _build_raid_options(raid_data, default_value: str | None = None) -> list[discord.SelectOption]:
    raids = sorted(set(r for r, _ in raid_data.keys()))[:24]  # "기타" 자리를 위해 24개까지만
    options = [
        discord.SelectOption(label=r, value=r, default=(r == default_value))
        for r in raids
    ]
    options.append(
        discord.SelectOption(
            label=OTHER_RAID_LABEL,
            value=OTHER_RAID_LABEL,
            description="레이드 외 컨텐츠 모집",
            default=(default_value == OTHER_RAID_LABEL),
        )
    )
    return options


def _build_diff_options(raid_data, default_value: str | None = None) -> list[discord.SelectOption]:
    diffs = sorted(set(d for _, d in raid_data.keys()), key=lambda x: DIFF_ORDER.get(x, 999))[:25]
    return [
        discord.SelectOption(label=d, value=d, default=(d == default_value))
        for d in diffs
    ]


# =========================
# 모달: 날짜 / 시 / 분 / 레이드 / 난이도 선택 (생성 + 수정 겸용)
# =========================
class RaidCreateModal(discord.ui.Modal):
    def __init__(
        self,
        cog: "RaidScheduleCog",
        title_text: str,
        *,
        description_text: str = "",
        defaults: dict | None = None,
        edit_raid_id: str | None = None,
        origin_message: discord.Message | None = None,
    ):
        super().__init__(title="레이드 일정 등록" if not edit_raid_id else "레이드 일정 수정")
        self.cog = cog
        self.title_text = title_text
        self.description_text = description_text
        self.edit_raid_id = edit_raid_id
        self.origin_message = origin_message
        defaults = defaults or {}

        self.date_select = discord.ui.Select(
            placeholder="날짜 선택",
            options=_build_date_options(defaults.get("date")),
        )
        self.hour_select = discord.ui.Select(
            placeholder="시 선택 (00-23)",
            options=_build_hour_options(defaults.get("hour")),
        )
        self.minute_select = discord.ui.Select(
            placeholder="분 선택 (10분 단위)",
            options=_build_minute_options(defaults.get("minute")),
        )
        schedulable = get_schedulable_raid_data(cog.raid_data)
        self.raid_select = discord.ui.Select(
            placeholder="레이드 선택",
            options=_build_raid_options(schedulable, defaults.get("raid")),
        )
        diff_default = defaults.get("diff")
        diff_options = [
            discord.SelectOption(
                label=OTHER_RAID_LABEL,
                value=NO_DIFF_VALUE,
                description="선택 안 함 (레이드가 '기타'일 때만 사용)",
                default=(diff_default in (None, "", NO_DIFF_VALUE)),
            )
        ]
        diff_options += _build_diff_options(schedulable, diff_default)
        self.diff_select = discord.ui.Select(
            placeholder="난이도 선택 (레이드가 '기타'면 생략 가능)",
            options=diff_options[:25],
        )

        self.add_item(discord.ui.Label(text="날짜", component=self.date_select))
        self.add_item(discord.ui.Label(text="시", component=self.hour_select))
        self.add_item(discord.ui.Label(text="분", component=self.minute_select))
        self.add_item(discord.ui.Label(text="레이드", component=self.raid_select))
        self.add_item(discord.ui.Label(text="난이도", component=self.diff_select))

    async def on_submit(self, interaction: discord.Interaction):
        if self.origin_message is not None:
            try:
                await self.origin_message.edit(view=None)
            except Exception:
                pass

        date_str = self.date_select.values[0]
        hour = int(self.hour_select.values[0])
        minute = int(self.minute_select.values[0])
        raid = self.raid_select.values[0]
        diff = self.diff_select.values[0] if self.diff_select.values else ""
        if diff == NO_DIFF_VALUE:
            diff = ""

        defaults = {"date": date_str, "hour": hour, "minute": minute, "raid": raid, "diff": diff}

        # 수정사항 2: 이미 지난 시간이면 생성/수정 불가
        target_dt = datetime.combine(date.fromisoformat(date_str), time(hour=hour, minute=minute), tzinfo=KST)
        if target_dt <= datetime.now(KST):
            retry_view = _RetryView(
                self.cog, self.title_text, defaults,
                description_text=self.description_text, edit_raid_id=self.edit_raid_id,
            )
            await interaction.response.send_message(
                "❌ 이미 지난 시간으로는 레이드 일정을 등록할 수 없어요. 다른 날짜/시간을 선택해주세요.",
                view=retry_view,
                ephemeral=True,
            )
            return

        if raid != OTHER_RAID_LABEL:
            # "기타"가 아니면 난이도는 필수, 클골에 등록된 실제 조합이어야 함
            key = (raid, diff)
            if not diff or diff == "싱글" or key not in get_schedulable_raid_data(self.cog.raid_data):
                retry_view = _RetryView(
                    self.cog, self.title_text, defaults,
                    description_text=self.description_text, edit_raid_id=self.edit_raid_id,
                )
                # 수정사항 1: "/클골추가로 먼저 등록하거나" 문구 제거
                await interaction.response.send_message(
                    f"❌ **{raid} - {diff or '(난이도 미선택)'}**: 존재하지 않는 조합입니다.\n"
                    f"아래 버튼으로 다른 조합을 다시 선택해주세요.",
                    view=retry_view,
                    ephemeral=True,
                )
                return

        if self.edit_raid_id:
            await self.cog.update_raid_post(interaction, self.edit_raid_id, date_str, hour, minute, raid, diff)
        else:
            view = _DescriptionStepView(self.cog, self.title_text, date_str, hour, minute, raid, diff)
            await interaction.response.send_message(
                "레이드 일정이 확인됐어요. 설명을 추가하시겠어요? (안 넣어도 괜찮아요)",
                view=view,
                ephemeral=True,
            )


class _DescriptionStepView(discord.ui.View):
    """레이드 생성 2단계: 설명을 쓸지, 그냥 게시할지 선택."""

    def __init__(
        self,
        cog: "RaidScheduleCog",
        title_text: str,
        date_str: str,
        hour: int,
        minute: int,
        raid: str,
        diff: str,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.title_text = title_text
        self.date_str = date_str
        self.hour = hour
        self.minute = minute
        self.raid = raid
        self.diff = diff

    @discord.ui.button(label="설명 작성하기", style=discord.ButtonStyle.blurple)
    async def write_description(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = RaidDescriptionModal(
            self.cog, self.title_text, self.date_str, self.hour, self.minute, self.raid, self.diff,
            origin_message=interaction.message,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="설명 없이 게시", style=discord.ButtonStyle.gray)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="게시물을 생성하고 있어요...", view=None)
        await self.cog.create_raid_post(
            interaction, self.title_text, "", self.date_str, self.hour, self.minute, self.raid, self.diff
        )


class RaidDescriptionModal(discord.ui.Modal):
    """레이드 생성 2단계: 줄바꿈 가능한 긴 설명 입력."""

    def __init__(
        self,
        cog: "RaidScheduleCog",
        title_text: str,
        date_str: str,
        hour: int,
        minute: int,
        raid: str,
        diff: str,
        *,
        origin_message: discord.Message | None = None,
    ):
        super().__init__(title="레이드 설명 작성")
        self.cog = cog
        self.title_text = title_text
        self.date_str = date_str
        self.hour = hour
        self.minute = minute
        self.raid = raid
        self.diff = diff
        self.origin_message = origin_message
        self.description_input = discord.ui.TextInput(
            label="설명 (줄바꿈 가능)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
        )
        self.add_item(self.description_input)

    async def on_submit(self, interaction: discord.Interaction):
        if self.origin_message is not None:
            try:
                await self.origin_message.edit(view=None)
            except Exception:
                pass

        description_text = self.description_input.value.strip()
        await self.cog.create_raid_post(
            interaction, self.title_text, description_text, self.date_str, self.hour, self.minute, self.raid, self.diff
        )


class _RetryView(discord.ui.View):
    """조합이 잘못됐거나 과거 시간일 때, 입력했던 값을 유지한 채 모달을 다시 띄우는 버튼."""

    def __init__(
        self,
        cog: "RaidScheduleCog",
        title_text: str,
        defaults: dict,
        *,
        description_text: str = "",
        edit_raid_id: str | None = None,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.title_text = title_text
        self.defaults = defaults
        self.description_text = description_text
        self.edit_raid_id = edit_raid_id

    @discord.ui.button(label="다시 입력", style=discord.ButtonStyle.blurple)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = RaidCreateModal(
            self.cog, self.title_text,
            description_text=self.description_text,
            defaults=self.defaults, edit_raid_id=self.edit_raid_id,
            origin_message=interaction.message,
        )
        await interaction.response.send_modal(modal)


# =========================
# 모달: 날짜/시/분만 수정 (레이드/난이도/정원은 안 건드림)
# =========================
class RaidRescheduleModal(discord.ui.Modal):
    def __init__(
        self,
        cog: "RaidScheduleCog",
        raid_id: str,
        defaults: dict,
        origin_message: discord.Message | None = None,
    ):
        super().__init__(title="레이드 일정 수정")
        self.cog = cog
        self.raid_id = raid_id
        self.origin_message = origin_message

        self.date_select = discord.ui.Select(
            placeholder="날짜 선택", options=_build_date_options(defaults.get("date"))
        )
        self.hour_select = discord.ui.Select(
            placeholder="시 선택 (00-23)", options=_build_hour_options(defaults.get("hour"))
        )
        self.minute_select = discord.ui.Select(
            placeholder="분 선택 (10분 단위)", options=_build_minute_options(defaults.get("minute"))
        )

        self.add_item(discord.ui.Label(text="날짜", component=self.date_select))
        self.add_item(discord.ui.Label(text="시", component=self.hour_select))
        self.add_item(discord.ui.Label(text="분", component=self.minute_select))

    async def on_submit(self, interaction: discord.Interaction):
        if self.origin_message is not None:
            try:
                await self.origin_message.edit(view=None)
            except Exception:
                pass

        date_str = self.date_select.values[0]
        hour = int(self.hour_select.values[0])
        minute = int(self.minute_select.values[0])

        target_dt = datetime.combine(date.fromisoformat(date_str), time(hour=hour, minute=minute), tzinfo=KST)
        if target_dt <= datetime.now(KST):
            retry_view = _RescheduleRetryView(
                self.cog, self.raid_id,
                {"date": date_str, "hour": hour, "minute": minute},
            )
            await interaction.response.send_message(
                "❌ 이미 지난 시간으로는 등록할 수 없어요. 다른 날짜/시간을 선택해주세요.",
                view=retry_view,
                ephemeral=True,
            )
            return

        await self.cog.update_raid_schedule_only(interaction, self.raid_id, date_str, hour, minute)


class _RescheduleRetryView(discord.ui.View):
    def __init__(self, cog: "RaidScheduleCog", raid_id: str, defaults: dict):
        super().__init__(timeout=300)
        self.cog = cog
        self.raid_id = raid_id
        self.defaults = defaults

    @discord.ui.button(label="다시 입력", style=discord.ButtonStyle.blurple)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = RaidRescheduleModal(self.cog, self.raid_id, self.defaults, origin_message=interaction.message)
        await interaction.response.send_modal(modal)


# =========================
# 모달: 제목만 수정
# =========================
class TitleEditModal(discord.ui.Modal):
    def __init__(self, cog: "RaidScheduleCog", raid_id: str):
        super().__init__(title="제목/설명 수정")
        self.cog = cog
        self.raid_id = raid_id
        entry = cog.raids.get(raid_id) or {}
        self.title_input = discord.ui.TextInput(
            label="제목", default=entry.get("title", ""), max_length=100
        )
        self.description_input = discord.ui.TextInput(
            label="설명 (줄바꿈 가능)", style=discord.TextStyle.paragraph, required=False, max_length=1000,
            default=entry.get("description", ""),
        )
        self.add_item(self.title_input)
        self.add_item(self.description_input)

    async def on_submit(self, interaction: discord.Interaction):
        entry = self.cog.raids.get(self.raid_id)
        if entry is None:
            await interaction.response.send_message("레이드 정보를 찾을 수 없어요.", ephemeral=True)
            return
        entry["title"] = self.title_input.value.strip()
        entry["description"] = self.description_input.value.strip()
        self.cog.raids[self.raid_id] = entry
        _save_raids(self.cog.raids)
        await self.cog._rename_thread(entry)
        await self.cog._update_post_embed_by_id(self.raid_id)
        await interaction.response.send_message("✅ 제목/설명이 수정됐어요.", ephemeral=True)


# =========================
# 모달: 캐릭터 닉네임 입력 (참가신청 / 닉네임변경 / 강제참여 공용)
# =========================
class CharacterNameModal(discord.ui.Modal):
    def __init__(
        self,
        cog: "RaidScheduleCog",
        raid_id: str,
        *,
        mode: str,  # "apply" | "rename" | "force" | "force_rename" | "fixed_add"
        member: discord.Member | None = None,
        is_mercenary: bool = False,
        target_loc: dict | None = None,
        origin_message: discord.Message | None = None,
        panel_interaction: discord.Interaction | None = None,
        panel_kind: str = "build",
    ):
        super().__init__(title="캐릭터 닉네임 입력")
        self.cog = cog
        self.raid_id = raid_id
        self.mode = mode
        self.member = member
        self.is_mercenary = is_mercenary
        self.target_loc = target_loc
        self.origin_message = origin_message
        self.panel_interaction = panel_interaction
        self.panel_kind = panel_kind
        self.name_input = discord.ui.TextInput(
            label="로스트아크 캐릭터 닉네임", max_length=16
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        character = self.name_input.value.strip()
        if self.mode in ("fixed_add", "fixed_rename"):
            entry = self.cog.fixed_parties.get(self.raid_id)
        else:
            entry = self.cog.raids.get(self.raid_id)
        if entry is None:
            await interaction.response.send_message("정보를 찾을 수 없어요.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # 이 모달을 띄운 원래 선택 메시지(멤버 검색 / 강제변경 대상 선택)가 있었다면
        # 여기서 버튼·셀렉트를 없애서 더 이상 조작 못 하게 정리함
        if self.origin_message is not None:
            try:
                await self.origin_message.edit(view=None)
            except Exception:
                pass

        try:
            info = await get_character_basic(character)
        except LostArkCharacterNotFoundError as e:
            # 수정사항 1: 존재하지 않는 캐릭터는 강제참여/강제변경이라도 무조건 거부
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        except LostArkAPIError as e:
            if self.mode in ("force", "force_rename", "fixed_add", "fixed_rename"):
                # 캐릭터는 존재할 텐데 API가 일시적으로 응답을 못 준 경우 등은
                # 관리자 재량으로 계속 진행
                info = {"level": None, "class_name": None, "combat_power": None}
                await interaction.followup.send(f"⚠️ 캐릭터 조회에 실패했지만 그대로 진행할게요: {e}", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ {e}", ephemeral=True)
                return
        else:
            min_level = entry.get("min_level") or 0
            # 수정사항 4: 강제참여도 입장레벨 미달이면 거부 (관리자 재량 예외 없음)
            # 고정공격대 로스터도 실제 신청과 동일하게 입장레벨을 검증함
            if self.mode in ("apply", "force", "fixed_add", "fixed_rename") and min_level and info["level"] < min_level:
                await interaction.followup.send(
                    f"❌ 입장 레벨이 부족해요. (필요: {min_level:,.2f} / 현재: {info['level']:,.2f})",
                    ephemeral=True,
                )
                return

        if self.mode == "apply":
            await self.cog.handle_apply(interaction, self.raid_id, character, info)
        elif self.mode == "rename":
            await self.cog.handle_rename(interaction, self.raid_id, character, info)
        elif self.mode == "force_rename":
            class_name = info.get("class_name")
            if class_name in SUPPORTER_CLASSES:
                view = RoleChoiceView(
                    self.cog, self.raid_id, character, info, mode="force_rename", target_loc=self.target_loc
                )
                await interaction.followup.send(f"**{character}** 님의 역할을 선택해주세요.", view=view, ephemeral=True)
            else:
                await self.cog.finalize_force_rename(interaction, self.raid_id, self.target_loc, character, info, "dealer")
        elif self.mode == "force":
            class_name = info.get("class_name")
            if class_name in SUPPORTER_CLASSES:
                view = RoleChoiceView(self.cog, self.raid_id, character, info, mode="force", member=self.member)
                await interaction.followup.send(f"**{character}** 님의 역할을 선택해주세요.", view=view, ephemeral=True)
            else:
                # 겸용 직업이 아니면(또는 조회 실패로 직업을 모르면) 바로 딜러로 강제참여
                await self.cog.finalize_force_join(interaction, self.raid_id, character, info, "dealer", self.member)
        elif self.mode == "fixed_add":
            class_name = info.get("class_name")
            if class_name in SUPPORTER_CLASSES:
                view = RoleChoiceView(
                    self.cog, self.raid_id, character, info, mode="fixed_add",
                    member=self.member, panel_interaction=self.panel_interaction, panel_kind=self.panel_kind,
                )
                await interaction.followup.send(f"**{character}** 님의 역할을 선택해주세요.", view=view, ephemeral=True)
            else:
                await self.cog.finalize_fixed_add(
                    interaction, self.raid_id, character, info, "dealer", self.member,
                    panel_interaction=self.panel_interaction, panel_kind=self.panel_kind,
                )
        elif self.mode == "fixed_rename":
            class_name = info.get("class_name")
            if class_name in SUPPORTER_CLASSES:
                view = RoleChoiceView(
                    self.cog, self.raid_id, character, info, mode="fixed_rename",
                    target_loc=self.target_loc, panel_interaction=self.panel_interaction, panel_kind=self.panel_kind,
                )
                await interaction.followup.send(f"**{character}** 님의 역할을 선택해주세요.", view=view, ephemeral=True)
            else:
                await self.cog.finalize_fixed_rename(
                    interaction, self.raid_id, self.target_loc, character, info, "dealer",
                    panel_interaction=self.panel_interaction, panel_kind=self.panel_kind,
                )


# =========================
# 딜러/서포터 겸용 직업일 때 역할 선택 버튼 (참가신청, 강제참여에서 사용)
# =========================
class RoleChoiceView(discord.ui.View):
    def __init__(
        self,
        cog: "RaidScheduleCog",
        raid_id: str,
        character: str,
        info: dict,
        *,
        mode: str,
        member: discord.Member | None = None,
        target_loc: dict | None = None,
        panel_interaction: discord.Interaction | None = None,
        panel_kind: str = "build",
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.raid_id = raid_id
        self.character = character
        self.info = info
        self.mode = mode
        self.member = member
        self.target_loc = target_loc
        self.panel_interaction = panel_interaction
        self.panel_kind = panel_kind

    @discord.ui.button(label="딜러", style=discord.ButtonStyle.blurple)
    async def as_dealer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, "dealer")

    @discord.ui.button(label="서포터", style=discord.ButtonStyle.blurple)
    async def as_support(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, "support")

    async def _finish(self, interaction: discord.Interaction, role: str):
        if self.mode == "apply":
            await self.cog.finalize_apply(interaction, self.raid_id, self.character, self.info, role)
        elif self.mode == "force":
            await self.cog.finalize_force_join(interaction, self.raid_id, self.character, self.info, role, self.member)
        elif self.mode == "rename":
            await self.cog.finalize_rename(interaction, self.raid_id, self.character, self.info, role)
        elif self.mode == "force_rename":
            await self.cog.finalize_force_rename(
                interaction, self.raid_id, self.target_loc, self.character, self.info, role
            )
        elif self.mode == "fixed_add":
            await self.cog.finalize_fixed_add(
                interaction, self.raid_id, self.character, self.info, role, self.member,
                panel_interaction=self.panel_interaction, panel_kind=self.panel_kind,
            )
        elif self.mode == "fixed_rename":
            await self.cog.finalize_fixed_rename(
                interaction, self.raid_id, self.target_loc, self.character, self.info, role,
                panel_interaction=self.panel_interaction, panel_kind=self.panel_kind,
            )


# =========================
# 관리 패널 (관리자 / 작성자 전용, 관리 버튼 클릭 시 표시)
# =========================
class ManagePanelView(discord.ui.View):
    def __init__(self, cog: "RaidScheduleCog", raid_id: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.raid_id = raid_id

    @discord.ui.button(label="제목/설명수정", style=discord.ButtonStyle.blurple)
    async def edit_title(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = TitleEditModal(self.cog, self.raid_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="일정수정", style=discord.ButtonStyle.blurple)
    async def edit_schedule(self, interaction: discord.Interaction, button: discord.ui.Button):
        entry = self.cog.raids.get(self.raid_id)
        if entry is None:
            await interaction.response.send_message("레이드 정보를 찾을 수 없어요.", ephemeral=True)
            return
        modal = RaidRescheduleModal(
            self.cog,
            self.raid_id,
            {"date": entry["date"], "hour": entry["hour"], "minute": entry["minute"]},
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="강제참여", style=discord.ButtonStyle.gray)
    async def force_join(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = ForceJoinPickView(self.cog, self.raid_id)
        await interaction.response.send_message(
            "강제 참여시킬 멤버를 아래에서 검색하거나, 서버에 없는 사람은 '용병으로 추가'를 눌러주세요.",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="강제변경", style=discord.ButtonStyle.gray)
    async def force_rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        entry = self.cog.raids.get(self.raid_id)
        if entry is None:
            await interaction.response.send_message("레이드 정보를 찾을 수 없어요.", ephemeral=True)
            return

        options = _build_applicant_options(entry, interaction.guild)
        if not options:
            await interaction.response.send_message("캐릭터를 변경할 신청자가 없어요.", ephemeral=True)
            return

        view = ForceRenamePickView(self.cog, self.raid_id, options[:25])
        await interaction.response.send_message("캐릭터를 변경할 신청자를 선택해주세요.", view=view, ephemeral=True)

    @discord.ui.button(label="강제취소", style=discord.ButtonStyle.gray)
    async def force_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        entry = self.cog.raids.get(self.raid_id)
        if entry is None:
            await interaction.response.send_message("레이드 정보를 찾을 수 없어요.", ephemeral=True)
            return

        options = _build_applicant_options(entry, interaction.guild)
        if not options:
            await interaction.response.send_message("취소시킬 신청자가 없어요.", ephemeral=True)
            return

        view = ForceCancelPickView(self.cog, self.raid_id, options[:25])
        await interaction.response.send_message("강제로 취소시킬 신청자를 선택해주세요.", view=view, ephemeral=True)

    @discord.ui.button(label="삭제", style=discord.ButtonStyle.red)
    async def delete_post(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = DeleteConfirmView(self.cog, self.raid_id)
        await interaction.response.send_message(
            "⚠️ 정말로 이 레이드 게시물을 삭제하시겠습니까? 되돌릴 수 없어요.", view=view, ephemeral=True
        )


class DeleteConfirmView(discord.ui.View):
    def __init__(self, cog: "RaidScheduleCog", raid_id: str):
        super().__init__(timeout=60)
        self.cog = cog
        self.raid_id = raid_id

    @discord.ui.button(label="확인 (삭제)", style=discord.ButtonStyle.red)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        entry = self.cog.raids.pop(self.raid_id, None)
        _save_raids(self.cog.raids)
        if entry is None:
            await interaction.response.edit_message(content="이미 삭제된 레이드예요.", view=None)
            return
        await interaction.response.edit_message(content="🗑️ 레이드 게시물을 삭제했어요.", view=None)
        try:
            channel = self.cog.bot.get_channel(entry["channel_id"]) or await self.cog.bot.fetch_channel(entry["channel_id"])
            await channel.delete()
        except Exception as e:
            print(f"[레이드일정] 스레드 삭제 실패: {e}")

    @discord.ui.button(label="취소", style=discord.ButtonStyle.gray)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="삭제를 취소했어요.", view=None)


def _describe_applicant(guild: discord.Guild | None, p: dict) -> str:
    if p.get("is_mercenary"):
        return "용병"
    uid = p.get("user_id")
    member = guild.get_member(uid) if (guild and uid) else None
    if member:
        return member.display_name
    return f"유저 ID {uid}" if uid else "알 수 없음"


def _build_applicant_options(entry: dict, guild: discord.Guild | None) -> list[discord.SelectOption]:
    """강제취소/강제변경에서 공용으로 쓰는, 현재 참가자+대기열 선택지 빌더."""
    options: list[discord.SelectOption] = []
    for role in ("dealer", "support"):
        for i, p in enumerate(entry["participants"][role]):
            options.append(discord.SelectOption(
                label=f"[{role_label(role)}] {p['character']}",
                description=_describe_applicant(guild, p),
                value=f"participant:{role}:{i}",
            ))
    for i, p in enumerate(entry.get("queue", [])):
        options.append(discord.SelectOption(
            label=f"[대기 - {role_label(p.get('role'))}] {p['character']}",
            description=_describe_applicant(guild, p),
            value=f"queue:{i}",
        ))
    return options


class ForceRenamePickView(discord.ui.View):
    def __init__(self, cog: "RaidScheduleCog", raid_id: str, options: list[discord.SelectOption]):
        super().__init__(timeout=180)
        self.cog = cog
        self.raid_id = raid_id
        select = discord.ui.Select(placeholder="캐릭터를 변경할 신청자 선택", options=options)
        select.callback = self._on_select
        self.add_item(select)
        self.select = select

    async def _on_select(self, interaction: discord.Interaction):
        value = self.select.values[0]
        parts = value.split(":")
        if parts[0] == "participant":
            target_loc = {"where": "participant", "role": parts[1], "index": int(parts[2])}
        else:
            target_loc = {"where": "queue", "index": int(parts[1])}
        modal = CharacterNameModal(
            self.cog, self.raid_id, mode="force_rename", target_loc=target_loc, origin_message=interaction.message
        )
        await interaction.response.send_modal(modal)


class ForceCancelPickView(discord.ui.View):
    def __init__(self, cog: "RaidScheduleCog", raid_id: str, options: list[discord.SelectOption]):
        super().__init__(timeout=180)
        self.cog = cog
        self.raid_id = raid_id
        select = discord.ui.Select(placeholder="취소시킬 신청자 선택", options=options)
        select.callback = self._on_select
        self.add_item(select)
        self.select = select

    async def _on_select(self, interaction: discord.Interaction):
        value = self.select.values[0]
        parts = value.split(":")
        entry = self.cog.raids.get(self.raid_id)
        if entry is None:
            await interaction.response.edit_message(content="레이드 정보를 찾을 수 없어요.", view=None)
            return

        if parts[0] == "participant":
            role, idx = parts[1], int(parts[2])
            lst = entry["participants"][role]
            if idx >= len(lst):
                await interaction.response.edit_message(content="이미 처리된 신청이에요. 다시 시도해주세요.", view=None)
                return
            removed = lst.pop(idx)
            self.cog._promote_from_queue(entry, role)
        else:
            idx = int(parts[1])
            queue = entry["queue"]
            if idx >= len(queue):
                await interaction.response.edit_message(content="이미 처리된 신청이에요. 다시 시도해주세요.", view=None)
                return
            removed = queue.pop(idx)

        self.cog.raids[self.raid_id] = entry
        _save_raids(self.cog.raids)
        await self.cog._update_post_embed_by_id(self.raid_id)
        await interaction.response.edit_message(
            content=f"✅ **{removed['character']}** 신청을 강제로 취소시켰어요.", view=None
        )


class ForceJoinPickView(discord.ui.View):
    def __init__(self, cog: "RaidScheduleCog", raid_id: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.raid_id = raid_id

        self.member_select = discord.ui.UserSelect(placeholder="멤버 검색", min_values=1, max_values=1)
        self.member_select.callback = self._on_member_selected
        self.add_item(self.member_select)

    async def _on_member_selected(self, interaction: discord.Interaction):
        member = self.member_select.values[0]
        modal = CharacterNameModal(
            self.cog, self.raid_id, mode="force", member=member, origin_message=interaction.message
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="용병으로 추가", style=discord.ButtonStyle.gray)
    async def add_mercenary(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = CharacterNameModal(
            self.cog, self.raid_id, mode="force", member=None, is_mercenary=True, origin_message=interaction.message
        )
        await interaction.response.send_modal(modal)


# =========================
# 게시물에 붙는 버튼 (영속 View - 봇 재구동 후에도 동작하도록 custom_id 고정)
# =========================
class RaidPostView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def _get_cog(self, interaction: discord.Interaction) -> "RaidScheduleCog | None":
        return interaction.client.get_cog("RaidScheduleCog")

    @discord.ui.button(label="참가신청", style=discord.ButtonStyle.green, custom_id="raid_apply")
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self._get_cog(interaction)
        raid_id = str(interaction.message.id)
        entry = cog.raids.get(raid_id) if cog else None
        if not cog or entry is None:
            await interaction.response.send_message("레이드 정보를 찾을 수 없어요.", ephemeral=True)
            return

        if _find_application(entry, interaction.user.id) is not None:
            await interaction.response.send_message(
                "이미 이 레이드에 신청하셨어요. 캐릭터를 변경하고 싶으면 참가변경을 눌러주세요.", ephemeral=True
            )
            return

        modal = CharacterNameModal(cog, raid_id, mode="apply")
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="참가변경", style=discord.ButtonStyle.blurple, custom_id="raid_rename")
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self._get_cog(interaction)
        raid_id = str(interaction.message.id)
        entry = cog.raids.get(raid_id) if cog else None
        if not cog or entry is None:
            await interaction.response.send_message("레이드 정보를 찾을 수 없어요.", ephemeral=True)
            return
        if _find_application(entry, interaction.user.id) is None:
            await interaction.response.send_message(
                "먼저 참가신청을 해주셔야 캐릭터를 변경할 수 있어요.", ephemeral=True
            )
            return
        modal = CharacterNameModal(cog, raid_id, mode="rename")
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="참가취소", style=discord.ButtonStyle.red, custom_id="raid_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self._get_cog(interaction)
        raid_id = str(interaction.message.id)
        entry = cog.raids.get(raid_id) if cog else None
        if not cog or entry is None:
            await interaction.response.send_message("레이드 정보를 찾을 수 없어요.", ephemeral=True)
            return
        await cog.handle_cancel(interaction, raid_id)

    @discord.ui.button(label="대기열 명단", style=discord.ButtonStyle.gray, custom_id="raid_queue_list")
    async def queue_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self._get_cog(interaction)
        raid_id = str(interaction.message.id)
        entry = cog.raids.get(raid_id) if cog else None
        if not cog or entry is None:
            await interaction.response.send_message("레이드 정보를 찾을 수 없어요.", ephemeral=True)
            return

        queue = entry.get("queue", [])
        if not queue:
            await interaction.response.send_message("대기열이 비어있어요.", ephemeral=True)
            return

        lines = []
        for i, p in enumerate(queue, 1):
            who = "용병" if p.get("is_mercenary") else f"<@{p['user_id']}>"
            lines.append(f"{i}. **{p['character']}** ({role_label(p.get('role'))}) - {who}")

        embed = discord.Embed(title="⏳ 대기열 명단", description="\n".join(lines), color=discord.Color.orange())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="⚙️ 관리", style=discord.ButtonStyle.gray, custom_id="raid_manage")
    async def manage(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self._get_cog(interaction)
        raid_id = str(interaction.message.id)
        entry = cog.raids.get(raid_id) if cog else None
        if not cog or entry is None:
            await interaction.response.send_message("레이드 정보를 찾을 수 없어요.", ephemeral=True)
            return

        is_creator = interaction.user.id == entry.get("creator_id")
        is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
        if not (is_creator or is_admin):
            await interaction.response.send_message(
                "이 레이드를 관리할 권한이 없어요. (서버 관리자 또는 작성자만 가능해요)", ephemeral=True
            )
            return

        view = ManagePanelView(cog, raid_id)
        await interaction.response.send_message("이 레이드를 관리해요. (나만 보여요)", view=view, ephemeral=True)


# =========================
# 고정공격대: 요일/시/분/레이드/난이도 모달 (날짜 대신 요일을 고름)
# =========================
def _build_weekday_options(default_value: int | None = None) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(label=f"매주 {WEEKDAYS_KO[i]}요일", value=str(i), default=(default_value == i))
        for i in range(7)
    ]


def _next_weekday_date(weekday: int, from_date: date | None = None) -> date:
    """오늘부터 가장 가까운(오늘 포함) 해당 요일의 날짜를 반환."""
    base = from_date or datetime.now(KST).date()
    days_ahead = (weekday - base.weekday()) % 7
    return base + timedelta(days=days_ahead)


class FixedPartyCreateModal(discord.ui.Modal):
    def __init__(
        self,
        cog: "RaidScheduleCog",
        title_text: str,
        *,
        defaults: dict | None = None,
        edit_fixed_id: str | None = None,
        origin_message: discord.Message | None = None,
    ):
        super().__init__(title="고정공격대 등록" if not edit_fixed_id else "고정공격대 일정 수정")
        self.cog = cog
        self.title_text = title_text
        self.edit_fixed_id = edit_fixed_id
        self.origin_message = origin_message
        defaults = defaults or {}

        schedulable = get_schedulable_raid_data(cog.raid_data)
        self.weekday_select = discord.ui.Select(
            placeholder="요일 선택", options=_build_weekday_options(defaults.get("weekday")),
        )
        self.hour_select = discord.ui.Select(
            placeholder="시 선택 (00-23)", options=_build_hour_options(defaults.get("hour")),
        )
        self.minute_select = discord.ui.Select(
            placeholder="분 선택 (10분 단위)", options=_build_minute_options(defaults.get("minute")),
        )
        self.raid_select = discord.ui.Select(
            placeholder="레이드 선택", options=_build_raid_options(schedulable, defaults.get("raid")),
        )
        diff_default = defaults.get("diff")
        diff_options = [
            discord.SelectOption(
                label=OTHER_RAID_LABEL,
                value=NO_DIFF_VALUE,
                description="선택 안 함 (레이드가 '기타'일 때만 사용)",
                default=(diff_default in (None, "", NO_DIFF_VALUE)),
            )
        ]
        diff_options += _build_diff_options(schedulable, diff_default)
        self.diff_select = discord.ui.Select(placeholder="난이도 선택 (레이드가 '기타'면 생략 가능)", options=diff_options[:25])

        self.add_item(discord.ui.Label(text="요일", component=self.weekday_select))
        self.add_item(discord.ui.Label(text="시", component=self.hour_select))
        self.add_item(discord.ui.Label(text="분", component=self.minute_select))
        self.add_item(discord.ui.Label(text="레이드", component=self.raid_select))
        self.add_item(discord.ui.Label(text="난이도", component=self.diff_select))

    async def on_submit(self, interaction: discord.Interaction):
        if self.origin_message is not None:
            try:
                await self.origin_message.edit(view=None)
            except Exception:
                pass

        weekday = int(self.weekday_select.values[0])
        hour = int(self.hour_select.values[0])
        minute = int(self.minute_select.values[0])
        raid = self.raid_select.values[0]
        diff = self.diff_select.values[0] if self.diff_select.values else ""
        if diff == NO_DIFF_VALUE:
            diff = ""

        defaults = {"weekday": weekday, "hour": hour, "minute": minute, "raid": raid, "diff": diff}

        if raid != OTHER_RAID_LABEL:
            key = (raid, diff)
            if not diff or diff == "싱글" or key not in get_schedulable_raid_data(self.cog.raid_data):
                retry_view = _FixedRetryView(self.cog, self.title_text, defaults, edit_fixed_id=self.edit_fixed_id)
                await interaction.response.send_message(
                    f"❌ **{raid} - {diff or '(난이도 미선택)'}**: 존재하지 않는 조합입니다.\n"
                    f"아래 버튼으로 다른 조합을 다시 선택해주세요.",
                    view=retry_view,
                    ephemeral=True,
                )
                return

        if self.edit_fixed_id:
            await self.cog.update_fixed_party_schedule(interaction, self.edit_fixed_id, weekday, hour, minute, raid, diff)
        else:
            # 설명/멤버는 생성 직후 별도 단계에서 채움 -> 우선 빈 상태로 등록하고 로스터 구성 패널을 보여줌
            await self.cog.create_fixed_party_entry(interaction, self.title_text, weekday, hour, minute, raid, diff)


class _FixedRetryView(discord.ui.View):
    def __init__(self, cog: "RaidScheduleCog", title_text: str, defaults: dict, *, edit_fixed_id: str | None = None):
        super().__init__(timeout=300)
        self.cog = cog
        self.title_text = title_text
        self.defaults = defaults
        self.edit_fixed_id = edit_fixed_id

    @discord.ui.button(label="다시 입력", style=discord.ButtonStyle.blurple)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = FixedPartyCreateModal(
            self.cog, self.title_text, defaults=self.defaults,
            edit_fixed_id=self.edit_fixed_id, origin_message=interaction.message,
        )
        await interaction.response.send_modal(modal)


# =========================
# 고정공격대 생성 2단계: 멤버(로스터) 구성
# =========================
def _fixed_roster_summary_text(entry: dict, *, footer: str | None = None) -> str:
    diff_part = f" {entry['diff']}" if entry.get("diff") else ""
    lines = [
        f"**{entry['title']}** ({entry['raid']}{diff_part}, 매주 {WEEKDAYS_KO[entry['weekday']]}요일 "
        f"{entry['hour']:02d}:{entry['minute']:02d})",
        f"⚔️ 딜러 ({len(entry['roster']['dealer'])}/{entry['dealer_slots']}) · "
        f"🛡️ 서포터 ({len(entry['roster']['support'])}/{entry['support_slots']})",
    ]
    for role in ("dealer", "support"):
        for p in entry["roster"][role]:
            who = "용병" if p.get("is_mercenary") else f"<@{p['user_id']}>"
            lines.append(f"[{role_label(role)}] {p['character']} - {who}")
    if entry.get("min_level"):
        lines.append(f"🔒 입장레벨 {entry['min_level']:,.2f}")
    if footer is None:
        footer = "\n멤버를 계속 추가하거나, 다 됐으면 '다음: 설명 작성'을 눌러주세요."
    lines.append(footer)
    return "\n".join(lines)


class FixedRosterBuildView(discord.ui.View):
    def __init__(self, cog: "RaidScheduleCog", fixed_id: str, *, panel_interaction: discord.Interaction | None = None):
        super().__init__(timeout=900)
        self.cog = cog
        self.fixed_id = fixed_id
        self.panel_interaction = panel_interaction

    @discord.ui.button(label="멤버추가", style=discord.ButtonStyle.green)
    async def add_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = FixedRosterAddPickView(self.cog, self.fixed_id, panel_interaction=self.panel_interaction)
        await interaction.response.send_message(
            "추가할 멤버를 아래에서 검색하거나, 서버에 없는 사람은 '용병으로 추가'를 눌러주세요.",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="멤버변경", style=discord.ButtonStyle.blurple)
    async def edit_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        entry = self.cog.fixed_parties.get(self.fixed_id)
        if entry is None:
            await interaction.response.send_message("고정공격대 정보를 찾을 수 없어요.", ephemeral=True)
            return
        options = _build_roster_options(entry, interaction.guild)
        if not options:
            await interaction.response.send_message("등록된 멤버가 없어요.", ephemeral=True)
            return
        view = FixedRosterEditPickView(self.cog, self.fixed_id, options[:25], panel_interaction=self.panel_interaction)
        await interaction.response.send_message("캐릭터를 변경할 멤버를 선택해주세요.", view=view, ephemeral=True)

    @discord.ui.button(label="멤버삭제", style=discord.ButtonStyle.red)
    async def remove_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        entry = self.cog.fixed_parties.get(self.fixed_id)
        if entry is None:
            await interaction.response.send_message("고정공격대 정보를 찾을 수 없어요.", ephemeral=True)
            return
        options = _build_roster_options(entry, interaction.guild)
        if not options:
            await interaction.response.send_message("등록된 멤버가 없어요.", ephemeral=True)
            return
        view = FixedRosterRemovePickView(self.cog, self.fixed_id, options[:25], panel_interaction=self.panel_interaction)
        await interaction.response.send_message("삭제할 멤버를 선택해주세요.", view=view, ephemeral=True)

    @discord.ui.button(label="다음: 설명 작성", style=discord.ButtonStyle.green)
    async def proceed(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = _FixedPartyDescriptionStepView(self.cog, self.fixed_id)
        await interaction.response.edit_message(
            content="설명을 추가하시겠어요? (안 넣어도 괜찮아요)", view=view
        )


class _FixedPartyDescriptionStepView(discord.ui.View):
    def __init__(self, cog: "RaidScheduleCog", fixed_id: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.fixed_id = fixed_id

    @discord.ui.button(label="설명 작성하기", style=discord.ButtonStyle.blurple)
    async def write_description(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = FixedPartyDescriptionModal(self.cog, self.fixed_id, origin_message=interaction.message)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="설명 없이 계속", style=discord.ButtonStyle.gray)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="등록을 마무리하고 있어요...", view=None)
        await self.cog.finalize_fixed_party_description(interaction, self.fixed_id, "")


class FixedPartyDescriptionModal(discord.ui.Modal):
    def __init__(self, cog: "RaidScheduleCog", fixed_id: str, *, origin_message: discord.Message | None = None):
        super().__init__(title="고정공격대 설명 작성")
        self.cog = cog
        self.fixed_id = fixed_id
        self.origin_message = origin_message
        self.description_input = discord.ui.TextInput(
            label="설명 (줄바꿈 가능)", style=discord.TextStyle.paragraph, required=False, max_length=1000,
        )
        self.add_item(self.description_input)

    async def on_submit(self, interaction: discord.Interaction):
        if self.origin_message is not None:
            try:
                await self.origin_message.edit(view=None)
            except Exception:
                pass
        description_text = self.description_input.value.strip()
        await self.cog.finalize_fixed_party_description(interaction, self.fixed_id, description_text)


class _FixedFirstPostDecisionView(discord.ui.View):
    def __init__(self, cog: "RaidScheduleCog", fixed_id: str, nearest_date: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.fixed_id = fixed_id
        self.nearest_date = nearest_date

    @discord.ui.button(label="지금 바로 이번 회차 게시", style=discord.ButtonStyle.green)
    async def post_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.post_fixed_party_now(interaction, self.fixed_id, self.nearest_date)

    @discord.ui.button(label="다음 회차부터 시작", style=discord.ButtonStyle.gray)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="✅ 등록 완료! 이번 회차는 건너뛰고, 다음 정기 주기부터 자동으로 게시될게요.",
            view=None,
        )


# =========================
# 고정공격대: 관리 패널 / 로스터(멤버) 관리
# =========================
class FixedPartyPanelView(discord.ui.View):
    def __init__(self, cog: "RaidScheduleCog", fixed_id: str, *, panel_interaction: discord.Interaction | None = None):
        super().__init__(timeout=180)
        self.cog = cog
        self.fixed_id = fixed_id
        self.panel_interaction = panel_interaction

    @discord.ui.button(label="제목/설명수정", style=discord.ButtonStyle.blurple)
    async def edit_title_desc(self, interaction: discord.Interaction, button: discord.ui.Button):
        entry = self.cog.fixed_parties.get(self.fixed_id)
        if entry is None:
            await interaction.response.send_message("고정공격대 정보를 찾을 수 없어요.", ephemeral=True)
            return
        modal = FixedPartyTitleDescModal(self.cog, self.fixed_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="일정수정", style=discord.ButtonStyle.blurple)
    async def edit_schedule(self, interaction: discord.Interaction, button: discord.ui.Button):
        entry = self.cog.fixed_parties.get(self.fixed_id)
        if entry is None:
            await interaction.response.send_message("고정공격대 정보를 찾을 수 없어요.", ephemeral=True)
            return
        modal = FixedPartyCreateModal(
            self.cog, entry["title"],
            defaults={
                "weekday": entry["weekday"], "hour": entry["hour"], "minute": entry["minute"],
                "raid": entry["raid"], "diff": entry["diff"],
            },
            edit_fixed_id=self.fixed_id,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="멤버추가", style=discord.ButtonStyle.green)
    async def add_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = FixedRosterAddPickView(self.cog, self.fixed_id, panel_interaction=self.panel_interaction, panel_kind="manage")
        await interaction.response.send_message(
            "추가할 멤버를 아래에서 검색하거나, 서버에 없는 사람은 '용병으로 추가'를 눌러주세요.",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="멤버수정", style=discord.ButtonStyle.blurple)
    async def edit_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        entry = self.cog.fixed_parties.get(self.fixed_id)
        if entry is None:
            await interaction.response.send_message("고정공격대 정보를 찾을 수 없어요.", ephemeral=True)
            return
        options = _build_roster_options(entry, interaction.guild)
        if not options:
            await interaction.response.send_message("등록된 멤버가 없어요.", ephemeral=True)
            return
        view = FixedRosterEditPickView(
            self.cog, self.fixed_id, options[:25], panel_interaction=self.panel_interaction, panel_kind="manage"
        )
        await interaction.response.send_message("캐릭터를 변경할 멤버를 선택해주세요.", view=view, ephemeral=True)

    @discord.ui.button(label="멤버삭제", style=discord.ButtonStyle.red)
    async def remove_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        entry = self.cog.fixed_parties.get(self.fixed_id)
        if entry is None:
            await interaction.response.send_message("고정공격대 정보를 찾을 수 없어요.", ephemeral=True)
            return
        options = _build_roster_options(entry, interaction.guild)
        if not options:
            await interaction.response.send_message("등록된 멤버가 없어요.", ephemeral=True)
            return
        view = FixedRosterRemovePickView(
            self.cog, self.fixed_id, options[:25], panel_interaction=self.panel_interaction, panel_kind="manage"
        )
        await interaction.response.send_message("삭제할 멤버를 선택해주세요.", view=view, ephemeral=True)

    @discord.ui.button(label="삭제", style=discord.ButtonStyle.red)
    async def delete_fixed_party(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = FixedPartyDeleteConfirmView(self.cog, self.fixed_id)
        await interaction.response.send_message(
            "⚠️ 이 고정공격대를 정말로 삭제하시겠습니까? (이미 올라간 레이드 게시물은 안 지워지고, 앞으로 자동 게시만 중단돼요)",
            view=view,
            ephemeral=True,
        )


class FixedPartyTitleDescModal(discord.ui.Modal):
    def __init__(self, cog: "RaidScheduleCog", fixed_id: str):
        super().__init__(title="제목/설명 수정")
        self.cog = cog
        self.fixed_id = fixed_id
        entry = cog.fixed_parties.get(fixed_id) or {}
        self.title_input = discord.ui.TextInput(label="제목", default=entry.get("title", ""), max_length=100)
        self.description_input = discord.ui.TextInput(
            label="설명 (줄바꿈 가능)", style=discord.TextStyle.paragraph, required=False, max_length=1000,
            default=entry.get("description", ""),
        )
        self.add_item(self.title_input)
        self.add_item(self.description_input)

    async def on_submit(self, interaction: discord.Interaction):
        entry = self.cog.fixed_parties.get(self.fixed_id)
        if entry is None:
            await interaction.response.send_message("고정공격대 정보를 찾을 수 없어요.", ephemeral=True)
            return
        entry["title"] = self.title_input.value.strip()
        entry["description"] = self.description_input.value.strip()
        self.cog.fixed_parties[self.fixed_id] = entry
        _save_fixed_parties(self.cog.fixed_parties)
        await interaction.response.send_message("✅ 제목/설명이 수정됐어요. (다음 자동 게시부터 반영돼요)", ephemeral=True)


class FixedPartyDeleteConfirmView(discord.ui.View):
    def __init__(self, cog: "RaidScheduleCog", fixed_id: str):
        super().__init__(timeout=60)
        self.cog = cog
        self.fixed_id = fixed_id

    @discord.ui.button(label="확인 (삭제)", style=discord.ButtonStyle.red)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        removed = self.cog.fixed_parties.pop(self.fixed_id, None)
        _save_fixed_parties(self.cog.fixed_parties)
        if removed is None:
            await interaction.response.edit_message(content="이미 삭제된 고정공격대예요.", view=None)
            return
        await interaction.response.edit_message(content=f"🗑️ **{removed['title']}** 고정공격대를 삭제했어요.", view=None)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.gray)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="삭제를 취소했어요.", view=None)


def _build_roster_options(entry: dict, guild: discord.Guild | None) -> list[discord.SelectOption]:
    options: list[discord.SelectOption] = []
    for role in ("dealer", "support"):
        for i, p in enumerate(entry["roster"][role]):
            options.append(discord.SelectOption(
                label=f"[{role_label(role)}] {p['character']}",
                description=_describe_applicant(guild, p),
                value=f"{role}:{i}",
            ))
    return options


class FixedRosterRemovePickView(discord.ui.View):
    def __init__(
        self, cog: "RaidScheduleCog", fixed_id: str, options: list[discord.SelectOption],
        *, panel_interaction: discord.Interaction | None = None, panel_kind: str = "build",
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.fixed_id = fixed_id
        self.panel_interaction = panel_interaction
        self.panel_kind = panel_kind
        select = discord.ui.Select(placeholder="삭제할 멤버 선택", options=options)
        select.callback = self._on_select
        self.add_item(select)
        self.select = select

    async def _on_select(self, interaction: discord.Interaction):
        role, idx_s = self.select.values[0].split(":")
        idx = int(idx_s)
        entry = self.cog.fixed_parties.get(self.fixed_id)
        if entry is None:
            await interaction.response.edit_message(content="고정공격대 정보를 찾을 수 없어요.", view=None)
            return
        lst = entry["roster"][role]
        if idx >= len(lst):
            await interaction.response.edit_message(content="이미 처리됐어요. 다시 시도해주세요.", view=None)
            return
        removed = lst.pop(idx)
        self.cog.fixed_parties[self.fixed_id] = entry
        _save_fixed_parties(self.cog.fixed_parties)
        await self.cog._refresh_fixed_panel(self.panel_interaction, self.panel_kind, self.fixed_id)
        await interaction.response.edit_message(content=f"✅ **{removed['character']}** 멤버를 삭제했어요.", view=None)


class FixedRosterEditPickView(discord.ui.View):
    def __init__(
        self, cog: "RaidScheduleCog", fixed_id: str, options: list[discord.SelectOption],
        *, panel_interaction: discord.Interaction | None = None, panel_kind: str = "build",
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.fixed_id = fixed_id
        self.panel_interaction = panel_interaction
        self.panel_kind = panel_kind
        select = discord.ui.Select(placeholder="캐릭터를 변경할 멤버 선택", options=options)
        select.callback = self._on_select
        self.add_item(select)
        self.select = select

    async def _on_select(self, interaction: discord.Interaction):
        role, idx_s = self.select.values[0].split(":")
        target_loc = {"role": role, "index": int(idx_s)}
        modal = CharacterNameModal(
            self.cog, self.fixed_id, mode="fixed_rename", target_loc=target_loc,
            origin_message=interaction.message, panel_interaction=self.panel_interaction, panel_kind=self.panel_kind,
        )
        await interaction.response.send_modal(modal)


class FixedRosterAddPickView(discord.ui.View):
    def __init__(
        self, cog: "RaidScheduleCog", fixed_id: str,
        *, panel_interaction: discord.Interaction | None = None, panel_kind: str = "build",
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.fixed_id = fixed_id
        self.panel_interaction = panel_interaction
        self.panel_kind = panel_kind

        self.member_select = discord.ui.UserSelect(placeholder="멤버 검색", min_values=1, max_values=1)
        self.member_select.callback = self._on_member_selected
        self.add_item(self.member_select)

    async def _on_member_selected(self, interaction: discord.Interaction):
        member = self.member_select.values[0]
        modal = CharacterNameModal(
            self.cog, self.fixed_id, mode="fixed_add", member=member,
            origin_message=interaction.message, panel_interaction=self.panel_interaction, panel_kind=self.panel_kind,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="용병으로 추가", style=discord.ButtonStyle.gray)
    async def add_mercenary(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = CharacterNameModal(
            self.cog, self.fixed_id, mode="fixed_add", member=None, is_mercenary=True,
            origin_message=interaction.message, panel_interaction=self.panel_interaction, panel_kind=self.panel_kind,
        )
        await interaction.response.send_modal(modal)


# =========================
# Cog
# =========================
class RaidScheduleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.raid_data = load_raid_data()
        self.raids: dict = _load_raids()
        self.fixed_parties: dict = _load_fixed_parties()

    async def cog_load(self) -> None:
        self.cleanup_loop.start()

    def cog_unload(self) -> None:
        self.cleanup_loop.cancel()

    @tasks.loop(minutes=1)
    async def cleanup_loop(self) -> None:
        now = datetime.now(KST)
        for raid_id, entry in list(self.raids.items()):
            try:
                start_dt = datetime.combine(
                    date.fromisoformat(entry["date"]),
                    time(hour=entry["hour"], minute=entry["minute"]),
                    tzinfo=KST,
                )
            except Exception:
                continue

            remaining = start_dt - now
            if not entry.get("reminder_sent") and timedelta(0) < remaining <= timedelta(minutes=10):
                await self._send_start_reminder(raid_id, entry)

            if now >= start_dt + timedelta(minutes=30):
                await self._close_and_lock_raid(raid_id, entry)

        # 고정공격대: 요일/시/분이 지금과 일치하고, 오늘 아직 안 올렸으면 다음 주 날짜로 자동 게시
        # (레이드 시작 "일주일 전"에 게시되도록, 매주 같은 요일/시각에 트리거되게 함)
        today_key = now.date().isoformat()
        for fixed_id, fentry in list(self.fixed_parties.items()):
            try:
                if (
                    fentry.get("weekday") == now.weekday()
                    and fentry.get("hour") == now.hour
                    and fentry.get("minute") == now.minute
                    and fentry.get("last_posted_key") != today_key
                ):
                    raid_date = (now.date() + timedelta(days=7)).isoformat()
                    posted = await self._post_fixed_party_instance(fixed_id, fentry, raid_date)
                    if posted is not None:
                        fentry["last_posted_key"] = today_key
                        self.fixed_parties[fixed_id] = fentry
                        _save_fixed_parties(self.fixed_parties)
            except Exception as e:
                print(f"[고정공격대] 자동 게시 처리 실패 (fixed_id={fixed_id}): {e}")

    @cleanup_loop.before_loop
    async def before_cleanup_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _send_start_reminder(self, raid_id: str, entry: dict) -> None:
        # 전송 실패해도 매분 재시도로 스팸이 되지 않게, 먼저 플래그부터 저장함
        entry["reminder_sent"] = True
        self.raids[raid_id] = entry
        _save_raids(self.raids)

        mentions = []
        for role in ("dealer", "support"):
            for p in entry["participants"][role]:
                if p.get("is_mercenary"):
                    continue
                uid = p.get("user_id")
                if uid:
                    mentions.append(f"<@{uid}>")

        try:
            channel = self.bot.get_channel(entry["channel_id"]) or await self.bot.fetch_channel(entry["channel_id"])
        except Exception as e:
            print(f"[레이드일정] 알림 채널 조회 실패 (raid_id={raid_id}): {e}")
            return

        text = f"⏰ 곧 시작! 약 10분 뒤에 **[{entry['diff']}] {entry['raid']}** 레이드가 시작돼요."
        if mentions:
            text += "\n" + " ".join(mentions)

        try:
            await channel.send(text)
        except discord.NotFound:
            pass  # 이미 삭제된 스레드
        except Exception as e:
            print(f"[레이드일정] 알림 전송 실패 (raid_id={raid_id}): {e}")

    async def _close_and_lock_raid(self, raid_id: str, entry: dict) -> None:
        # 처리 실패해도 데이터는 먼저 지워서 무한 재시도로 매분 실패 로그가 쌓이지 않게 함
        self.raids.pop(raid_id, None)
        _save_raids(self.raids)

        try:
            thread = self.bot.get_channel(entry["channel_id"]) or await self.bot.fetch_channel(entry["channel_id"])
        except discord.NotFound:
            return
        except Exception as e:
            print(f"[레이드일정] 스레드 조회 실패 (raid_id={raid_id}): {e}")
            return

        try:
            await thread.send("🔒 레이드 시작 후 30분이 지나서 이 게시물을 마감했어요.")
        except Exception as e:
            print(f"[레이드일정] 마감 메시지 전송 실패 (raid_id={raid_id}): {e}")

        try:
            # 포스트 잠그기(locked)는 포스트 닫기(archived)와 함께 설정해야
            # 디스코드 UI의 "포스트 닫기" + "포스트 잠그기"를 동시에 적용한 것과 동일해짐
            await thread.edit(archived=True, locked=True)
            print(f"[레이드일정] 시작 30분 경과로 포스트를 닫고 잠갔어요 (raid_id={raid_id})")
        except discord.NotFound:
            pass  # 이미 삭제된 스레드
        except Exception as e:
            print(f"[레이드일정] 포스트 닫기/잠그기 실패 (raid_id={raid_id}): {e}")

    # ---------------- 레이드 채널 지정 ----------------
    @app_commands.command(name="레이드채널", description="레이드 모집 게시물을 올릴 포럼 채널을 지정합니다. (관리자 전용)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(채널="레이드 모집 게시물을 올릴 포럼 채널")
    async def set_raid_channel(self, interaction: discord.Interaction, 채널: discord.ForumChannel):
        if interaction.guild is None:
            await interaction.response.send_message("이 명령어는 서버에서만 사용할 수 있어요.", ephemeral=True)
            return

        me = interaction.guild.me
        if me is not None:
            perms = 채널.permissions_for(me)
            if not (perms.view_channel and perms.send_messages_in_threads and perms.create_public_threads):
                await interaction.response.send_message(
                    "봇에게 이 채널에 대한 권한(채널 보기 / 스레드 생성 / 스레드에서 메시지 보내기)이 부족해요.",
                    ephemeral=True,
                )
                return

        raid_channel_manager.set_channel(interaction.guild.id, 채널.id)
        await interaction.response.send_message(
            f"✅ 레이드 채널이 {채널.mention}(으)로 설정됐어요. 서버당 하나의 채널만 지정할 수 있어서, "
            f"이전에 다른 채널이 지정돼 있었다면 이 채널로 교체됐어요.",
            ephemeral=True,
        )

    @app_commands.command(name="레이드채널해제", description="레이드 채널 설정을 해제합니다. (관리자 전용)")
    @app_commands.default_permissions(administrator=True)
    async def remove_raid_channel(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("이 명령어는 서버에서만 사용할 수 있어요.", ephemeral=True)
            return
        if raid_channel_manager.get_channel(interaction.guild.id) is None:
            await interaction.response.send_message("현재 설정된 레이드 채널이 없어요.", ephemeral=True)
            return
        raid_channel_manager.remove_channel(interaction.guild.id)
        await interaction.response.send_message("✅ 레이드 채널 설정이 해제됐어요.", ephemeral=True)

    @app_commands.command(name="레이드채널정보", description="현재 설정된 레이드 채널을 확인합니다.")
    async def raid_channel_info(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("이 명령어는 서버에서만 사용할 수 있어요.", ephemeral=True)
            return
        channel_id = raid_channel_manager.get_channel(interaction.guild.id)
        if channel_id is None:
            await interaction.response.send_message(
                "현재 설정된 레이드 채널이 없어요. `/레이드채널`로 지정해주세요.", ephemeral=True
            )
            return
        channel = interaction.guild.get_channel(channel_id)
        if channel is None:
            # 채널이 삭제된 것으로 판단, 지정도 같이 해제
            raid_channel_manager.remove_channel(interaction.guild.id)
            await interaction.response.send_message(
                "설정됐던 레이드 채널이 삭제된 것 같아서 자동으로 지정을 해제했어요. `/레이드채널`로 다시 지정해주세요.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(f"현재 레이드 채널: {channel.mention}", ephemeral=True)

    # ---------------- 레이드 생성 ----------------
    @app_commands.command(name="레이드", description="레이드 모집 일정을 등록합니다.")
    @app_commands.describe(제목="모집 게시물 제목")
    async def create_raid(self, interaction: discord.Interaction, 제목: str):
        if interaction.guild is None:
            await interaction.response.send_message("이 명령어는 서버에서만 사용할 수 있어요.", ephemeral=True)
            return

        channel_id = raid_channel_manager.get_channel(interaction.guild.id)
        if channel_id is None:
            await interaction.response.send_message(
                "레이드 채널이 아직 지정되지 않았어요. 관리자에게 `/레이드채널`로 먼저 지정해달라고 요청해주세요.",
                ephemeral=True,
            )
            return

        forum = interaction.guild.get_channel(channel_id)
        if not isinstance(forum, discord.ForumChannel):
            raid_channel_manager.remove_channel(interaction.guild.id)
            await interaction.response.send_message(
                "설정된 레이드 채널을 찾을 수 없어서 자동으로 지정을 해제했어요. "
                "관리자에게 `/레이드채널`로 다시 지정해달라고 요청해주세요.",
                ephemeral=True,
            )
            return

        modal = RaidCreateModal(self, 제목.strip())
        await interaction.response.send_modal(modal)

    # ---------------- 고정공격대 명령어 ----------------
    async def fixed_party_autocomplete(self, interaction: discord.Interaction, current: str):
        guild = interaction.guild
        if guild is None:
            return []
        choices = []
        for fid, fentry in self.fixed_parties.items():
            if fentry.get("guild_id") != guild.id:
                continue
            title = fentry.get("title", "")
            if current.lower() in title.lower():
                label = f"{title} (매주 {WEEKDAYS_KO[fentry['weekday']]} {fentry['hour']:02d}:{fentry['minute']:02d})"
                choices.append(app_commands.Choice(name=label[:100], value=fid))
        return choices[:25]

    @app_commands.command(name="고정공격대생성", description="매주 자동으로 게시되는 고정공격대를 등록합니다.")
    @app_commands.describe(제목="모집 게시물 제목")
    async def create_fixed_party_cmd(self, interaction: discord.Interaction, 제목: str):
        if interaction.guild is None:
            await interaction.response.send_message("이 명령어는 서버에서만 사용할 수 있어요.", ephemeral=True)
            return

        channel_id = raid_channel_manager.get_channel(interaction.guild.id)
        if channel_id is None:
            await interaction.response.send_message(
                "레이드 채널이 아직 지정되지 않았어요. `/레이드채널`로 먼저 지정해주세요.", ephemeral=True
            )
            return

        forum = interaction.guild.get_channel(channel_id)
        if not isinstance(forum, discord.ForumChannel):
            raid_channel_manager.remove_channel(interaction.guild.id)
            await interaction.response.send_message(
                "설정된 레이드 채널을 찾을 수 없어서 자동으로 지정을 해제했어요. `/레이드채널`로 다시 지정해주세요.",
                ephemeral=True,
            )
            return

        modal = FixedPartyCreateModal(self, 제목.strip())
        await interaction.response.send_modal(modal)

    @app_commands.command(name="고정공격대목록", description="이 서버에 등록된 고정공격대 목록을 봅니다.")
    async def list_fixed_parties(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("이 명령어는 서버에서만 사용할 수 있어요.", ephemeral=True)
            return

        items = [f for f in self.fixed_parties.values() if f.get("guild_id") == interaction.guild.id]
        if not items:
            await interaction.response.send_message("등록된 고정공격대가 없어요.", ephemeral=True)
            return

        lines = []
        for f in items:
            d_count = len(f["roster"]["dealer"])
            s_count = len(f["roster"]["support"])
            diff_part = f" {f['diff']}" if f.get("diff") else ""
            lines.append(
                f"**{f['title']}** — {f['raid']}{diff_part} · 매주 {WEEKDAYS_KO[f['weekday']]}요일 "
                f"{f['hour']:02d}:{f['minute']:02d} · 딜러 {d_count}/{f['dealer_slots']} · 서포터 {s_count}/{f['support_slots']}"
            )
        embed = discord.Embed(title="📌 고정공격대 목록", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="고정공격대관리", description="고정공격대의 일정/멤버를 관리합니다. (작성자 또는 관리자만 실제 조작 가능)")
    @app_commands.describe(고정공격대="관리할 고정공격대")
    @app_commands.autocomplete(고정공격대=fixed_party_autocomplete)
    async def manage_fixed_party(self, interaction: discord.Interaction, 고정공격대: str):
        if interaction.guild is None:
            await interaction.response.send_message("이 명령어는 서버에서만 사용할 수 있어요.", ephemeral=True)
            return

        entry = self.fixed_parties.get(고정공격대)
        if entry is None or entry.get("guild_id") != interaction.guild.id:
            await interaction.response.send_message(
                "해당 고정공격대를 찾을 수 없어요. 목록에서 다시 선택해주세요.", ephemeral=True
            )
            return

        is_creator = interaction.user.id == entry.get("creator_id")
        is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
        if not (is_creator or is_admin):
            await interaction.response.send_message(
                "이 고정공격대를 관리할 권한이 없어요. (서버 관리자 또는 만든 사람만 가능해요)", ephemeral=True
            )
            return

        view = FixedPartyPanelView(self, 고정공격대, panel_interaction=interaction)
        content = _fixed_roster_summary_text(entry, footer="\n아래 버튼으로 계속 관리할 수 있어요. (나만 보여요)")
        await interaction.response.send_message(content, view=view, ephemeral=True)

    async def create_raid_post(
        self,
        interaction: discord.Interaction,
        title_text: str,
        description_text: str,
        date_str: str,
        hour: int,
        minute: int,
        raid: str,
        diff: str,
    ):
        guild = interaction.guild
        channel_id = raid_channel_manager.get_channel(guild.id) if guild else None
        forum = guild.get_channel(channel_id) if (guild and channel_id) else None

        if not isinstance(forum, discord.ForumChannel):
            if guild:
                raid_channel_manager.remove_channel(guild.id)
            await self._final_respond(
                interaction,
                "레이드 채널 설정에 문제가 생겨서 자동으로 지정을 해제했어요. "
                "관리자에게 `/레이드채널`로 다시 지정해달라고 요청해주세요.",
            )
            return

        if raid == OTHER_RAID_LABEL:
            dealer_slots, support_slots, min_level = OTHER_DEALER_SLOTS, OTHER_SUPPORT_SLOTS, 0
        else:
            gold, bound, total, min_level, dealer_slots, support_slots = self.raid_data.get(
                (raid, diff), (0, 0, 0, 0, DEFAULT_DEALER_SLOTS, DEFAULT_SUPPORT_SLOTS)
            )

        entry = {
            "guild_id": guild.id,
            "channel_id": None,  # 스레드 생성 후 채움
            "creator_id": interaction.user.id,
            "title": title_text,
            "description": description_text,
            "raid": raid,
            "diff": diff,
            "date": date_str,
            "hour": hour,
            "minute": minute,
            "dealer_slots": dealer_slots,
            "support_slots": support_slots,
            "min_level": min_level,
            "participants": {"dealer": [], "support": []},
            "queue": [],
            "reminder_sent": False,
        }

        thread_name = _thread_title(entry)
        if len(thread_name) > 100:
            thread_name = thread_name[:99] + "…"

        embed = self._build_embed(entry)
        view = RaidPostView()

        try:
            result = await forum.create_thread(name=thread_name, embed=embed, view=view)
        except discord.Forbidden:
            await self._final_respond(interaction, "봇에게 레이드 채널에 글을 작성할 권한이 없어요.")
            return
        except discord.HTTPException as e:
            await self._final_respond(
                interaction,
                f"게시물 생성에 실패했어요: {e}\n"
                f"(포럼 채널에 필수 태그가 지정돼 있으면 지금 버전에서는 자동으로 못 붙여요.)",
            )
            return

        thread = result.thread
        message = result.message
        entry["channel_id"] = thread.id

        self.raids[str(message.id)] = entry
        _save_raids(self.raids)

        await self._final_respond(interaction, f"✅ 레이드 모집 게시물을 만들었어요: {thread.mention}")

    async def update_raid_schedule_only(
        self,
        interaction: discord.Interaction,
        raid_id: str,
        date_str: str,
        hour: int,
        minute: int,
    ):
        entry = self.raids.get(raid_id)
        if entry is None:
            await interaction.response.send_message("레이드 정보를 찾을 수 없어요.", ephemeral=True)
            return

        entry["date"] = date_str
        entry["hour"] = hour
        entry["minute"] = minute
        entry["reminder_sent"] = False  # 시간이 바뀌었을 수 있으니 알림 상태 초기화

        self.raids[raid_id] = entry
        _save_raids(self.raids)

        await self._rename_thread(entry)
        await self._update_post_embed_by_id(raid_id)
        await interaction.response.send_message("✅ 레이드 일정이 수정됐어요.", ephemeral=True)

    # ---------------- 고정공격대 ----------------
    async def _refresh_fixed_panel(
        self, panel_interaction: discord.Interaction | None, panel_kind: str, fixed_id: str
    ) -> None:
        """로스터 구성/관리 패널(항상 최초로 그 패널을 띄웠던 인터랙션)을 최신 상태로 다시 그림.

        ephemeral 메시지는 그걸 만든 인터랙션의 토큰으로만 편집할 수 있어서,
        나중에 캡처해둔 Message 객체를 직접 .edit()하는 방식은 동작하지 않음 -> 항상
        원본 interaction.edit_original_response()를 사용해야 함.
        """
        if panel_interaction is None:
            return
        entry = self.fixed_parties.get(fixed_id)
        if entry is None:
            return
        if panel_kind == "manage":
            content = _fixed_roster_summary_text(entry, footer="\n아래 버튼으로 계속 관리할 수 있어요.")
            view: discord.ui.View = FixedPartyPanelView(self, fixed_id)
        else:
            content = _fixed_roster_summary_text(entry)
            view = FixedRosterBuildView(self, fixed_id, panel_interaction=panel_interaction)
        try:
            await panel_interaction.edit_original_response(content=content, view=view)
        except Exception as e:
            print(f"[고정공격대] 패널 갱신 실패: {e}")

    async def create_fixed_party_entry(
        self,
        interaction: discord.Interaction,
        title_text: str,
        weekday: int,
        hour: int,
        minute: int,
        raid: str,
        diff: str,
    ):
        guild = interaction.guild
        if guild is None:
            await self._final_respond(interaction, "이 명령어는 서버에서만 사용할 수 있어요.")
            return

        if raid == OTHER_RAID_LABEL:
            dealer_slots, support_slots, min_level = OTHER_DEALER_SLOTS, OTHER_SUPPORT_SLOTS, 0
        else:
            gold, bound, total, min_level, dealer_slots, support_slots = self.raid_data.get(
                (raid, diff), (0, 0, 0, 0, DEFAULT_DEALER_SLOTS, DEFAULT_SUPPORT_SLOTS)
            )

        fixed_id = f"{guild.id}-{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        entry = {
            "guild_id": guild.id,
            "creator_id": interaction.user.id,
            "title": title_text,
            "description": "",
            "raid": raid,
            "diff": diff,
            "weekday": weekday,
            "hour": hour,
            "minute": minute,
            "dealer_slots": dealer_slots,
            "support_slots": support_slots,
            "min_level": min_level,
            "roster": {"dealer": [], "support": []},
            "last_posted_key": None,
        }
        self.fixed_parties[fixed_id] = entry
        _save_fixed_parties(self.fixed_parties)

        view = FixedRosterBuildView(self, fixed_id, panel_interaction=interaction)
        await self._final_respond_view(interaction, _fixed_roster_summary_text(entry), view)

    async def update_fixed_party_schedule(
        self,
        interaction: discord.Interaction,
        fixed_id: str,
        weekday: int,
        hour: int,
        minute: int,
        raid: str,
        diff: str,
    ):
        entry = self.fixed_parties.get(fixed_id)
        if entry is None:
            await interaction.response.send_message("고정공격대 정보를 찾을 수 없어요.", ephemeral=True)
            return

        if raid == OTHER_RAID_LABEL:
            dealer_slots, support_slots, min_level = OTHER_DEALER_SLOTS, OTHER_SUPPORT_SLOTS, 0
        else:
            gold, bound, total, min_level, dealer_slots, support_slots = self.raid_data.get(
                (raid, diff), (0, 0, 0, 0, DEFAULT_DEALER_SLOTS, DEFAULT_SUPPORT_SLOTS)
            )

        entry["weekday"] = weekday
        entry["hour"] = hour
        entry["minute"] = minute
        entry["raid"] = raid
        entry["diff"] = diff
        entry["dealer_slots"] = dealer_slots
        entry["support_slots"] = support_slots
        entry["min_level"] = min_level

        self.fixed_parties[fixed_id] = entry
        _save_fixed_parties(self.fixed_parties)
        await interaction.response.send_message(
            f"✅ 고정공격대 일정이 수정됐어요. (매주 {WEEKDAYS_KO[weekday]}요일 {hour:02d}:{minute:02d})\n"
            f"⚠️ 정원이 바뀌었다면 기존 로스터 인원이 새 정원을 초과할 수 있어요. 필요하면 '멤버 삭제'로 조정해주세요.",
            ephemeral=True,
        )

    async def finalize_fixed_party_description(
        self, interaction: discord.Interaction, fixed_id: str, description_text: str
    ):
        entry = self.fixed_parties.get(fixed_id)
        if entry is None:
            await self._final_respond(interaction, "고정공격대 정보를 찾을 수 없어요.")
            return
        entry["description"] = description_text
        self.fixed_parties[fixed_id] = entry
        _save_fixed_parties(self.fixed_parties)

        nearest = _next_weekday_date(entry["weekday"])
        view = _FixedFirstPostDecisionView(self, fixed_id, nearest.isoformat())
        d = nearest
        nearest_label = f"{nearest.isoformat()}({WEEKDAYS_KO[d.weekday()]})"
        await self._final_respond_view(
            interaction,
            f"✅ 고정공격대 **{entry['title']}** 등록이 거의 끝났어요.\n"
            f"가장 가까운 회차는 **{nearest_label} {entry['hour']:02d}:{entry['minute']:02d}**인데, "
            f"보통은 이 회차 기준 일주일 전에 자동 게시돼요 (지금이 이미 그 시점을 지났을 수도 있어요).\n"
            f"이번 회차도 지금 바로 게시할까요, 아니면 다음 회차부터 정상적으로 시작할까요?",
            view,
        )

    async def post_fixed_party_now(self, interaction: discord.Interaction, fixed_id: str, nearest_date: str):
        fentry = self.fixed_parties.get(fixed_id)
        if fentry is None:
            await interaction.response.edit_message(content="고정공격대 정보를 찾을 수 없어요.", view=None)
            return

        posted = await self._post_fixed_party_instance(fixed_id, fentry, nearest_date)
        if posted is None:
            await interaction.response.edit_message(
                content="게시에 실패했어요. `/레이드채널`이 제대로 지정돼 있는지 확인해주세요.", view=None
            )
            return
        thread, _message = posted
        await interaction.response.edit_message(content=f"✅ 이번 회차도 바로 게시했어요: {thread.mention}", view=None)

    async def finalize_fixed_add(
        self,
        interaction: discord.Interaction,
        fixed_id: str,
        character: str,
        info: dict,
        role: str,
        member: discord.Member | None,
        *,
        panel_interaction: discord.Interaction | None = None,
        panel_kind: str = "build",
    ):
        entry = self.fixed_parties.get(fixed_id)
        if entry is None:
            await self._respond(interaction, "고정공격대 정보를 찾을 수 없어요.")
            return

        if member is not None:
            for r in ("dealer", "support"):
                for p in entry["roster"][r]:
                    if not p.get("is_mercenary") and p.get("user_id") == member.id:
                        await self._respond(interaction, f"{member.mention}님은 이미 이 고정공격대에 등록돼있어요.")
                        return

        applicant = {
            "user_id": member.id if member else None,
            "character": character,
            "class": info.get("class_name"),
            "level": info.get("level"),
            "combat_power": info.get("combat_power"),
            "is_mercenary": member is None,
        }

        slots_key = f"{role}_slots"
        capacity = entry.get(slots_key, 0)
        current = entry["roster"][role]
        if len(current) >= capacity:
            await self._respond(
                interaction,
                f"{role_label(role)} 정원({capacity}명)이 이미 다 찼어요. 다른 멤버를 빼거나 정원을 늘려주세요.",
            )
            return

        current.append(applicant)
        self.fixed_parties[fixed_id] = entry
        _save_fixed_parties(self.fixed_parties)

        await self._refresh_fixed_panel(panel_interaction, panel_kind, fixed_id)
        await self._respond(interaction, f"✅ **{character}**님을 {role_label(role)}로 고정 멤버에 추가했어요.")

    async def finalize_fixed_rename(
        self,
        interaction: discord.Interaction,
        fixed_id: str,
        target_loc: dict,
        character: str,
        info: dict,
        role: str,
        *,
        panel_interaction: discord.Interaction | None = None,
        panel_kind: str = "build",
    ):
        entry = self.fixed_parties.get(fixed_id)
        if entry is None:
            await self._respond(interaction, "고정공격대 정보를 찾을 수 없어요.")
            return

        old_role = target_loc["role"]
        idx = target_loc["index"]
        lst = entry["roster"][old_role]
        if idx >= len(lst):
            await self._respond(interaction, "이미 처리됐어요. 다시 시도해주세요.")
            return

        if role == old_role:
            target = lst[idx]
            target["character"] = character
            target["class"] = info.get("class_name")
            target["level"] = info.get("level")
            target["combat_power"] = info.get("combat_power")
            msg = f"✅ 캐릭터가 **{character}**(으)로 변경됐어요."
        else:
            slots_key = f"{role}_slots"
            capacity = entry.get(slots_key, 0)
            if len(entry["roster"][role]) >= capacity:
                await self._respond(
                    interaction,
                    f"❌ {role_label(role)} 정원({capacity}명)이 이미 다 찼어요. 역할을 바꾸려면 먼저 자리를 비워주세요.",
                )
                return
            original = lst.pop(idx)
            original["character"] = character
            original["class"] = info.get("class_name")
            original["level"] = info.get("level")
            original["combat_power"] = info.get("combat_power")
            entry["roster"][role].append(original)
            msg = f"✅ 캐릭터가 **{character}**(으)로 변경됐고, {role_label(role)}로 재배치됐어요."

        self.fixed_parties[fixed_id] = entry
        _save_fixed_parties(self.fixed_parties)

        await self._refresh_fixed_panel(panel_interaction, panel_kind, fixed_id)
        await self._respond(interaction, msg)

    async def _post_fixed_party_instance(
        self, fixed_id: str, fentry: dict, raid_date: str
    ) -> tuple[discord.Thread, discord.Message] | None:
        guild = self.bot.get_guild(fentry["guild_id"])
        if guild is None:
            print(f"[고정공격대] 서버를 찾을 수 없어 건너뜀 (fixed_id={fixed_id})")
            return None

        channel_id = raid_channel_manager.get_channel(guild.id)
        forum = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(forum, discord.ForumChannel):
            print(f"[고정공격대] 레이드 채널이 없어 건너뜀 (guild_id={guild.id})")
            return None

        entry = {
            "guild_id": guild.id,
            "channel_id": None,
            "creator_id": fentry.get("creator_id"),
            "title": fentry["title"],
            "description": fentry.get("description", ""),
            "raid": fentry["raid"],
            "diff": fentry["diff"],
            "date": raid_date,
            "hour": fentry["hour"],
            "minute": fentry["minute"],
            "dealer_slots": fentry.get("dealer_slots", DEFAULT_DEALER_SLOTS),
            "support_slots": fentry.get("support_slots", DEFAULT_SUPPORT_SLOTS),
            "min_level": fentry.get("min_level", 0),
            "participants": {
                "dealer": [dict(p) for p in fentry["roster"]["dealer"]],
                "support": [dict(p) for p in fentry["roster"]["support"]],
            },
            "queue": [],
            "reminder_sent": False,
            "from_fixed_party": fixed_id,
        }

        thread_name = _thread_title(entry)
        if len(thread_name) > 100:
            thread_name = thread_name[:99] + "…"

        embed = self._build_embed(entry)
        view = RaidPostView()

        try:
            result = await forum.create_thread(name=thread_name, embed=embed, view=view)
        except Exception as e:
            print(f"[고정공격대] 게시물 생성 실패 (fixed_id={fixed_id}): {e}")
            return None

        entry["channel_id"] = result.thread.id
        self.raids[str(result.message.id)] = entry
        _save_raids(self.raids)

        print(f"[고정공격대] 게시 완료 (fixed_id={fixed_id}, raid_date={raid_date})")
        return result.thread, result.message

    async def _final_respond_view(self, interaction: discord.Interaction, content: str, view: discord.ui.View) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(content, view=view, ephemeral=True)

    async def update_raid_post(
        self,
        interaction: discord.Interaction,
        raid_id: str,
        date_str: str,
        hour: int,
        minute: int,
        raid: str,
        diff: str,
    ):
        entry = self.raids.get(raid_id)
        if entry is None:
            await interaction.response.send_message("레이드 정보를 찾을 수 없어요.", ephemeral=True)
            return

        entry["date"] = date_str
        entry["hour"] = hour
        entry["minute"] = minute
        entry["raid"] = raid
        entry["diff"] = diff
        gold, bound, total, min_level, dealer_slots, support_slots = self.raid_data.get(
            (raid, diff), (0, 0, 0, 0, DEFAULT_DEALER_SLOTS, DEFAULT_SUPPORT_SLOTS)
        )
        entry["min_level"] = min_level
        entry["dealer_slots"] = dealer_slots
        entry["support_slots"] = support_slots
        entry["reminder_sent"] = False  # 시간이 바뀌었을 수 있으니 알림 상태 초기화

        self.raids[raid_id] = entry
        _save_raids(self.raids)

        await self._rename_thread(entry)
        await self._update_post_embed_by_id(raid_id)
        await interaction.response.send_message("✅ 레이드 일정이 수정됐어요.", ephemeral=True)

    # ---------------- 참가신청 ----------------
    async def handle_apply(self, interaction: discord.Interaction, raid_id: str, character: str, info: dict):
        class_name = info["class_name"]
        if class_name in SUPPORTER_CLASSES:
            view = RoleChoiceView(self, raid_id, character, info, mode="apply")
            await interaction.followup.send(
                f"**{character}** ({class_name}, {info['level']:,.2f})님은 딜러/서포터 모두 지원 가능해요. 역할을 선택해주세요.",
                view=view,
                ephemeral=True,
            )
            return
        await self.finalize_apply(interaction, raid_id, character, info, "dealer")

    async def finalize_apply(self, interaction: discord.Interaction, raid_id: str, character: str, info: dict, role: str):
        entry = self.raids.get(raid_id)
        if entry is None:
            await self._respond(interaction, "레이드 정보를 찾을 수 없어요.")
            return

        user_id = interaction.user.id
        if _find_application(entry, user_id) is not None:
            await self._respond(interaction, "이미 이 레이드에 신청하셨어요.")
            return

        applicant = {
            "user_id": user_id,
            "character": character,
            "class": info.get("class_name"),
            "level": info.get("level"),
            "combat_power": info.get("combat_power"),
            "is_mercenary": False,
            "applied_at": datetime.now(timezone.utc).isoformat(),
        }

        slots_key = f"{role}_slots"
        capacity = entry.get(slots_key, 0)
        current = entry["participants"][role]

        if len(current) < capacity:
            current.append(applicant)
            msg = f"✅ **{character}** ({role_label(role)})로 신청 완료됐어요!"
        else:
            applicant["role"] = role
            entry["queue"].append(applicant)
            msg = f"⏳ **{role_label(role)}** 자리가 가득 차서 대기열에 등록됐어요. (대기 순번 {len(entry['queue'])}번)"

        self.raids[raid_id] = entry
        _save_raids(self.raids)
        await self._update_post_embed_by_id(raid_id)
        await self._respond(interaction, msg)

    # ---------------- 취소 ----------------
    async def handle_cancel(self, interaction: discord.Interaction, raid_id: str):
        entry = self.raids.get(raid_id)
        if entry is None:
            await interaction.response.send_message("레이드 정보를 찾을 수 없어요.", ephemeral=True)
            return

        user_id = interaction.user.id

        for role in ("dealer", "support"):
            lst = entry["participants"][role]
            for i, p in enumerate(lst):
                if not p.get("is_mercenary") and p.get("user_id") == user_id:
                    lst.pop(i)
                    self._promote_from_queue(entry, role)
                    self.raids[raid_id] = entry
                    _save_raids(self.raids)
                    await self._update_post_embed_by_id(raid_id)
                    await interaction.response.send_message("✅ 참가 신청이 취소됐어요.", ephemeral=True)
                    return

        queue = entry["queue"]
        for i, p in enumerate(queue):
            if p.get("user_id") == user_id:
                queue.pop(i)
                self.raids[raid_id] = entry
                _save_raids(self.raids)
                await interaction.response.send_message("✅ 대기열에서 취소됐어요.", ephemeral=True)
                return

        await interaction.response.send_message("신청 내역을 찾을 수 없어요.", ephemeral=True)

    def _promote_from_queue(self, entry: dict, role: str) -> None:
        """빈 자리(role)에 대기열에서 가장 먼저 신청한 사람을 자동으로 승격시킴."""
        queue = entry["queue"]
        for i, p in enumerate(queue):
            if p.get("role") == role:
                promoted = queue.pop(i)
                promoted.pop("role", None)
                entry["participants"][role].append(promoted)
                return

    # ---------------- 닉네임 변경 (이름만 변경, 역할/순서 유지) ----------------
    async def handle_rename(self, interaction: discord.Interaction, raid_id: str, character: str, info: dict):
        entry = self.raids.get(raid_id)
        if entry is None:
            await self._respond(interaction, "레이드 정보를 찾을 수 없어요.")
            return

        loc = _find_application(entry, interaction.user.id)
        if loc is None:
            await self._respond(interaction, "신청 내역을 찾을 수 없어요.")
            return

        class_name = info.get("class_name")
        if class_name in SUPPORTER_CLASSES:
            view = RoleChoiceView(self, raid_id, character, info, mode="rename")
            await interaction.followup.send(
                f"**{character}** ({class_name})님은 딜러/서포터 모두 지원 가능해요. 역할을 선택해주세요.",
                view=view, ephemeral=True,
            )
            return

        # 겸용 직업이 아니면 무조건 딜러
        await self.finalize_rename(interaction, raid_id, character, info, "dealer")

    async def finalize_rename(
        self, interaction: discord.Interaction, raid_id: str, character: str, info: dict, role: str
    ):
        entry = self.raids.get(raid_id)
        if entry is None:
            await self._respond(interaction, "레이드 정보를 찾을 수 없어요.")
            return

        loc = _find_application(entry, interaction.user.id)
        if loc is None:
            await self._respond(interaction, "신청 내역을 찾을 수 없어요.")
            return

        # 역할이 그대로면 자리 이동 없이 캐릭터 정보만 갱신
        if loc["where"] == "participant" and loc["role"] == role:
            target = entry["participants"][role][loc["index"]]
            target["character"] = character
            target["class"] = info.get("class_name")
            target["level"] = info.get("level")
            target["combat_power"] = info.get("combat_power")
            msg = f"✅ 캐릭터가 **{character}**(으)로 변경됐어요."
        else:
            # 역할이 바뀌었거나 대기열에 있던 경우: 기존 자리를 비우고 새 역할로 재배치
            if loc["where"] == "participant":
                old_role = loc["role"]
                entry["participants"][old_role].pop(loc["index"])
                self._promote_from_queue(entry, old_role)
            else:
                entry["queue"].pop(loc["index"])

            applicant = {
                "user_id": interaction.user.id,
                "character": character,
                "class": info.get("class_name"),
                "level": info.get("level"),
                "combat_power": info.get("combat_power"),
                "is_mercenary": False,
                "applied_at": datetime.now(timezone.utc).isoformat(),
            }
            slots_key = f"{role}_slots"
            capacity = entry.get(slots_key, 0)
            current = entry["participants"][role]
            if len(current) < capacity:
                current.append(applicant)
                msg = f"✅ 캐릭터가 **{character}**(으)로 변경됐고, {role_label(role)}로 등록됐어요."
            else:
                applicant["role"] = role
                entry["queue"].append(applicant)
                msg = (
                    f"✅ 캐릭터가 **{character}**(으)로 변경됐지만, {role_label(role)} 자리가 가득 차서 "
                    f"대기열로 이동했어요. (대기 순번 {len(entry['queue'])}번)"
                )

        self.raids[raid_id] = entry
        _save_raids(self.raids)
        await self._update_post_embed_by_id(raid_id)
        await self._respond(interaction, msg)

    # ---------------- 관리자 강제참여 ----------------
    async def finalize_force_join(
        self,
        interaction: discord.Interaction,
        raid_id: str,
        character: str,
        info: dict,
        role: str,
        member: discord.Member | None,
    ):
        entry = self.raids.get(raid_id)
        if entry is None:
            await self._respond(interaction, "레이드 정보를 찾을 수 없어요.")
            return

        # 실제 서버 멤버라면 한 사람당 한 캐릭만 신청 가능하도록 중복 체크
        # (용병은 디스코드 계정이 없어서 중복 개념이 없음 - 여러 명 추가 가능)
        if member is not None and _find_application(entry, member.id) is not None:
            await self._respond(
                interaction,
                f"{member.mention}님은 이미 이 레이드에 참가 중이에요. 먼저 `강제취소` 또는 `강제변경`을 사용해주세요.",
            )
            return

        applicant = {
            "user_id": member.id if member else None,
            "character": character,
            "class": info.get("class_name"),
            "level": info.get("level"),
            "combat_power": info.get("combat_power"),
            "is_mercenary": member is None,
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "forced": True,
        }

        slots_key = f"{role}_slots"
        capacity = entry.get(slots_key, 0)
        current = entry["participants"][role]

        if len(current) < capacity:
            current.append(applicant)
            msg = f"✅ **{character}**님을 {role_label(role)}로 강제참여시켰어요."
        else:
            applicant["role"] = role
            entry["queue"].append(applicant)
            msg = f"⏳ **{role_label(role)}** 자리가 가득 차서 대기열로 강제 등록했어요. (대기 순번 {len(entry['queue'])}번)"

        self.raids[raid_id] = entry
        _save_raids(self.raids)
        await self._update_post_embed_by_id(raid_id)
        await self._respond(interaction, msg)

    # ---------------- 관리자 강제변경 (특정 신청자의 캐릭터를 관리자가 대신 변경) ----------------
    async def finalize_force_rename(
        self,
        interaction: discord.Interaction,
        raid_id: str,
        target_loc: dict,
        character: str,
        info: dict,
        role: str,
    ):
        entry = self.raids.get(raid_id)
        if entry is None:
            await self._respond(interaction, "레이드 정보를 찾을 수 없어요.")
            return

        if target_loc["where"] == "participant":
            lst = entry["participants"][target_loc["role"]]
        else:
            lst = entry["queue"]

        idx = target_loc["index"]
        if idx >= len(lst):
            await self._respond(interaction, "이미 처리된 신청이에요. 다시 시도해주세요.")
            return

        original = lst[idx]

        # 역할이 그대로면 자리 이동 없이 캐릭터 정보만 갱신
        if target_loc["where"] == "participant" and target_loc["role"] == role:
            original["character"] = character
            original["class"] = info.get("class_name")
            original["level"] = info.get("level")
            original["combat_power"] = info.get("combat_power")
            msg = f"✅ 캐릭터가 **{character}**(으)로 강제 변경됐어요."
        else:
            user_id = original.get("user_id")
            is_mercenary = original.get("is_mercenary", False)
            applied_at = original.get("applied_at") or datetime.now(timezone.utc).isoformat()

            lst.pop(idx)
            if target_loc["where"] == "participant":
                self._promote_from_queue(entry, target_loc["role"])

            new_applicant = {
                "user_id": user_id,
                "character": character,
                "class": info.get("class_name"),
                "level": info.get("level"),
                "combat_power": info.get("combat_power"),
                "is_mercenary": is_mercenary,
                "applied_at": applied_at,
                "forced": True,
            }
            slots_key = f"{role}_slots"
            capacity = entry.get(slots_key, 0)
            current = entry["participants"][role]
            if len(current) < capacity:
                current.append(new_applicant)
                msg = f"✅ 캐릭터가 **{character}**(으)로 강제 변경됐고, {role_label(role)}로 재배치됐어요."
            else:
                new_applicant["role"] = role
                entry["queue"].append(new_applicant)
                msg = (
                    f"✅ 캐릭터가 **{character}**(으)로 강제 변경됐지만, {role_label(role)} 자리가 가득 차서 "
                    f"대기열로 이동했어요."
                )

        self.raids[raid_id] = entry
        _save_raids(self.raids)
        await self._update_post_embed_by_id(raid_id)
        await self._respond(interaction, msg)

    # ---------------- 응답 / 임베드 갱신 헬퍼 ----------------
    async def _final_respond(self, interaction: discord.Interaction, content: str) -> None:
        """항상 '새 메시지 전송' 방식으로 응답 (버튼 클릭으로 이미 edit_message를 써버린 뒤에도 안전)."""
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)

    async def _respond(self, interaction: discord.Interaction, content: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            # 버튼 클릭 등으로 아직 응답 안 한 인터랙션이면, 선택지가 있던 원래 메시지를
            # 결과 메시지로 바로 바꿔서(버튼/셀렉트 제거) 눌렀던 흔적이 안 남게 함
            await interaction.response.edit_message(content=content, embed=None, view=None)

    def _build_embed(self, entry: dict) -> discord.Embed:
        post_title = _thread_title(entry)
        embed = discord.Embed(title=post_title, description=entry.get("description") or None, color=discord.Color.blurple())
        d = date.fromisoformat(entry["date"])
        embed.add_field(
            name="🗓️ 일시",
            value=f"{entry['date']}({WEEKDAYS_KO[d.weekday()]}) {entry['hour']:02d}:{entry['minute']:02d}",
            inline=False,
        )
        embed.add_field(name="👑 공격대 생성자", value=f"<@{entry['creator_id']}>", inline=False)

        def _fmt(p: dict) -> str:
            who = "용병" if p.get("is_mercenary") else f"<@{p['user_id']}>"
            parts = [p["character"]]
            lvl = p.get("level")
            if lvl:
                parts.append(f"Lv {lvl:,.2f}")
            cls = p.get("class")
            if cls:
                parts.append(cls)
            cp = p.get("combat_power")
            if cp:
                parts.append(f"⚡ {cp:,.2f}")
            return f"{who}\n{' · '.join(parts)}"

        dealer_list = entry["participants"]["dealer"]
        support_list = entry["participants"]["support"]

        embed.add_field(
            name=f"⚔️ 딜러 ({len(dealer_list)}/{entry['dealer_slots']})",
            value="\n".join(_fmt(p) for p in dealer_list) or "아직 없음",
            inline=False,
        )
        embed.add_field(
            name=f"🛡️ 서포터 ({len(support_list)}/{entry['support_slots']})",
            value="\n".join(_fmt(p) for p in support_list) or "아직 없음",
            inline=False,
        )

        if entry.get("min_level"):
            embed.add_field(name="🔒 입장레벨", value=f"{entry['min_level']:,.2f}", inline=False)

        if entry.get("queue"):
            embed.add_field(
                name="⏳ 대기열", value=f"{len(entry['queue'])}명 대기 중 ('대기열 명단' 버튼으로 확인)", inline=False
            )

        return embed

    async def _update_post_embed_by_id(self, raid_id: str) -> None:
        entry = self.raids.get(raid_id)
        if entry is None:
            return
        guild = self.bot.get_guild(entry["guild_id"])
        if guild is None:
            return
        channel = guild.get_channel(entry["channel_id"]) or guild.get_thread(entry["channel_id"])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(entry["channel_id"])
            except Exception:
                return
        try:
            message = await channel.fetch_message(int(raid_id))
        except Exception:
            return

        embed = self._build_embed(entry)
        try:
            await message.edit(embed=embed)
        except Exception as e:
            print(f"[레이드일정] 게시물 갱신 실패 (raid_id={raid_id}): {e}")

    async def _rename_thread(self, entry: dict) -> None:
        channel_id = entry.get("channel_id")
        if not channel_id:
            return
        guild = self.bot.get_guild(entry["guild_id"])
        thread = guild.get_thread(channel_id) if guild else None
        if thread is None:
            try:
                thread = await self.bot.fetch_channel(channel_id)
            except Exception:
                return
        new_name = _thread_title(entry)
        if len(new_name) > 100:
            new_name = new_name[:99] + "…"
        try:
            await thread.edit(name=new_name)
        except Exception as e:
            print(f"[레이드일정] 스레드 이름 변경 실패: {e}")


async def setup(bot: commands.Bot):
    cog = RaidScheduleCog(bot)
    await bot.add_cog(cog)
    # 영속 View 등록: 봇이 재구동돼도 기존에 올라간 게시물의 버튼이 계속 동작하도록 함
    bot.add_view(RaidPostView())