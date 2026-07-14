import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import tempfile

from core.config import EGG_ID

# 봇 소스 위치(EGG-BOT/) 기준 절대경로로 고정
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../EGG-BOT
_DATA_DIR = os.path.join(_BASE_DIR, "data")
DATA_FILE = os.path.join(_DATA_DIR, "raid_gold.json")

# 최초 실행 시 파일이 없으면 이 기본값으로 data/raid_gold.json을 생성함
DEFAULT_RAID_DATA = {
    ("에키드나", "하드"): (3600, 3600, 7200),
    ("베히모스", "노말"): (3600, 3600, 7200),

    ("1막", "싱글"): (5750, 5750, 11500),
    ("1막", "노말"): (5750, 5750, 11500),
    ("1막", "하드"): (9000, 9000, 18000),

    ("2막", "싱글"): (8250, 8250, 16500),
    ("2막", "노말"): (8250, 8250, 16500),
    ("2막", "하드"): (11500, 11500, 23000),

    ("3막", "싱글"): (10500, 10500, 21000),
    ("3막", "노말"): (10500, 10500, 21000),
    ("3막", "하드"): (13500, 13500, 27000),

    ("4막", "싱글"): (16500, 16500, 33000),
    ("4막", "노말"): (16500, 16500, 33000),
    ("4막", "하드"): (42000, 0, 42000),

    ("종막", "싱글"): (20000, 20000, 40000),
    ("종막", "노말"): (20000, 20000, 40000),
    ("종막", "하드"): (52000, 0, 52000),

    ("세르카", "매칭"): (17500, 17500, 35000),
    ("세르카", "노말"): (17500, 17500, 35000),
    ("세르카", "하드"): (44000, 0, 44000),
    ("세르카", "나이트메어"): (54000, 0, 54000),

    ("지평의 성당", "1단계"): (0, 30000, 30000),
    ("지평의 성당", "2단계"): (0, 40000, 40000),
    ("지평의 성당", "3단계"): (0, 50000, 50000),
}

DIFF_ORDER = {
    "노말": 1,
    "하드": 2,
    "나이트메어": 3,

    "EX 노말": 4,
    "EX 하드": 5,
    "EX 나이트메어": 6,

    "1단계": 7,
    "2단계": 8,
    "3단계": 9,
}


def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == EGG_ID


# =========================
# 저장/로드 (레이드+난이도 튜플 키는 JSON에 그대로 못 담아서 "레이드|난이도" 문자열로 변환)
# =========================
def _key_to_str(key: tuple[str, str]) -> str:
    return f"{key[0]}|{key[1]}"


def _str_to_key(s: str) -> tuple[str, str]:
    raid, diff = s.split("|", 1)
    return (raid, diff)


def _load_raid_data() -> dict[tuple[str, str], tuple[int, int, int]]:
    os.makedirs(_DATA_DIR, exist_ok=True)
    if not os.path.exists(DATA_FILE):
        data = dict(DEFAULT_RAID_DATA)
        _save_raid_data(data)
        print(f"[클골] 데이터 파일이 없어 기본값으로 생성했습니다: {DATA_FILE}")
        return data
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        data = {_str_to_key(k): tuple(v) for k, v in raw.items()}
        print(f"[클골] 데이터 로드 완료: {len(data)}개 조합 (경로: {DATA_FILE})")
        return data
    except Exception as e:
        print(f"[클골] 데이터 로드 실패, 기본값 사용: {e}")
        return dict(DEFAULT_RAID_DATA)


def _save_raid_data(data: dict[tuple[str, str], tuple[int, int, int]]) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=_DATA_DIR)
    try:
        serializable = {_key_to_str(k): list(v) for k, v in data.items()}
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, DATA_FILE)
    except Exception as e:
        print(f"[클골] 데이터 저장 실패: {e}")
        try:
            os.remove(tmp_path)
        except Exception:
            pass


class ClearGoldCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.raid_data: dict[tuple[str, str], tuple[int, int, int]] = _load_raid_data()

    # ---------------- 자동완성 (항상 최신 데이터 기준) ----------------
    async def raid_autocomplete(self, interaction: discord.Interaction, current: str):
        raids = sorted(set(r[0] for r in self.raid_data.keys()))
        return [
            app_commands.Choice(name=r, value=r)
            for r in raids if current.lower() in r.lower()
        ][:25]

    async def diff_autocomplete(self, interaction: discord.Interaction, current: str):
        raid = interaction.namespace.레이드

        if raid:
            valid = {d for (r, d) in self.raid_data.keys() if r == raid}
        else:
            valid = {d for (_, d) in self.raid_data.keys()}

        sorted_valid = sorted(valid, key=lambda x: DIFF_ORDER.get(x, 999))

        return [
            app_commands.Choice(name=d, value=d)
            for d in sorted_valid
            if current.lower() in d.lower()
        ][:25]

    # ---------------- 조회 ----------------
    @app_commands.command(name="클골", description="레이드 클리어 골드 조회")
    @app_commands.autocomplete(레이드=raid_autocomplete, 난이도=diff_autocomplete)
    async def cleargold(self, interaction: discord.Interaction, 레이드: str, 난이도: str):
        key = (레이드, 난이도)

        if key not in self.raid_data:
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ 존재하지 않는 조합",
                    description=f"{레이드} - {난이도}",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )

        gold, bound, total = self.raid_data[key]

        embed = discord.Embed(
            title=f"💰 {레이드} ({난이도}) 클리어 골드",
            color=discord.Color.gold()
        )

        embed.add_field(name="골드", value=f"**{gold:,}🪙**", inline=True)
        embed.add_field(name="귀속골드", value=f"**{bound:,}🪙**", inline=True)
        embed.add_field(name="총합", value=f"**{total:,}🪙**", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------------- 추가 (계란 전용) ----------------
    @app_commands.command(name="클골추가", description="레이드 클리어 골드 추가 (계란 전용)")
    @app_commands.check(is_owner)
    @app_commands.autocomplete(레이드=raid_autocomplete, 난이도=diff_autocomplete)
    @app_commands.describe(
        레이드="레이드 이름",
        난이도="난이도",
        골드="골드",
        귀속골드="귀속골드"
    )
    async def add_raid_gold(
        self,
        interaction: discord.Interaction,
        레이드: str,
        난이도: str,
        골드: int,
        귀속골드: int,
    ):
        레이드 = 레이드.strip()
        난이도 = 난이도.strip()
        key = (레이드, 난이도)

        if key in self.raid_data:
            return await interaction.response.send_message(
                f"⚠️ 이미 등록된 조합이에요: **{레이드} - {난이도}**\n"
                f"내용을 바꾸시려면 `/클골수정`을 사용해주세요.",
                ephemeral=True
            )

        총합 = 골드 + 귀속골드
        self.raid_data[key] = (골드, 귀속골드, 총합)
        _save_raid_data(self.raid_data)

        embed = discord.Embed(
            title="✅ 클골 추가 완료",
            description=f"**{레이드} ({난이도})**",
            color=discord.Color.green()
        )
        embed.add_field(name="골드", value=f"**{골드:,}🪙**", inline=True)
        embed.add_field(name="귀속골드", value=f"**{귀속골드:,}🪙**", inline=True)
        embed.add_field(name="총합", value=f"**{총합:,}🪙**", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------------- 삭제 (계란 전용) ----------------
    @app_commands.command(name="클골삭제", description="레이드 클리어 골드 삭제 (계란 전용)")
    @app_commands.check(is_owner)
    @app_commands.autocomplete(레이드=raid_autocomplete, 난이도=diff_autocomplete)
    async def remove_raid_gold(self, interaction: discord.Interaction, 레이드: str, 난이도: str):
        key = (레이드.strip(), 난이도.strip())

        if key not in self.raid_data:
            return await interaction.response.send_message(
                f"❌ 존재하지 않는 조합이에요: **{key[0]} - {key[1]}**",
                ephemeral=True
            )

        del self.raid_data[key]
        _save_raid_data(self.raid_data)

        await interaction.response.send_message(
            f"🗑️ 삭제 완료: **{key[0]} ({key[1]})**",
            ephemeral=True
        )

    # ---------------- 수정 (계란 전용) ----------------
    @app_commands.command(name="클골수정", description="레이드 클리어 골드 수정 (계란 전용)")
    @app_commands.check(is_owner)
    @app_commands.autocomplete(레이드=raid_autocomplete, 난이도=diff_autocomplete)
    @app_commands.describe(
        레이드="수정할 레이드 이름",
        난이도="수정할 난이도",
        골드="골드",
        귀속골드="귀속골드"
    )
    async def edit_raid_gold(
        self,
        interaction: discord.Interaction,
        레이드: str,
        난이도: str,
        골드: int,
        귀속골드: int,
    ):
        key = (레이드.strip(), 난이도.strip())

        if key not in self.raid_data:
            return await interaction.response.send_message(
                f"❌ 존재하지 않는 조합이에요: **{key[0]} - {key[1]}**\n"
                f"새로 등록하시려면 `/클골추가`를 사용해주세요.",
                ephemeral=True
            )

        총합 = 골드 + 귀속골드
        self.raid_data[key] = (골드, 귀속골드, 총합)
        _save_raid_data(self.raid_data)

        embed = discord.Embed(
            title="✏️ 클골 수정 완료",
            description=f"**{key[0]} ({key[1]})**",
            color=discord.Color.blurple()
        )
        embed.add_field(name="골드", value=f"**{골드:,}🪙**", inline=True)
        embed.add_field(name="귀속골드", value=f"**{귀속골드:,}🪙**", inline=True)
        embed.add_field(name="총합", value=f"**{총합:,}🪙**", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------------- 권한 오류 처리 ----------------
    @add_raid_gold.error
    @remove_raid_gold.error
    @edit_raid_gold.error
    async def owner_only_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "⛔ 이 명령어는 계란 외에는 사용할 수 없습니다.",
                ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(ClearGoldCog(bot))