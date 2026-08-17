# translator/manager.py
#
# Orquestador principal del modulo de traduccion.
#
# Se encarga de:
#   1. Gestionar la sesion de voz (VoiceClient) del traductor
#   2. Instalar el TranslatorAudioSink y conectar el pipeline
#   3. Coordinar STT -> Traduccion -> TTS -> Reproduccion
#   4. Guardar historial y alimentar el sistema de aprendizaje

import asyncio
import logging
import os
import discord

from translator import stt, translate as tr, tts
from translator.audio_sink import TranslatorAudioSink
from translator.profile import build_whisper_prompt, get_voice_for_lang
from translator.database import (
    get_user_settings,
    save_translation,
    init_db,
)

logger = logging.getLogger("musicbot.translator.manager")


class TranslatorSession:
    """
    Sesion de traduccion para un guild.
    Cada guild tiene su propia sesion con un VoiceClient dedicado.
    """

    def __init__(self, guild_id: int, bot: discord.Client):
        self.guild_id = guild_id
        self.bot = bot
        self.voice_client: discord.VoiceClient | None = None
        self.sink: TranslatorAudioSink | None = None
        self._active_users: set[int] = set()   # user_ids participando
        self._processing: set[int] = set()     # user_ids en proceso (evitar solapamiento)
        self._tts_queue: asyncio.Queue = asyncio.Queue()
        self._tts_task: asyncio.Task | None = None
        self._text_channel_id: int | None = None

    # ── Inicio y fin de sesion ────────────────────────────────

    async def start(self, voice_channel: discord.VoiceChannel, text_channel_id: int):
        """Une el bot al canal de voz e instala el AudioSink."""
        self._text_channel_id = text_channel_id

        # Conectar al canal de voz
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.move_to(voice_channel)
        else:
            self.voice_client = await voice_channel.connect()

        # Instalar el sink
        self.sink = TranslatorAudioSink(
            on_speech=self._on_speech,
            active_users=self._active_users if self._active_users else None,
        )
        self.voice_client.listen(self.sink)

        # Iniciar cola de TTS para reproduccion ordenada
        self._tts_task = asyncio.create_task(self._tts_player_loop())

        logger.info(f"[Guild {self.guild_id}] Sesion de traduccion iniciada en {voice_channel.name}")

    async def stop(self):
        """Desconecta el bot y limpia la sesion."""
        if self._tts_task:
            self._tts_task.cancel()
        if self.sink:
            self.sink.cleanup()
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect()
        self.voice_client = None
        self.sink = None
        self._active_users.clear()
        logger.info(f"[Guild {self.guild_id}] Sesion de traduccion detenida.")

    def add_user(self, user_id: int):
        self._active_users.add(user_id)
        if self.sink:
            self.sink.active_users = self._active_users

    def remove_user(self, user_id: int):
        self._active_users.discard(user_id)
        if self.sink:
            self.sink.active_users = self._active_users if self._active_users else None

    @property
    def is_active(self) -> bool:
        return self.voice_client is not None and self.voice_client.is_connected()

    # ── Pipeline STT -> TR -> TTS ─────────────────────────────

    async def _on_speech(self, user_id: int, pcm_bytes: bytes):
        """
        Callback del AudioSink. Se llama cuando un usuario termina de hablar.
        Ejecuta el pipeline completo: transcribir -> traducir -> sintetizar.
        """
        # Evitar procesar el mismo usuario dos veces al mismo tiempo
        if user_id in self._processing:
            return
        self._processing.add(user_id)

        try:
            # Obtener configuracion del usuario
            settings = await get_user_settings(user_id)
            target_lang = settings.get("target_lang", "es")

            # ── 1. STT con contexto adaptativo ──────────────
            audio_array = stt.preprocess_audio(pcm_bytes)
            whisper_prompt = await build_whisper_prompt(user_id)  # Aprendizaje
            stt_result = await stt.transcribe(audio_array, initial_prompt=whisper_prompt)

            text = stt_result["text"]
            detected_lang = stt_result["language"]
            confidence = stt_result["confidence"]

            if not text or confidence < 0.3:
                return

            logger.info(f"[{user_id}] STT ({detected_lang}): '{text}' (conf={confidence:.2f})")

            # No traducir si ya esta en el idioma destino
            if detected_lang == target_lang:
                return

            # ── 2. Traduccion con glosario personal ─────────
            tr_result = await tr.translate_text(
                text=text,
                target_lang=target_lang,
                source_lang=detected_lang,
                user_id=user_id,
            )
            translated = tr_result["translated"]

            if not translated:
                return

            logger.info(f"[{user_id}] TR ({detected_lang}->{target_lang}): '{translated}'")

            # Mostrar en canal de texto
            await self._send_to_text_channel(user_id, text, translated, detected_lang, target_lang)

            # ── 3. TTS ───────────────────────────────────────
            voice_name = settings.get("voice_name") or get_voice_for_lang(target_lang)
            audio_path = await tts.synthesize(translated, voice=voice_name)

            if audio_path:
                await self._tts_queue.put((audio_path, user_id))

            # ── 4. Guardar historial (alimenta el aprendizaje) ─
            await save_translation(
                user_id=user_id,
                guild_id=self.guild_id,
                original=text,
                detected_lang=detected_lang,
                translated=translated,
                target_lang=target_lang,
            )

        except Exception as e:
            logger.error(f"Error en pipeline de traduccion para {user_id}: {e}", exc_info=True)
        finally:
            self._processing.discard(user_id)

    # ── Reproduccion de TTS en cola ──────────────────────────

    async def _tts_player_loop(self):
        """
        Reproduce los audios de TTS en orden, sin solaparse.
        Usa una cola asyncio para serializar la reproduccion.
        """
        while True:
            try:
                audio_path, user_id = await self._tts_queue.get()

                if not self.voice_client or not self.voice_client.is_connected():
                    os.unlink(audio_path)
                    continue

                # Esperar si ya hay algo reproduciendose
                while self.voice_client.is_playing():
                    await asyncio.sleep(0.1)

                source = discord.FFmpegPCMAudio(audio_path)
                done = asyncio.Event()

                def _after(error):
                    try:
                        os.unlink(audio_path)
                    except Exception:
                        pass
                    done.set()

                self.voice_client.play(source, after=_after)
                await done.wait()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error en TTS player loop: {e}")

    async def _send_to_text_channel(
        self,
        user_id: int,
        original: str,
        translated: str,
        src_lang: str,
        tgt_lang: str,
    ):
        """Muestra la traduccion en el canal de texto."""
        if not self._text_channel_id:
            return
        guild = self.bot.get_guild(self.guild_id)
        if not guild:
            return
        channel = guild.get_channel(self._text_channel_id)
        if not channel:
            return

        try:
            embed = discord.Embed(color=discord.Color.blurple())
            embed.set_author(
                name=f"Translation ({src_lang.upper()} -> {tgt_lang.upper()})",
            )
            embed.add_field(name=f"Original ({src_lang})", value=f"*{original}*", inline=False)
            embed.add_field(name=f"Translated ({tgt_lang})", value=f"**{translated}**", inline=False)
            embed.set_footer(text=f"Speaker: <@{user_id}>")
            await channel.send(embed=embed)
        except Exception as e:
            logger.warning(f"No se pudo enviar embed de traduccion: {e}")


# ──────────────────────────────────────────────────────────────
# Registry global de sesiones (una por guild)
# ──────────────────────────────────────────────────────────────

_sessions: dict[int, TranslatorSession] = {}


async def get_or_create_session(guild_id: int, bot: discord.Client) -> TranslatorSession:
    if guild_id not in _sessions:
        await init_db()
        _sessions[guild_id] = TranslatorSession(guild_id, bot)
    return _sessions[guild_id]


async def get_session(guild_id: int) -> TranslatorSession | None:
    return _sessions.get(guild_id)


async def destroy_session(guild_id: int):
    session = _sessions.pop(guild_id, None)
    if session:
        await session.stop()
