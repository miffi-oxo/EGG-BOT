import discord
from discord import app_commands
from discord.ext import commands
from core.tts_channels import tts_channel_manager
from core.tts_engine_manager import tts_engine_manager
import asyncio

from handler.tts import (
    _ffmpeg_from_bytes,
    add_guild_custom_sound,
    get_guild_custom_sounds,
    remove_guild_custom_sound,
    join_voice_channel,
    disconnect_from_guild,
    AUDIO_BASE_PATH,
    force_reset_guild_tts
)
import os
import re

try:
    from core.tts_engine_manager import tts_engine_manager  # type: ignore
except Exception:
    tts_engine_manager = None  # type: ignore

ENGINE_LABELS = {
    "engine1": "여성 목소리 - 1 (Default)",
    "engine2": "여성 목소리 - 2 (SH)",
    "engine3": "남성 목소리 - 1 (TT)",
    "engine4": "여성 목소리 - 3 (TT)",
    "engine5": "남성 목소리 - 3 (TT)",
    "engine7": "남성 목소리 - 2 (HM)",
    "engine9": "남성 목소리 - 4 (IJ)",
}
ENGINE_CHOICES = [
    app_commands.Choice(name=f"{k} - {v}", value=k)
    for k, v in ENGINE_LABELS.items()
]

THUMB_URL = "https://img1.daumcdn.net/thumb/R1280x0/?scode=mtistory2&fname=https%3A%2F%2Fblog.kakaocdn.net%2Fdna%2FcMbtCx%2FdJMcaffaK08%2FAAAAAAAAAAAAAAAAAAAAAInK0bCD2uVP_oh4_fWrH-ZkFm8RzWxVAXiQu2qPIn2C%2Fimg.png%3Fcredential%3DyqXZFxpELC7KVnFOS48ylbz2pIh7yKj8%26expires%3D1777561199%26allow_ip%3D%26allow_referer%3D%26signature%3DRSXvh37hScYRb16sltwD1eiFvzE%253D"


class TTSCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.auto_leave_tasks = {}  # guild_id -> task

    async def _auto_leave_check(self, guild: discord.Guild, vc: discord.VoiceClient):
        await asyncio.sleep(5)  # 5초 대기 (원하면 조정)

        # 다시 확인
        if not vc or not vc.is_connected():
            return

        channel = vc.channel
        if not channel:
            return

        # 봇 제외 사람 수 체크
        human_count = sum(
            1 for m in channel.members if not m.bot
        )

        if human_count == 0:
            try:
                await vc.disconnect()
                print(f"[TTS] 자동 퇴장: {guild.id}")
            except Exception as e:
                print("[TTS] auto leave error:", e)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return

        guild = member.guild
        vc = discord.utils.get(self.bot.voice_clients, guild=guild)

        if not vc or not vc.is_connected():
            return

        # 봇이 있는 채널
        bot_channel = vc.channel
        if not bot_channel:
            return

        # 사람이 아직 있으면 취소
        human_count = sum(1 for m in bot_channel.members if not m.bot)

        if human_count == 0:
            # 기존 task 있으면 취소
            task = self.auto_leave_tasks.get(guild.id)
            if task:
                task.cancel()

            # 새로 타이머 시작
            task = asyncio.create_task(self._auto_leave_check(guild, vc))
            self.auto_leave_tasks[guild.id] = task

    def _engine_desc(self, engine_id: str) -> str:
        return ENGINE_LABELS.get(engine_id, engine_id)

    def _embed_to_text(self, embed: discord.Embed) -> str:
        parts = []
        if embed.title:
            parts.append(str(embed.title))
        if embed.description:
            parts.append(str(embed.description))
        for f in getattr(embed, "fields", []) or []:
            name = getattr(f, "name", "")
            value = getattr(f, "value", "")
            if name or value:
                parts.append(f"{name}\n{value}".strip())
        return "\n".join([p for p in parts if p]).strip() or "요청을 처리할 수 없습니다."

    async def _safe_respond(self, interaction: discord.Interaction, *, content: str | None = None, embed: discord.Embed | None = None, ephemeral: bool = True):
        try:
            return await interaction.respond(content=content, embed=embed, ephemeral=ephemeral)
        except Exception:
            try:
                return await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
            except discord.Forbidden:
                if embed and not content:
                    try:
                        return await interaction.followup.send(content=self._embed_to_text(embed), ephemeral=ephemeral)
                    except Exception:
                        return None
                return None
            except Exception:
                return None

    async def _safe_edit_original(self, interaction: discord.Interaction, *, content: str | None = None, embed: discord.Embed | None = None, ephemeral_fallback: bool = True):
        try:
            return await interaction.edit_original_response(content=content, embed=embed)
        except discord.Forbidden:
            if embed and not content:
                return await self._safe_respond(interaction, content=self._embed_to_text(embed), ephemeral=ephemeral_fallback)
            return await self._safe_respond(interaction, content=content, embed=embed, ephemeral=ephemeral_fallback)
        except Exception:
            return await self._safe_respond(interaction, content=content, embed=embed, ephemeral=ephemeral_fallback)

    def _make_embed(self, title: str, description: str, color: int) -> discord.Embed:
        embed = discord.Embed(title=title, description=description, color=color)
        embed.set_thumbnail(url=THUMB_URL)
        return embed

    def _bot_member(self, guild: discord.Guild) -> discord.Member | None:
        me = getattr(guild, "me", None)
        if me:
            return me
        try:
            return guild.get_member(self.bot.user.id) if self.bot.user else None
        except Exception:
            return None

    def _missing_perms_text(self, missing: list[str]) -> str:
        if not missing:
            return "권한이 부족합니다."
        return "봇 권한이 부족합니다: " + ", ".join(missing)

    async def _handle_perm_error(self, interaction: discord.Interaction, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await self._safe_respond(interaction, content="권한이 없어 해당 명령어 사용이 불가능해요!", ephemeral=True)
            return
        if isinstance(error, commands.BotMissingPermissions):
            miss = getattr(error, "missing_permissions", None) or []
            txt = "봇 권한이 부족합니다: " + (", ".join(miss) if miss else "필수 권한")
            await self._safe_respond(interaction, content=txt, ephemeral=True)
            return
        if isinstance(error, commands.CheckFailure):
            await self._safe_respond(interaction, content="권한이 없어 해당 명령어 사용이 불가능해요!", ephemeral=True)
            return
        await self._safe_respond(interaction, content="처리 중 오류가 발생했습니다.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        print("[TTS DEBUG] on_message triggered")  # <- 반드시 확인용

        if message.author.bot:
            return

        guild = message.guild
        if not guild:
            return

        channel_id = tts_channel_manager.get_channel(guild.id)
        if channel_id != message.channel.id:
            return

        try:
            from handler.tts import handle_tts_message
            await handle_tts_message(message)
        except Exception as e:
            print("TTS 실행 오류:", e)

    @app_commands.command(name="엔진", description="사용자별 TTS 목소리 엔진을 설정하거나 조회합니다.")
    @app_commands.describe(voice="설정할 엔진을 선택하세요.")
    @app_commands.choices(voice=ENGINE_CHOICES)
    async def tts_engine(self, interaction: discord.Interaction, voice: str = None):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        # ✅ 조회
        if not voice:
            try:
                engine_id = tts_engine_manager.get_engine(user_id)
            except Exception:
                engine_id = "engine1"

            await interaction.followup.send(
                f"현재 TTS 엔진은 **{engine_id}** ({self._engine_desc(engine_id)}) 입니다.",
                ephemeral=True
            )
            return

        # ✅ 설정
        engine_id = voice

        try:
            tts_engine_manager.set_engine(user_id, engine_id)
        except Exception as e:
            await interaction.followup.send(f"엔진 설정 중 오류 발생: {e}", ephemeral=True)
            return

        await interaction.followup.send(
            f"✅ 엔진이 **{engine_id}** ({self._engine_desc(engine_id)}) 으로 설정되었습니다.",
            ephemeral=True
        )

        engine_id = voice
        
        tts_engine_manager.set_engine(user_id, engine_id)

        await interaction.followup.send(
            f"✅ 엔진이 **{engine_id}** ({self._engine_desc(engine_id)}) 으로 설정되었습니다.",
            ephemeral=True
        )

    @app_commands.command(name="tts", description="계란봇 TTS 채널을 지정해요.")
    @app_commands.describe(채널="TTS를 사용할 채널을 선택하세요.")
    @app_commands.default_permissions(administrator=True)
    async def tts_set(self, interaction: discord.Interaction, 채널: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await self._safe_edit_original(interaction, content="이 명령어는 서버에서만 사용할 수 있습니다.")
            return

        me = self._bot_member(guild)
        missing = []
        if me is not None:
            perms = 채널.permissions_for(me)
            if not perms.view_channel:
                missing.append("채널 보기(View Channel)")
            if not perms.send_messages:
                missing.append("메시지 보내기(Send Messages)")
            if not perms.embed_links:
                missing.append("임베드 링크(Embed Links)")
        if missing:
            embed = self._make_embed("❌ TTS 채널 설정 실패", self._missing_perms_text(missing), 0xFF6B6B)
            await self._safe_edit_original(interaction, embed=embed)
            return

        try:
            success = await tts_channel_manager.set_channel(guild.id, 채널.id)
        except Exception as e:
            embed = self._make_embed("❌ TTS 채널 설정 실패", f"오류가 발생했습니다: {e}", 0xFF6B6B)
            print("TTS 채널 설정 오류:", e) 
            await self._safe_edit_original(interaction, embed=embed)
            return

        if success:
            embed = self._make_embed(
                "✅ TTS 채널 설정 완료",
                f"{채널.mention} 채널이 TTS 채널로 설정되었습니다.\n해당 채널에서 음성 채널에 있는 사용자의 메시지를 읽어드려요!",
                0x2ECC71,
            )
            await self._safe_edit_original(interaction, embed=embed)
            return

        embed = self._make_embed("❌ TTS 채널 설정 실패", "TTS 채널 설정 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.", 0xFF6B6B)
        await self._safe_edit_original(interaction, embed=embed)

    @app_commands.command(name="join", description="현재 들어가 있는 음성채널로 봇을 불러오고, 이 텍스트 채널을 임시 TTS 채널로 지정합니다.")
    async def tts_join(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("이 명령어는 서버에서만 사용할 수 있습니다.", ephemeral=True)
            return

        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.followup.send("먼저 음성 채널에 들어가 주세요.", ephemeral=True)
            return

        voice_ch: discord.VoiceChannel = interaction.user.voice.channel
        me = self._bot_member(guild)
        if me is not None:
            vperms = voice_ch.permissions_for(me)
            missing = []
            if not vperms.connect:
                missing.append("연결(Connect)")
            if not vperms.speak:
                missing.append("말하기(Speak)")
            if missing:
                await interaction.followup.send("봇 권한이 부족합니다: " + ", ".join(missing), ephemeral=True)
                return

        try:
            tts_channel_manager.set_override(guild.id, interaction.channel.id, by_user_id=interaction.user.id)
        except Exception:
            pass

        try:
            vc, join_embed = await join_voice_channel(voice_ch, guild.id)
        except discord.Forbidden:
            await interaction.followup.send("음성 채널에 연결할 수 없습니다. 봇 권한(연결/말하기)을 확인해 주세요.", ephemeral=True)
            return
        except Exception:
            await interaction.followup.send("음성 채널에 연결할 수 없습니다. 권한(연결/말하기) 또는 FFmpeg를 확인해 주세요.", ephemeral=True)
            return

        if not vc:
            await interaction.followup.send("음성 채널에 연결할 수 없습니다. 권한(연결/말하기) 또는 FFmpeg를 확인해 주세요.", ephemeral=True)
            return

        try:
            if join_embed:
                await interaction.channel.send(embed=join_embed)
        except discord.Forbidden:
            pass
        except Exception:
            pass

        await interaction.followup.send("이 채널에서 보내는 메시지를 읽을게요. (임시 TTS 채널, 설정보다 우선)", ephemeral=True)

    @app_commands.command(name="leave", description="TTS를 종료하고 음성채널에서 나갑니다. (임시 TTS 채널 해제)")
    async def tts_leave(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("이 명령어는 서버에서만 사용할 수 있습니다.", ephemeral=True)
            return
        try:
            tts_channel_manager.clear_override(guild.id)
        except Exception:
            pass
        try:
            await disconnect_from_guild(guild.id)
        except Exception:
            pass
        await interaction.followup.send("음성채널에서 나갔어요. (임시 TTS 채널 해제됨)", ephemeral=True)

    @app_commands.command(name="tts해제", description="TTS 기능을 해제합니다.")
    @app_commands.default_permissions(administrator=True)
    async def tts_remove(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await self._safe_edit_original(interaction, content="이 명령어는 서버에서만 사용할 수 있습니다.")
            return

        try:
            current_channel_id = tts_channel_manager.get_channel(guild.id)
        except Exception:
            current_channel_id = None

        if not current_channel_id:
            embed = self._make_embed("⚠️ TTS 채널 없음", "현재 설정된 TTS 채널이 없습니다.", 0xFFA726)
            await self._safe_edit_original(interaction, embed=embed)
            return

        try:
            success = await tts_channel_manager.remove_channel(guild.id)
        except Exception as e:
            embed = self._make_embed("❌ TTS 채널 해제 실패", f"오류가 발생했습니다: {e}", 0xFF6B6B)
            await self._safe_edit_original(interaction, embed=embed)
            return

        if success:
            embed = self._make_embed("✅ TTS 채널 해제 완료", "TTS 채널 설정이 해제되었습니다.", 0x2ECC71)
            await self._safe_edit_original(interaction, embed=embed)
            return

        embed = self._make_embed("❌ TTS 채널 해제 실패", "TTS 채널 해제 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.", 0xFF6B6B)
        await self._safe_edit_original(interaction, embed=embed)

    @app_commands.command(name="tts정보", description="현재 TTS 채널 설정을 확인합니다.")
    async def tts_info(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await self._safe_respond(interaction, content="이 명령어는 서버에서만 사용할 수 있습니다.", ephemeral=True)
            return

        try:
            channel_id = tts_channel_manager.get_channel(guild.id)
        except Exception:
            channel_id = None

        if channel_id:
            channel = guild.get_channel(channel_id)
            if channel:
                embed = self._make_embed(
                    "🎤 TTS 채널 정보",
                    f"현재 TTS 채널: {channel.mention}\n해당 채널에서 음성 채널에 있는 사용자의 메시지를 읽어드려요!",
                    0x2ECC71,
                )
            else:
                embed = self._make_embed(
                    "⚠️ TTS 채널 오류",
                    "설정된 TTS 채널을 찾을 수 없습니다. 채널이 삭제되었거나 봇이 접근할 수 없습니다.",
                    0xFFA726,
                )
        else:
            embed = self._make_embed(
                "📢 TTS 채널 없음",
                "현재 설정된 TTS 채널이 없습니다.\n`/tts` 명령어로 TTS 채널을 설정해주세요.",
                0x95A5A6,
            )

        await self._safe_respond(interaction, embed=embed, ephemeral=True)

    @app_commands.command(name="tts해결", description="TTS 사용 중 문제가 발생했을 때 상태를 초기화합니다.")
    @app_commands.default_permissions(administrator=True)
    async def tts_fix(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("이 명령어는 길드(서버) 내에서만 사용할 수 있습니다.", ephemeral=True)
            return
        try:
            await force_reset_guild_tts(guild=guild, reason=f"manual:{interaction.user.id}")
        except Exception:
            pass
        try:
            tts_channel_manager.clear_override(guild.id)
        except Exception:
            pass
        embed = self._make_embed("🔧 TTS 상태 초기화", "TTS 관련 상태를 초기화했습니다. 다시 메시지를 보내 TTS를 사용해 보세요.", 0x3498DB)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="tts커스텀", description="서버별 커스텀 TTS를 추가/삭제하거나 현재 상태를 조회합니다.")
    @app_commands.describe(
        옵션="추가 또는 삭제를 선택하세요.",
        별명="추가하거나 삭제할 커스텀 사운드의 별명을 입력하세요.",
        파일="추가할 MP3 파일 (최대 25MB, 10초 이하)"
    )
    @app_commands.choices(
        옵션=[
            app_commands.Choice(name="추가", value="add"),
            app_commands.Choice(name="삭제", value="remove"),
        ]
    )
    @commands.has_permissions(administrator=True)
    async def tts_custom(self, interaction: discord.Interaction, 옵션: str = None, 별명: str = None, 파일: discord.Attachment = None):
        guild = interaction.guild
        if guild is None:
            await self._safe_respond(interaction, content="이 명령어는 서버에서만 사용할 수 있습니다.", ephemeral=True)
            return
        gid = guild.id
        await interaction.response.defer(ephemeral=True)

        if not 옵션:
            items = get_guild_custom_sounds(gid)
            if items:
                lines = []
                for item in items:
                    aliases = ", ".join(str(a) for a in item.get("aliases", []) if a)
                    lines.append(f"• **{item['key']}** (별명: {aliases})")
                desc = "\n".join(lines)
            else:
                desc = "등록된 커스텀 사운드가 없습니다.\n`옵션`을 \"추가\"로 지정하여 새 음성파일을 등록할 수 있습니다."
            warn = "각 서버는 최대 **8개**의 커스텀 사운드를 등록할 수 있으며, 각 음성 파일은 **10초 이하**여야 해요."
            embed = discord.Embed(title="🎵 현재 커스텀 TTS 목록", description=desc, color=0x3498DB)
            embed.set_thumbnail(url=THUMB_URL)
            embed.add_field(name="⚠️ 제한 사항", value=warn, inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if 옵션 == "add":
            if not 별명 or not 파일:
                await interaction.followup.send("별명과 파일을 모두 제공해야 합니다.", ephemeral=True)
                return

            alias_str = str(별명).strip()
            if alias_str.startswith("[") and alias_str.endswith("]"):
                alias_str = alias_str[1:-1].strip()
            if not alias_str:
                await interaction.followup.send("별명이 유효하지 않습니다.", ephemeral=True)
                return

            existing = get_guild_custom_sounds(gid)
            if any(item.get("key") == alias_str for item in existing):
                await interaction.followup.send(f"이미 `{alias_str}` 별명이 등록되어 있습니다.", ephemeral=True)
                return
            if len(existing) >= 8:
                await interaction.followup.send("커스텀 사운드는 최대 8개까지 등록할 수 있습니다.", ephemeral=True)
                return

            try:
                if 파일.size > 25 * 1024 * 1024:
                    await interaction.followup.send("파일 크기가 25MB를 초과합니다.", ephemeral=True)
                    return
            except Exception:
                pass

            file_ext = (파일.filename or "").rsplit(".", 1)[-1].lower()
            if file_ext != "mp3":
                await interaction.followup.send("MP3 파일만 지원합니다.", ephemeral=True)
                return

            try:
                mp3_bytes = await 파일.read()
            except Exception as e:
                await interaction.followup.send(f"파일을 읽을 수 없습니다: {e}", ephemeral=True)
                return

            try:
                audio_data, _ = await _ffmpeg_from_bytes(mp3_bytes, volume=1.0, trim_tail=False)
                if not audio_data:
                    raise RuntimeError("오디오 변환 실패")
                length_sec = len(audio_data) / (48000 * 2 * 2)
                if length_sec > 10.0:
                    await interaction.followup.send("오디오 길이가 10초를 초과합니다.", ephemeral=True)
                    return
            except Exception:
                await interaction.followup.send("오디오 파일을 분석하는 중 오류가 발생했습니다. 다른 파일을 사용해 주세요.", ephemeral=True)
                return

            base_dir = AUDIO_BASE_PATH
            guild_dir = os.path.join(base_dir, str(gid))
            try:
                os.makedirs(guild_dir, exist_ok=True)
            except Exception as e:
                await interaction.followup.send(f"디렉토리 생성 중 오류가 발생했습니다: {e}", ephemeral=True)
                return

            safe_name = re.sub(r"[^\w가-힣_-]", "_", alias_str)
            file_name = f"{safe_name}.mp3"
            file_path = os.path.join(guild_dir, file_name)

            try:
                with open(file_path, "wb") as f:
                    f.write(mp3_bytes)
            except Exception as e:
                await interaction.followup.send(f"파일 저장 중 오류가 발생했습니다: {e}", ephemeral=True)
                return

            aliases = [alias_str, f"[{alias_str}]"]
            try:
                add_guild_custom_sound(gid, alias_str, file_name, aliases)
            except Exception as e:
                await interaction.followup.send(f"커스텀 사운드 등록 중 오류가 발생했습니다: {e}", ephemeral=True)
                return

            embed = discord.Embed(
                title="✅ 커스텀 사운드 추가 완료",
                description=f"`{alias_str}` 별명의 커스텀 사운드가 등록되었습니다!\nTTS 채팅에서 `[ {alias_str} ]` 라고 입력하면 이 음성이 재생됩니다.",
                color=0x2ECC71,
            )
            embed.set_thumbnail(url=THUMB_URL)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if 옵션 == "remove":
            if not 별명:
                await interaction.followup.send("삭제할 별명을 입력해주세요.", ephemeral=True)
                return

            alias_str = str(별명).strip()
            if alias_str.startswith("[") and alias_str.endswith("]"):
                alias_str = alias_str[1:-1].strip()

            existing = get_guild_custom_sounds(gid)
            if not any(item.get("key") == alias_str for item in existing):
                await interaction.followup.send("해당 별명의 커스텀 사운드가 존재하지 않습니다.", ephemeral=True)
                return

            try:
                remove_guild_custom_sound(gid, alias_str)
            except Exception as e:
                await interaction.followup.send(f"커스텀 사운드 삭제 중 오류가 발생했습니다: {e}", ephemeral=True)
                return

            embed = discord.Embed(title="🗑️ 커스텀 사운드 삭제 완료", description=f"`{alias_str}` 별명의 커스텀 사운드가 삭제되었습니다.", color=0xFF6B6B)
            embed.set_thumbnail(url=THUMB_URL)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        await interaction.followup.send("알 수 없는 옵션입니다. 옵션을 생략하거나 \"추가\", \"삭제\" 중 하나를 선택하세요.", ephemeral=True)

    @tts_set.error
    async def tts_set_error(self, interaction: discord.Interaction, error):
        await self._handle_perm_error(interaction, error)

    @tts_remove.error
    async def tts_remove_error(self, interaction: discord.Interaction, error):
        await self._handle_perm_error(interaction, error)

    @tts_custom.error
    async def tts_custom_error(self, interaction: discord.Interaction, error):
        await self._handle_perm_error(interaction, error)

async def setup(bot):
    await bot.add_cog(TTSCog(bot))