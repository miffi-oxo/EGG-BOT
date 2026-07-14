import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import contextlib
from datetime import datetime, timezone, timedelta


class CleanCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------- 권한 체크 ----------------
    def is_admin(self, interaction: discord.Interaction) -> bool:
        return (
            interaction.guild is not None
            and interaction.user.guild_permissions.administrator
        )

    # ---------------- slash command ----------------
    @app_commands.command(
        name="청소",
        description="현재 채널의 채팅을 삭제합니다 (계란 전용)"
    )
    @app_commands.default_permissions(administrator=True)  # ⭐ 핵심
    @app_commands.describe(count="삭제할 개수 (비우면 전체)")
    async def clean(self, interaction: discord.Interaction, count: int | None = None):

        # ---------------- 서버 체크 ----------------
        if not interaction.guild:
            await interaction.response.send_message(
                "❌ 서버에서만 사용할 수 있어요.",
                ephemeral=True
            )
            return

        # ---------------- 관리자 체크 (2차 방어) ----------------
        if not self.is_admin(interaction):
            # 👉 이건 “혹시 권한 뚫고 들어온 경우” 방지용
            await interaction.response.send_message(
                "❌ 이 명령어는 관리자만 사용할 수 있어요.",
                ephemeral=True
            )
            return

        channel = interaction.channel

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "❌ 일반 채널 또는 스레드에서만 사용할 수 있어요.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        # ---------------- 스레드 복구 ----------------
        if isinstance(channel, discord.Thread) and channel.archived:
            with contextlib.suppress(Exception):
                await channel.edit(archived=False)

        cutoff = datetime.now(timezone.utc) - timedelta(days=14)

        targets = []
        recent_msgs = []
        old_msgs = []

        try:
            async for msg in channel.history(limit=None):

                if isinstance(channel, discord.Thread) and msg.id == channel.id:
                    continue

                targets.append(msg)

                if msg.created_at >= cutoff:
                    recent_msgs.append(msg)
                else:
                    old_msgs.append(msg)

                if count is not None and len(targets) >= count:
                    break

        except discord.Forbidden:
            await interaction.followup.send(
                "❌ 봇 권한이 부족합니다.",
                ephemeral=True
            )
            return

        if not targets:
            await interaction.followup.send(
                "🧹 삭제할 메시지가 없어요.",
                ephemeral=True
            )
            return

        deleted = 0

        # ---------------- 최근 메시지 bulk 삭제 ----------------
        for i in range(0, len(recent_msgs), 100):
            chunk = recent_msgs[i:i + 100]

            try:
                await channel.delete_messages(chunk)
                deleted += len(chunk)

            except discord.Forbidden:
                break

            except discord.HTTPException:
                for msg in chunk:
                    try:
                        await msg.delete()
                        deleted += 1
                        await asyncio.sleep(0.2)
                    except:
                        pass

            await asyncio.sleep(0.5)

        # ---------------- 오래된 메시지 ----------------
        for idx, msg in enumerate(old_msgs, 1):
            try:
                await msg.delete()
                deleted += 1
            except:
                pass

            if idx % 10 == 0:
                await asyncio.sleep(1)

        await interaction.followup.send(
            f"🧹 총 `{deleted}`개 메시지를 삭제했어요.",
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(CleanCog(bot))