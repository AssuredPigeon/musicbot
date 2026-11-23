# translator/manager.py
#
# Orquestador principal del modulo de traduccion (Bloque 0A v3.3.13).

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any

import discord

from translator import stt, translate as tr, tts
from translator.audio_sink import TranslatorAudioSink
from translator.database import (
    get_user_settings,
    init_db,
    save_translation,
)
from translator.profile import build_whisper_prompt, get_voice_for_lang
from utils.voice_session_manager import VoiceSessionManager, voice_session_manager

logger = logging.getLogger("musicbot.translator.manager")

TTS_MAX_HOLD_S = 60.0
MAX_DEFERRED_TTS = 5


class TranslatorSession:
    """
    Sesion de traduccion para un guild.
    Comparte el VoiceRecvClient autoritativo del guild a traves de VoiceSessionManager.
    """

    def __init__(
        self,
        guild_id: int,
        bot: discord.Client,
        *,
        session_manager: VoiceSessionManager | None = None,
    ):
        self.guild_id = guild_id
        self.bot = bot
        self._vsm: VoiceSessionManager = session_manager or voice_session_manager
        self.voice_client: discord.VoiceClient | None = None
        self.sink: TranslatorAudioSink | None = None
        self._session_token: int | None = None
        self._listening_active: bool = False
        self._revoked: bool = False
        self._active_users: set[int] = set()
        self._processing: set[int] = set()
        self._tts_queue: asyncio.Queue[tuple[str, int, float, int]] = asyncio.Queue(
            maxsize=MAX_DEFERRED_TTS
        )
        self._tts_task: asyncio.Task[Any] | None = None
        self._text_channel_id: int | None = None

        # Métricas fail-closed thread-safe en cierre de loop
        self._closed_loop_metrics_lock = threading.Lock()
        self.dropped_after_callbacks_on_closed_loop: int = 0
        self._incomplete_close_reconciliations: list[
            tuple[int, int | None, int | None, str, float]
        ] = []

    def _record_dropped_after_on_closed_loop(
        self, guild_id: int, song_token: int | None, pb_token: int | None, *, reason: str
    ) -> None:
        entry = (guild_id, song_token, pb_token, reason, time.monotonic())
        with self._closed_loop_metrics_lock:
            self.dropped_after_callbacks_on_closed_loop += 1
            self._incomplete_close_reconciliations.append(entry)
        self._vsm.record_dropped_after_on_closed_loop(
            guild_id, song_token, pb_token, reason=reason
        )

    def get_incomplete_close_reconciliations(
        self,
    ) -> list[tuple[int, int | None, int | None, str, float]]:
        with self._closed_loop_metrics_lock:
            return list(self._incomplete_close_reconciliations)

    def _get_music_player(self):
        return getattr(self.bot, "_music_player", None)

    def _can_play_tts(self) -> bool:
        if self._revoked or not self._listening_active:
            return False
        current_vc = self._vsm.voice_clients.get(self.guild_id) or self.voice_client
        if not current_vc or not current_vc.is_connected():
            return False
        music_player = self._get_music_player()
        if music_player is not None:
            return bool(music_player.can_play_auxiliary_audio(self.guild_id))
        return not current_vc.is_playing() and not current_vc.is_paused()

    @staticmethod
    def _unlink_silent(path: str | None) -> None:
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except Exception:
                pass

    def _enqueue_tts(self, audio_path: str, user_id: int, job_token: int) -> None:
        item = (audio_path, user_id, time.monotonic(), job_token)
        if self._tts_queue.full():
            try:
                old_path, _, _, _ = self._tts_queue.get_nowait()
                self._tts_queue.task_done()
                self._unlink_silent(old_path)
            except Exception:
                pass
        try:
            self._tts_queue.put_nowait(item)
        except asyncio.QueueFull:
            self._unlink_silent(audio_path)

    def _clear_tts_queue(self) -> None:
        while not self._tts_queue.empty():
            try:
                old_path, _, _, _ = self._tts_queue.get_nowait()
                self._tts_queue.task_done()
                self._unlink_silent(old_path)
            except Exception:
                break

    # ── Inicio y fin de sesion ────────────────────────────────

    async def start(self, voice_channel: discord.VoiceChannel, text_channel_id: int):
        """Une o reutiliza el VoiceRecvClient del guild e instala el TranslatorAudioSink."""
        self._text_channel_id = text_channel_id
        music_player = self._get_music_player()

        self.voice_client = await self._vsm.ensure_voice_client(
            voice_channel,
            require_receive=True,
            is_music_active_cb=music_player.is_music_active if music_player else None,
        )

        # Limpiar sink previo propio si existiera antes de adquirir el nuevo token
        if self._session_token is not None:
            self._vsm.release_sink(self.guild_id, self._session_token, self.voice_client)
            self._session_token = None

        self._revoked = False
        loop = asyncio.get_running_loop()
        self.sink = TranslatorAudioSink(
            on_speech=self._on_speech,
            active_users=self._active_users if self._active_users else None,
            loop=loop,
        )
        self._listening_active = True
        self._session_token = await self._vsm.acquire_sink(
            guild_id=self.guild_id,
            voice_client=self.voice_client,
            owner_mode="translator",
            sink=self.sink,
            on_revoked=self._handle_sink_revoked,
        )

        if self._tts_task is None or self._tts_task.done():
            self._tts_task = asyncio.create_task(self._tts_player_loop())

        logger.info(f"[Guild {self.guild_id}] Sesion de traduccion iniciada en {voice_channel.name}")

    def _handle_sink_revoked(self):
        """Llamado por VoiceSessionManager si /listen toma el control del sink."""
        self._revoked = True
        self._listening_active = False
        self._session_token = None
        if self.sink:
            self.sink.cleanup()
            self.sink = None
        self._clear_tts_queue()
        logger.info(f"[Guild {self.guild_id}] Sink de traduccion cedido a otra sesion.")

    async def stop(self):
        """Detiene la sesion de traduccion sin interrumpir música si sigue activa."""
        self._revoked = True
        self._listening_active = False

        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()
            try:
                await self._tts_task
            except asyncio.CancelledError:
                pass
        self._tts_task = None
        self._clear_tts_queue()

        if self._session_token is not None:
            token = self._session_token
            self._session_token = None
            self._vsm.release_sink(self.guild_id, token, self.voice_client)
        elif self.sink:
            self.sink.cleanup()

        self.sink = None
        self._active_users.clear()

        music_player = self._get_music_player()
        vc = self._vsm.voice_clients.get(self.guild_id) or self.voice_client
        self.voice_client = None

        can_disc = self._vsm.can_disconnect(
            self.guild_id,
            requester_mode="translator",
            is_music_active_cb=music_player.is_music_active if music_player else None,
        )
        if can_disc and vc is not None:
            await self._vsm.request_deferred_disconnect(
                self.guild_id, vc, on_disconnected_cb=None
            )
        else:
            logger.info(
                f"[Guild {self.guild_id}] Se mantiene VoiceClient conectado (música u otro modo activo)."
            )

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
        vc = self._vsm.voice_clients.get(self.guild_id) or self.voice_client
        return self._listening_active and vc is not None and bool(vc.is_connected())

    # ── Validación centralizada y Pipeline STT -> TR -> TTS ──

    def _is_current(self, user_id: int, job_token: int | None) -> bool:
        return (
            not self._revoked
            and self._listening_active
            and job_token is not None
            and self._session_token == job_token
            and ((not self._active_users) or (user_id in self._active_users))
        )

    def _is_job_cancelled(self, user_id: int, session_token: int | None = None) -> bool:
        token_to_check = session_token if session_token is not None else self._session_token
        return not self._is_current(user_id, token_to_check)

    def _is_user_cancelled(self, user_id: int) -> bool:
        return self._is_job_cancelled(user_id, self._session_token)

    async def _on_speech(self, user_id: int, pcm_bytes: bytes):
        """Callback del AudioSink al finalizar una frase."""
        job_token = self._session_token
        if (
            job_token is None
            or user_id in self._processing
            or not self._is_current(user_id, job_token)
        ):
            return
        self._processing.add(user_id)

        try:
            settings = await get_user_settings(user_id)
            if not self._is_current(user_id, job_token):
                return
            target_lang = settings.get("target_lang", "es")

            # 1. STT en cola compartida con prompt adaptativo
            audio_array = stt.preprocess_audio(pcm_bytes)
            whisper_prompt = await build_whisper_prompt(user_id)
            if not self._is_current(user_id, job_token):
                return

            stt_result = await stt.transcribe(
                audio_array,
                initial_prompt=whisper_prompt,
                is_cancelled_cb=lambda: not self._is_current(user_id, job_token),
            )
            if not self._is_current(user_id, job_token):
                return

            text = stt_result["text"]
            detected_lang = stt_result["language"]
            confidence = stt_result["confidence"]

            if not text or confidence < 0.3:
                return

            logger.info(f"[{user_id}] STT ({detected_lang}): '{text}' (conf={confidence:.2f})")

            if detected_lang == target_lang:
                return

            # 2. Traduccion con glosario personal
            tr_result = await tr.translate_text(
                text=text,
                target_lang=target_lang,
                source_lang=detected_lang,
                user_id=user_id,
            )
            if not self._is_current(user_id, job_token):
                return

            translated = tr_result["translated"]
            if not translated:
                return

            logger.info(f"[{user_id}] TR ({detected_lang}->{target_lang}): '{translated}'")

            # 3. Enviar al canal de texto y revalidar antes de sintetizar
            deferred = not self._can_play_tts()
            await self._send_to_text_channel(
                user_id, text, translated, detected_lang, target_lang, deferred_tts=deferred
            )
            if not self._is_current(user_id, job_token):
                return

            # 4. TTS (verificando vigencia tras sintetizar; borrar temporal si revocada)
            voice_name = settings.get("voice_name") or get_voice_for_lang(target_lang)
            audio_path = await tts.synthesize(translated, voice=voice_name)

            if not self._is_current(user_id, job_token):
                self._unlink_silent(audio_path)
                return

            if audio_path:
                self._enqueue_tts(audio_path, user_id, job_token)

            # 5. Revalidar justo antes de iniciar save_translation() en SQLite
            if not self._is_current(user_id, job_token):
                return

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

    # ── Reproduccion de TTS en cola con prioridad musical ────

    async def _tts_player_loop(self):
        """
        Reproduce TTS reservando el canal en VoiceSessionManager y validando vigencia
        con _is_current(user_id, job_token).
        """
        while True:
            try:
                audio_path, user_id, enqueued_at, job_token = await self._tts_queue.get()
                pb_token: int | None = None
                vc_selected: Any = None
                try:
                    expired = False
                    while True:
                        vc_selected = self._vsm.voice_clients.get(self.guild_id) or self.voice_client
                        if (
                            not self._is_current(user_id, job_token)
                            or vc_selected is None
                            or not vc_selected.is_connected()
                            or (time.monotonic() - enqueued_at) > TTS_MAX_HOLD_S
                        ):
                            expired = True
                            break

                        pb_token = await self._vsm.acquire_tts_playback(
                            self.guild_id,
                            expected_vc=vc_selected,
                            can_play_cb=self._can_play_tts,
                        )
                        if pb_token is not None:
                            break
                        await asyncio.sleep(0.15)

                    if (
                        expired
                        or pb_token is None
                        or not self._is_current(user_id, job_token)
                        or (time.monotonic() - enqueued_at) > TTS_MAX_HOLD_S
                    ):
                        if pb_token is not None:
                            self._vsm.release_playback(self.guild_id, playback_token=pb_token)
                        self._unlink_silent(audio_path)
                        logger.debug(f"[{user_id}] Clip TTS descartado o expirado esperando turno.")
                        continue

                    source = None
                    try:
                        source = discord.FFmpegPCMAudio(audio_path)
                        done = asyncio.Event()
                        loop = asyncio.get_running_loop()

                        def _finish_in_loop(error: Exception | None) -> None:
                            if error:
                                logger.warning(f"[{user_id}] Error en stream TTS: {error}")
                            self._vsm.release_playback(self.guild_id, playback_token=pb_token)
                            if not done.is_set():
                                done.set()

                        def _after(error: Exception | None) -> None:
                            # Ejecutado en hilo de audio de discord.py: PROHIBIDO mutar asyncio o state.
                            if loop.is_closed():
                                self._record_dropped_after_on_closed_loop(
                                    self.guild_id,
                                    job_token,
                                    pb_token,
                                    reason="tts_loop_is_closed",
                                )
                                logger.warning(
                                    "Fail-closed TTS: event loop cerrado antes de after (guild=%s, pb_token=%s).",
                                    self.guild_id,
                                    pb_token,
                                )
                                return
                            try:
                                loop.call_soon_threadsafe(_finish_in_loop, error)
                            except RuntimeError as exc:
                                self._record_dropped_after_on_closed_loop(
                                    self.guild_id,
                                    job_token,
                                    pb_token,
                                    reason=f"tts_runtime_error:{exc}",
                                )
                                logger.warning(
                                    "Fail-closed TTS: call_soon_threadsafe falló (guild=%s, pb_token=%s): %s",
                                    self.guild_id,
                                    pb_token,
                                    exc,
                                )

                        vc_selected.play(source, after=_after)
                        await done.wait()
                    finally:
                        if source is not None:
                            cleanup_fn = getattr(source, "cleanup", None)
                            if callable(cleanup_fn):
                                try:
                                    cleanup_fn()
                                except Exception as cleanup_exc:
                                    logger.warning(
                                        f"[{user_id}] Error en cleanup() de fuente TTS: {cleanup_exc}"
                                    )
                        self._vsm.release_playback(self.guild_id, playback_token=pb_token)
                        self._unlink_silent(audio_path)
                finally:
                    self._tts_queue.task_done()

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
        deferred_tts: bool = False,
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
            footer = f"Speaker: <@{user_id}>"
            if deferred_tts:
                footer += " | Audio TTS en cola (música activa)"
            embed.set_footer(text=footer)
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
