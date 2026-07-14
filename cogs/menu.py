import discord
from discord.ext import commands
from discord import app_commands
import random
from pathlib import Path

OWNER_ID = 1188894271894462514


def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == OWNER_ID


# =========================
# 리롤 버튼
# =========================
class RerollView(discord.ui.View):
    def __init__(self, cog, author_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.author_id = author_id
        self.reroll_count = 0
        self.history: list[str] = []
        self.message: discord.Message | None = None

    # -------------------------
    def pick_menu(self):
        menus = self.cog.load_menus()
        if not menus:
            return None

        choice = random.choice(menus)
        self.history.append(choice)
        return choice

    # -------------------------
    def build_live_message(self) -> str | None:
        if not self.history:
            return None

        current = self.history[-1]

        return (
            f"🍽️ 오늘의 추천 메뉴는 **{current}**입니다, 마스터 🧐\n"
            f"└ 🎲 리롤: {self.reroll_count}회"
        )

    # -------------------------
    def build_final_message(self) -> str | None:
        if not self.history:
            return None

        current = self.history[-1]
        history_text = ", ".join(self.history)

        base = (
            f"🍽️ 오늘의 추천 메뉴는 **{current}**입니다, 마스터 🧐\n"
            f"└ 🎲 리롤: {self.reroll_count}회\n\n"
            f"📜 추천 기록\n{history_text}\n"
        )

        # 🔥 리롤 했을 때만 멘트 출력
        if self.reroll_count > 0:
            base += "\n_이쯤 되면 그냥 드십시오, 마스터._"

        return base

    # -------------------------
    async def on_timeout(self):
        if not self.message:
            return

        try:
            msg = self.build_final_message()

            if msg is None:
                msg = "메뉴가 비어있어요 😢"

            await self.message.edit(
                content=msg,
                view=None
            )
        except:
            pass

    # -------------------------
    @discord.ui.button(label="🎲 리롤", style=discord.ButtonStyle.blurple)
    async def reroll(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                "⛔ 명령어를 사용하신 마스터만 사용하실 수 있습니다.",
                ephemeral=True
            )

        new_view = RerollView(self.cog, self.author_id)
        new_view.history = self.history.copy()
        new_view.reroll_count = self.reroll_count + 1

        new_view.pick_menu()

        await self.cog.edit_menu(interaction, new_view)


# =========================
# Cog
# =========================
class MenuCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.base_dir = Path(__file__).resolve().parent
        self.menu_path = self.base_dir / ".." / "core" / "menu.txt"

        self.menu_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.menu_path.exists():
            self.menu_path.write_text("", encoding="utf-8")

    # -------------------------
    def load_menus(self) -> list[str]:
        with open(self.menu_path, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]

    def save_menus(self, menus: list[str]):
        unique = sorted(set(m.strip() for m in menus if m.strip()))
        with open(self.menu_path, "w", encoding="utf-8") as f:
            f.write("\n".join(unique) + "\n" if unique else "")

    # -------------------------
    async def send_menu(self, interaction: discord.Interaction):
        view = RerollView(self, interaction.user.id)

        if view.pick_menu() is None:
            return await interaction.response.send_message("메뉴가 비어있어요 😢")

        msg = view.build_live_message()

        await interaction.response.send_message(msg, view=view)
        view.message = await interaction.original_response()

    async def edit_menu(self, interaction: discord.Interaction, view: RerollView):
        msg = view.build_live_message()

        if msg is None:
            return await interaction.response.edit_message(
                content="메뉴가 비어있어요 😢",
                view=None
            )

        await interaction.response.edit_message(content=msg, view=view)
        view.message = await interaction.original_response()

    # -------------------------
    @app_commands.command(name="점메추", description="오늘의 점심 메뉴 추천")
    async def recommend_menu(self, interaction: discord.Interaction):
        await self.send_menu(interaction)

    # -------------------------
    @app_commands.command(name="메뉴추가", description="메뉴 추가 (계란 전용)")
    @app_commands.check(is_owner)
    async def add_menu(self, interaction: discord.Interaction, menu: str):
        menu = menu.strip()
        menus = self.load_menus()

        if menu in menus:
            return await interaction.response.send_message(
                "⚠️ 이미 등록된 메뉴입니다.",
                ephemeral=True
            )

        menus.append(menu)
        self.save_menus(menus)

        await interaction.response.send_message(
            f"✅ 메뉴 추가 완료: **{menu}**",
            ephemeral=True
        )

    # -------------------------
    @app_commands.command(name="메뉴삭제", description="메뉴 삭제 (계란 전용)")
    @app_commands.check(is_owner)
    async def remove_menu(self, interaction: discord.Interaction, menu: str):
        menu = menu.strip()
        menus = self.load_menus()

        if menu not in menus:
            return await interaction.response.send_message(
                "❌ 해당 메뉴는 존재하지 않습니다.",
                ephemeral=True
            )

        menus.remove(menu)
        self.save_menus(menus)

        await interaction.response.send_message(
            f"🗑️ 메뉴 삭제 완료: **{menu}**",
            ephemeral=True
        )

    # -------------------------
    @app_commands.command(name="메뉴판", description="전체 메뉴 보기")
    async def menu_list(self, interaction: discord.Interaction):
        menus = self.load_menus()

        if not menus:
            return await interaction.response.send_message(
                "메뉴가 비어있어요 😢",
                ephemeral=True
            )

        menu_text = "\n".join(f"• {m}" for m in menus)

        embed = discord.Embed(
            title="🍽️ 메뉴판",
            description=menu_text,
            color=discord.Color.green()
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -------------------------
    @add_menu.error
    @remove_menu.error
    async def owner_only_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "⛔ 이 명령어는 계란 외에는 사용할 수 없습니다.",
                ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(MenuCog(bot))