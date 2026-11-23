# translator/commands.py
#
# Cog de Discord con todos los comandos del modulo de traduccion.
# Se carga en bot_client.py:
#   await bot.load_extension("translator.commands")
#
# Comandos disponibles:
#   /translate join              - Entrar al canal en modo traduccion
#   /translate leave             - Salir del modo traduccion
#   /translate lang [codigo]     - Configurar tu idioma destino
#   /translate voice [nombre]    - Elegir voz TTS
#   /translate status            - Ver quien esta traduciendo y en que idioma
#   /translate history           - Ver tus ultimas traducciones
#   /translate glossary add      - Agregar correccion personal
#   /translate glossary remove   - Eliminar correccion
#   /translate glossary list     - Ver tu glosario

import logging
import discord
from discord.ext import commands
from discord import app_commands

from translator import manager
from utils.voice_session_manager import (
    VoiceClientUpgradeBlockedError,
    VoiceReceiveUnavailableError,
)
from translator.profile import (
    SUPPORTED_LANGUAGES,
    get_lang_name,
    get_voice_for_lang,
    is_valid_lang,
)
from translator.database import (
    get_user_settings,
    save_user_settings,
    add_glossary_entry,
    remove_glossary_entry,
    get_glossary,
    get_translation_history,
    init_db,
)
from translator.tts import list_voices_for_lang

logger = logging.getLogger("musicbot.translator.commands")


class TranslatorCog(commands.Cog, name="Translator"):
    """Modulo de traduccion en tiempo real por voz."""

    def __init__(self, bot: discord.Client):
        self.bot = bot

    translate_group = app_commands.Group(
        name="translate",
        description="Comandos de traduccion en tiempo real por voz",
    )

    # ── /translate join ───────────────────────────────────────

    @translate_group.command(name="join", description="Inicia la traduccion en tu canal de voz")
    async def translate_join(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            await interaction.response.send_message(
                "Necesitas estar en un canal de voz primero.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        guild_id = interaction.guild_id
        session = await manager.get_or_create_session(guild_id, self.bot)

        settings = await get_user_settings(interaction.user.id)
        target_lang = settings.get("target_lang", "es")

        try:
            await session.start(
                voice_channel=interaction.user.voice.channel,
                text_channel_id=interaction.channel_id,
            )
        except (VoiceReceiveUnavailableError, VoiceClientUpgradeBlockedError) as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        session.add_user(interaction.user.id)

        lang_name = get_lang_name(target_lang)
        await interaction.followup.send(
            f"Escuchando y traduciendo a **{lang_name}**.\n"
            f"Habla normalmente — detecto el idioma automaticamente.\n"
            f"Usa `/translate leave` para salir."
        )

    # ── /translate leave ──────────────────────────────────────

    @translate_group.command(name="leave", description="Desactiva la traduccion en tiempo real")
    async def translate_leave(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        guild_id = interaction.guild_id
        session = await manager.get_session(guild_id)

        if not session or not session.is_active:
            await interaction.followup.send("No hay ninguna sesion de traduccion activa.", ephemeral=True)
            return

        session.remove_user(interaction.user.id)

        # Si no quedan usuarios activos, terminar la sesion completamente
        if not session._active_users:
            await manager.destroy_session(guild_id)
            await interaction.followup.send("Sesion de traduccion finalizada.")
        else:
            await interaction.followup.send("Dejaste el modo de traduccion.")

    # ── /translate lang ───────────────────────────────────────

    @translate_group.command(name="lang", description="Configura el idioma al que quieres escuchar")
    @app_commands.describe(language="Codigo de idioma: es, en, fr, de, it, pt, ja, ko, zh, ru...")
    async def translate_lang(self, interaction: discord.Interaction, language: str):
        lang = language.lower().strip()
        if not is_valid_lang(lang):
            supported = ", ".join(f"`{k}` ({v})" for k, v in list(SUPPORTED_LANGUAGES.items())[:8])
            await interaction.response.send_message(
                f"Idioma no reconocido. Idiomas soportados:\n{supported}\n... y mas.",
                ephemeral=True,
            )
            return

        await save_user_settings(
            interaction.user.id,
            target_lang=lang,
            voice_name=get_voice_for_lang(lang),
        )

        await interaction.response.send_message(
            f"Tu idioma destino ahora es **{get_lang_name(lang)}** (`{lang}`).\n"
            f"Voz TTS: `{get_voice_for_lang(lang)}`"
        )

    # ── /translate voice ──────────────────────────────────────

    @translate_group.command(name="voice", description="Elige la voz TTS para las traducciones")
    @app_commands.describe(voice="Nombre de voz edge-tts (ej: es-MX-JorgeNeural)")
    async def translate_voice(self, interaction: discord.Interaction, voice: str):
        await save_user_settings(interaction.user.id, voice_name=voice)
        await interaction.response.send_message(
            f"Voz TTS configurada: `{voice}`", ephemeral=True
        )

    @translate_group.command(name="voices", description="Lista voces TTS disponibles para un idioma")
    @app_commands.describe(language="Codigo de idioma (ej: es, en, fr)")
    async def translate_voices(self, interaction: discord.Interaction, language: str):
        await interaction.response.defer(thinking=True, ephemeral=True)
        voices = await list_voices_for_lang(language.lower())
        if not voices:
            await interaction.followup.send(f"No encontre voces para `{language}`.", ephemeral=True)
            return

        lines = [f"`{v['ShortName']}` — {v.get('Gender', '?')}" for v in voices[:15]]
        await interaction.followup.send(
            f"**Voces disponibles para `{language}`:**\n" + "\n".join(lines),
            ephemeral=True,
        )

    # ── /translate status ─────────────────────────────────────

    @translate_group.command(name="status", description="Ver el estado actual de la traduccion")
    async def translate_status(self, interaction: discord.Interaction):
        session = await manager.get_session(interaction.guild_id)
        if not session or not session.is_active:
            await interaction.response.send_message("No hay sesion de traduccion activa.", ephemeral=True)
            return

        embed = discord.Embed(title="Translator — Estado", color=discord.Color.blurple())

        if session._active_users:
            lines = []
            for uid in session._active_users:
                s = await get_user_settings(uid)
                lang = get_lang_name(s.get("target_lang", "es"))
                lines.append(f"<@{uid}> → **{lang}**")
            embed.add_field(name="Usuarios activos", value="\n".join(lines), inline=False)
        else:
            embed.description = "Sesion activa pero sin usuarios configurados."

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /translate history ────────────────────────────────────

    @translate_group.command(name="history", description="Ver tus ultimas traducciones")
    async def translate_history(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        history = await get_translation_history(interaction.user.id, limit=8)

        if not history:
            await interaction.followup.send("No tienes historial de traducciones.", ephemeral=True)
            return

        embed = discord.Embed(title="Tus ultimas traducciones", color=discord.Color.blurple())
        for entry in history[:5]:
            embed.add_field(
                name=f"{entry['detected_lang'].upper()} -> {entry['target_lang'].upper()}",
                value=f"*{entry['original'][:60]}*\n**{entry['translated'][:60]}**",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /translate glossary add ───────────────────────────────

    @translate_group.command(name="glossary_add", description="Agrega una correccion personal de traduccion")
    @app_commands.describe(
        original="Palabra o frase original (como la dices tu)",
        corrected="Como debe traducirse esa palabra/frase",
    )
    async def translate_glossary_add(
        self, interaction: discord.Interaction, original: str, corrected: str
    ):
        settings = await get_user_settings(interaction.user.id)
        tgt_lang = settings.get("target_lang", "es")

        await add_glossary_entry(
            user_id=interaction.user.id,
            original=original,
            translation=corrected,
            tgt_lang=tgt_lang,
        )
        await interaction.response.send_message(
            f"Correccion guardada: `{original}` -> `{corrected}` (para `{tgt_lang}`)\n"
            f"El bot aplicara esta correccion automaticamente en tus traducciones.",
            ephemeral=True,
        )

    @translate_group.command(name="glossary_remove", description="Elimina una correccion de tu glosario")
    @app_commands.describe(original="Palabra o frase a eliminar del glosario")
    async def translate_glossary_remove(self, interaction: discord.Interaction, original: str):
        settings = await get_user_settings(interaction.user.id)
        tgt_lang = settings.get("target_lang", "es")
        await remove_glossary_entry(interaction.user.id, original, tgt_lang)
        await interaction.response.send_message(
            f"Entrada `{original}` eliminada del glosario.", ephemeral=True
        )

    @translate_group.command(name="glossary_list", description="Muestra tu glosario personal de traducciones")
    async def translate_glossary_list(self, interaction: discord.Interaction):
        settings = await get_user_settings(interaction.user.id)
        tgt_lang = settings.get("target_lang", "es")
        glossary = await get_glossary(interaction.user.id, tgt_lang)

        if not glossary:
            await interaction.response.send_message(
                "No tienes entradas en tu glosario.", ephemeral=True
            )
            return

        lines = [
            f"`{e['original']}` -> `{e['translation']}` (usado {e['usage_count']}x)"
            for e in glossary[:15]
        ]
        embed = discord.Embed(
            title=f"Tu glosario ({tgt_lang})",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: discord.Client):
    """Punto de entrada para bot.load_extension()."""
    await init_db()
    cog = TranslatorCog(bot)
    await bot.add_cog(cog)
    bot.tree.add_command(cog.translate_group)
    logger.info("TranslatorCog cargado.")
