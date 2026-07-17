import discord
from discord.ext import commands
from discord import app_commands

from core.config import EGG_ID
from core.raid_data import (
    load_raid_data,
    save_raid_data,
    DIFF_ORDER,
    DEFAULT_DEALER_SLOTS,
    DEFAULT_SUPPORT_SLOTS,
)


def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == EGG_ID


class ClearGoldCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.raid_data: dict[tuple[str, str], tuple] = load_raid_data()

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

        gold, bound, total, min_level, dealer_slots, support_slots = self.raid_data[key]

        embed = discord.Embed(
            title=f"💰 {레이드} ({난이도}) 클리어 골드",
            color=discord.Color.gold()
        )

        embed.add_field(name="골드", value=f"**{gold:,}🪙**", inline=True)
        embed.add_field(name="귀속골드", value=f"**{bound:,}🪙**", inline=True)
        embed.add_field(name="총합", value=f"**{total:,}🪙**", inline=False)
        if min_level:
            embed.add_field(name="입장레벨", value=f"**{min_level:,.2f}**", inline=True)
        embed.add_field(name="파티 정원", value=f"딜러 {dealer_slots} / 서포터 {support_slots}", inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------------- 추가 (계란 전용) ----------------
    @app_commands.command(name="클골추가", description="레이드 클리어 골드 추가 (계란 전용)")
    @app_commands.check(is_owner)
    @app_commands.autocomplete(레이드=raid_autocomplete, 난이도=diff_autocomplete)
    @app_commands.describe(
        레이드="레이드 이름",
        난이도="난이도",
        골드="골드",
        귀속골드="귀속골드",
        입장레벨="입장에 필요한 최소 아이템 레벨 (비우면 제한 없음)",
        딜러정원="파티 내 딜러 정원 (기본 3명, 4인 레이드 기준)",
        서포터정원="파티 내 서포터 정원 (기본 1명, 4인 레이드 기준)"
    )
    async def add_raid_gold(
        self,
        interaction: discord.Interaction,
        레이드: str,
        난이도: str,
        골드: int,
        귀속골드: int,
        입장레벨: float = 0.0,
        딜러정원: int = DEFAULT_DEALER_SLOTS,
        서포터정원: int = DEFAULT_SUPPORT_SLOTS,
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
        self.raid_data[key] = (골드, 귀속골드, 총합, 입장레벨, 딜러정원, 서포터정원)
        save_raid_data(self.raid_data)

        embed = discord.Embed(
            title="✅ 클골 추가 완료",
            description=f"**{레이드} ({난이도})**",
            color=discord.Color.green()
        )
        embed.add_field(name="골드", value=f"**{골드:,}🪙**", inline=True)
        embed.add_field(name="귀속골드", value=f"**{귀속골드:,}🪙**", inline=True)
        embed.add_field(name="총합", value=f"**{총합:,}🪙**", inline=False)
        if 입장레벨:
            embed.add_field(name="입장레벨", value=f"**{입장레벨:,.2f}**", inline=True)
        embed.add_field(name="파티 정원", value=f"딜러 {딜러정원} / 서포터 {서포터정원}", inline=True)

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
        save_raid_data(self.raid_data)

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
        귀속골드="귀속골드",
        입장레벨="입장에 필요한 최소 아이템 레벨 (비우면 제한 없음)",
        딜러정원="파티 내 딜러 정원 (기본 3명, 4인 레이드 기준)",
        서포터정원="파티 내 서포터 정원 (기본 1명, 4인 레이드 기준)"
    )
    async def edit_raid_gold(
        self,
        interaction: discord.Interaction,
        레이드: str,
        난이도: str,
        골드: int,
        귀속골드: int,
        입장레벨: float = 0.0,
        딜러정원: int = DEFAULT_DEALER_SLOTS,
        서포터정원: int = DEFAULT_SUPPORT_SLOTS,
    ):
        key = (레이드.strip(), 난이도.strip())

        if key not in self.raid_data:
            return await interaction.response.send_message(
                f"❌ 존재하지 않는 조합이에요: **{key[0]} - {key[1]}**\n"
                f"새로 등록하시려면 `/클골추가`를 사용해주세요.",
                ephemeral=True
            )

        총합 = 골드 + 귀속골드
        self.raid_data[key] = (골드, 귀속골드, 총합, 입장레벨, 딜러정원, 서포터정원)
        save_raid_data(self.raid_data)

        embed = discord.Embed(
            title="✏️ 클골 수정 완료",
            description=f"**{key[0]} ({key[1]})**",
            color=discord.Color.blurple()
        )
        embed.add_field(name="골드", value=f"**{골드:,}🪙**", inline=True)
        embed.add_field(name="귀속골드", value=f"**{귀속골드:,}🪙**", inline=True)
        embed.add_field(name="총합", value=f"**{총합:,}🪙**", inline=False)
        if 입장레벨:
            embed.add_field(name="입장레벨", value=f"**{입장레벨:,.2f}**", inline=True)
        embed.add_field(name="파티 정원", value=f"딜러 {딜러정원} / 서포터 {서포터정원}", inline=True)

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