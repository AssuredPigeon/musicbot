# modules/music_player.py
#
# Reproductor de música (Bloque 0A v3.3.13).

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord
import yt_dlp

import config
from data.database import add_song_to_history, get_user_history
from modules.recommendation import RecommendationSystem
from modules.voice_processor import capture_and_process_voice
from modules.voice_receiver import VoiceReceiver
from utils.cookie_manager import is_cookie_error
from utils.error_handler import log_exception
from utils.spotify_utils import get_spotify_token, search_spotify_track
from utils.stt_queue import stt_queue_manager
from utils.voice_session_manager import (
    PlaybackPreemptionTimeoutError,
    PlaybackStateInconsistencyError,
    VoiceClientUnavailableError,
    VoiceClientUpgradeBlockedError,
    VoiceReceiveUnavailableError,
    VoiceSessionManager,
    _probe_vc_connected,
    voice_session_manager,
)
from utils.youtube_utils import search_youtube

logger = logging.getLogger("musicbot.player")

TRANSITION_BLOCKED_MSG = "Conexión de voz en transición; inténtalo de nuevo en un momento."


@dataclass(frozen=True)
class MusicOpToken:
    guild_id: int
    gen: int
    song_token: int


class MusicPlayer:
    def __init__(self, bot: Any, *, session_manager: VoiceSessionManager | None = None):
        self.bot = bot
        if self.bot is not None:
            setattr(self.bot, "_music_player", self)

        self._vsm: VoiceSessionManager = session_manager or voice_session_manager
        # Alias directo de solo lectura al diccionario autoritativo de VoiceSessionManager
        self.voice_clients = self._vsm.voice_clients

        self.queues: dict[int, list[dict[str, Any]]] = {}
        self.current_songs: dict[int, dict[str, Any] | None] = {}
        self.is_playing: dict[int, bool] = {}
        self.is_paused: dict[int, bool] = {}
        self._transition_active: dict[int, bool] = {}
        self._music_gen: dict[int, int] = {}
        self._song_token_counter: int = 0
        self._active_op_tokens: dict[int, MusicOpToken | None] = {}
        self._track_playback_tokens: dict[int, int | None] = {}
        self._retry_gates: dict[tuple[int, int], asyncio.Event] = {}
        self._in_use_cache_files: dict[tuple[int, int], Path] = {}
        self._play_next_tasks: dict[int, asyncio.Task[Any]] = {}

        # Métricas fail-closed thread-safe en cierre de loop
        self._closed_loop_metrics_lock = threading.Lock()
        self.dropped_after_callbacks_on_closed_loop: int = 0
        self._incomplete_close_reconciliations: list[
            tuple[int, int | None, int | None, str, float]
        ] = []

        self.voice_command_mode: dict[int, dict[int, bool]] = {}
        self.recommendation_system = RecommendationSystem()
        self.voice_command_handler = None
        self.volume: dict[int, float] = {}
        self.default_volume = 0.5

        self.file_cache: dict[str, dict[str, Any]] = {}
        self.cache_expiry_days = 7
        self.cache_size_limit_mb = 500

        self.voice_receivers: dict[int, VoiceReceiver] = {}
        self._voice_text_channels: dict[int, int] = {}

        self._vsm.register_vc_lifecycle_callback(
            self._on_vc_lifecycle_change, key="music_player"
        )
        self._vsm.set_music_active_callback(self.is_music_active)
        stt_queue_manager.set_music_active_callback(self.is_music_active)

    # ── Validación de tokens y ciclo de vida ────────────────────

    @staticmethod
    def _require_guild_id(guild_id: int | None) -> int:
        if guild_id is None or isinstance(guild_id, bool) or not isinstance(guild_id, int):
            raise ValueError(f"Se requiere un guild_id entero válido (recibido: {guild_id!r}).")
        return guild_id

    def _next_song_token(self) -> int:
        self._song_token_counter += 1
        return self._song_token_counter

    def _is_current(self, guild_id: int, token: MusicOpToken | None) -> bool:
        if token is None or token.guild_id != guild_id:
            return False
        if self._music_gen.get(guild_id, 0) != token.gen:
            return False
        if self._active_op_tokens.get(guild_id) != token:
            return False
        vc = self.voice_clients.get(guild_id)
        return vc is not None and _probe_vc_connected(vc) is True

    def _wake_retry_gates_for_guild(self, guild_id: int) -> None:
        for (gid, _song_tok), ev in list(self._retry_gates.items()):
            if gid == guild_id and not ev.is_set():
                ev.set()

    def _invalidate_guild_operations(self, guild_id: int) -> None:
        """
        Incrementa _music_gen y limpia estado musical transitorio sin tocar
        _in_use_cache_files ni depender de callbacks externos.
        """
        self._music_gen[guild_id] = self._music_gen.get(guild_id, 0) + 1
        self._active_op_tokens[guild_id] = None
        self.is_playing[guild_id] = False
        self.is_paused[guild_id] = False
        self.current_songs[guild_id] = None
        self._track_playback_tokens[guild_id] = None
        self._wake_retry_gates_for_guild(guild_id)

    def _on_vc_lifecycle_change(
        self,
        guild_id: int,
        *,
        vc: Any = None,
        physically_disconnected: bool = False,
    ) -> None:
        # Hook invocado síncronamente por VoiceSessionManager bajo _get_lock(guild_id).
        # Jamás toca _in_use_cache_files (liberado solo al liberar su pb_token en el loop).
        self._invalidate_guild_operations(guild_id)

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

    # ── Gestión de caché con protección incondicional _in_use_cache_files ──

    def _protected_cache_paths(self) -> set[Path]:
        protected: set[Path] = set()
        for p in self._in_use_cache_files.values():
            try:
                protected.add(Path(p).resolve())
            except Exception:
                pass
        return protected

    def _is_path_protected(self, file_path: str | Path, protected: set[Path] | None = None) -> bool:
        try:
            resolved = Path(file_path).resolve()
        except Exception:
            return False
        pts = protected if protected is not None else self._protected_cache_paths()
        return resolved in pts

    async def _add_to_cache(self, song_id: str, file_path: str) -> None:
        """Añade un archivo al sistema de caché con su timestamp."""
        try:
            file_size = Path(file_path).stat().st_size
            self.file_cache[song_id] = {
                "path": file_path,
                "last_accessed": time.time(),
                "size": file_size,
            }
            logger.info(f"Añadido a caché: {song_id}, tamaño: {file_size / (1024*1024):.2f} MB")
            await self._check_cache_size()
        except Exception as e:
            log_exception(f"Error añadiendo archivo a caché: {song_id}", e)

    def _update_cache_access(self, song_id: str) -> None:
        if song_id in self.file_cache:
            file_path = self.file_cache[song_id]["path"]
            if Path(file_path).exists():
                self.file_cache[song_id]["last_accessed"] = time.time()
            else:
                del self.file_cache[song_id]
                logger.warning(f"Archivo no encontrado en cache: {song_id}, eliminando registro")

    async def _check_cache_size(self) -> None:
        """Verifica si el tamaño de la caché excede el límite y limpia archivos no protegidos."""
        try:
            total_size = sum(item["size"] for item in self.file_cache.values())
            total_size_mb = total_size / (1024 * 1024)
            if total_size_mb <= self.cache_size_limit_mb:
                return

            logger.info(
                f"Caché excede el límite ({total_size_mb:.2f}MB > {self.cache_size_limit_mb}MB). Limpiando..."
            )
            protected = self._protected_cache_paths()
            sorted_files = sorted(self.file_cache.items(), key=lambda x: x[1]["last_accessed"])
            target_size = self.cache_size_limit_mb * 0.8 * 1024 * 1024
            current_size = total_size

            for song_id, info in sorted_files:
                if current_size <= target_size:
                    break
                file_path = info["path"]
                if self._is_path_protected(file_path, protected):
                    continue
                file_size = info["size"]
                try:
                    if Path(file_path).exists():
                        Path(file_path).unlink()
                    del self.file_cache[song_id]
                    current_size -= file_size
                    logger.info(f"Eliminado de caché por límite de tamaño: {song_id}")
                except Exception as e:
                    log_exception(f"Error eliminando archivo de caché: {file_path}", e)
        except Exception as e:
            log_exception("Error verificando tamaño de caché", e)

    async def start_cleanup_task(self) -> None:
        self.cleanup_task = asyncio.create_task(self._periodic_cache_cleanup())
        logger.info("Tarea de limpieza de caché iniciada")

    async def _periodic_cache_cleanup(self) -> None:
        retry_count = 0
        max_retries = 3
        try:
            while True:
                await asyncio.sleep(6 * 60 * 60)
                try:
                    await self._cleanup_expired_files()
                    retry_count = 0
                    Path(config.TEMP_AUDIO_DIR).mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    retry_count += 1
                    log_exception(
                        f"Error durante limpieza (intento {retry_count}/{max_retries})", e
                    )
                    if retry_count >= max_retries:
                        logger.error("Demasiados errores consecutivos, deteniendo tarea de limpieza")
                        break
                    await asyncio.sleep(30 * 60)
        except asyncio.CancelledError:
            logger.info("Tarea de limpieza de caché cancelada")
        except Exception as e:
            log_exception("Error crítico en tarea de limpieza de caché", e)
            await asyncio.sleep(30 * 60)
            self.cleanup_task = asyncio.create_task(self._periodic_cache_cleanup())

    async def _cleanup_expired_files(self) -> None:
        try:
            expiry_timestamp = time.time() - (self.cache_expiry_days * 24 * 60 * 60)
            total_files = len(self.file_cache)
            removed_files = 0
            protected = self._protected_cache_paths()

            expired_song_ids = [
                song_id
                for song_id, info in self.file_cache.items()
                if info["last_accessed"] < expiry_timestamp
                and not self._is_path_protected(info["path"], protected)
            ]

            for song_id in expired_song_ids:
                file_path = self.file_cache[song_id]["path"]
                try:
                    if Path(file_path).exists():
                        Path(file_path).unlink()
                    del self.file_cache[song_id]
                    removed_files += 1
                except Exception as e:
                    log_exception(f"Error eliminando archivo expirado: {file_path}", e)

            await self._cleanup_orphaned_files()
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info(
                f"[{now}] Limpieza de caché: eliminados {removed_files} de {total_files} archivos por expiración"
            )
        except Exception as e:
            log_exception("Error en limpieza de archivos expirados", e)

    async def _cleanup_orphaned_files(self) -> None:
        try:
            cache_dir = Path(config.TEMP_AUDIO_DIR)
            cache_dir.mkdir(parents=True, exist_ok=True)
            known_files = {Path(info["path"]).resolve() for info in self.file_cache.values()}
            protected = self._protected_cache_paths()
            orphans_removed = 0

            for file_path in cache_dir.glob("*"):
                if not file_path.is_file():
                    continue
                resolved = file_path.resolve()
                if resolved in known_files or resolved in protected:
                    continue
                try:
                    file_path.unlink()
                    orphans_removed += 1
                except Exception as e:
                    log_exception(f"Error eliminando archivo huérfano: {file_path}", e)

            if orphans_removed > 0:
                logger.info(
                    f"Limpieza de huérfanos: eliminados {orphans_removed} archivos no registrados"
                )
        except Exception as e:
            log_exception("Error en limpieza de archivos huérfanos", e)

    async def clear_cache(self, older_than_days: float | None = None) -> str:
        """Limpia la caché excluyendo incondicionalmente archivos presentes en _in_use_cache_files."""
        try:
            protected = self._protected_cache_paths()
            if older_than_days is not None:
                expiry_timestamp = time.time() - (older_than_days * 24 * 60 * 60)
                expired_song_ids = [
                    song_id
                    for song_id, info in self.file_cache.items()
                    if info["last_accessed"] < expiry_timestamp
                    and not self._is_path_protected(info["path"], protected)
                ]
                for song_id in expired_song_ids:
                    file_path = self.file_cache[song_id]["path"]
                    if Path(file_path).exists():
                        Path(file_path).unlink()
                    del self.file_cache[song_id]
                return f"Caché limpiada: eliminados {len(expired_song_ids)} archivos más antiguos que {older_than_days} días."

            removed = 0
            for song_id, info in list(self.file_cache.items()):
                file_path = info["path"]
                if self._is_path_protected(file_path, protected):
                    continue
                if Path(file_path).exists():
                    Path(file_path).unlink()
                del self.file_cache[song_id]
                removed += 1

            cache_dir = Path(config.TEMP_AUDIO_DIR)
            if cache_dir.exists():
                for entry in list(cache_dir.iterdir()):
                    if self._is_path_protected(entry, protected):
                        continue
                    try:
                        if entry.is_dir():
                            shutil.rmtree(entry, ignore_errors=True)
                        else:
                            entry.unlink()
                    except Exception:
                        pass
            cache_dir.mkdir(parents=True, exist_ok=True)
            return f"Caché limpiada: eliminados {removed} archivos."
        except Exception as e:
            log_exception("Error en limpieza manual de caché", e)
            return "Error al intentar limpiar la caché."

    # ── Conexión y consultas de estado ──────────────────────────

    def register_voice_command_handler(self, handler: Any) -> None:
        self.voice_command_handler = handler

    async def join_voice_channel(self, voice_channel: Any, require_receive: bool = False) -> bool:
        if not voice_channel:
            return False
        guild_id = voice_channel.guild.id
        try:
            await self._vsm.ensure_voice_client(
                voice_channel,
                require_receive=require_receive,
                is_music_active_cb=self.is_music_active,
            )
            logger.info(f"Conectado/movido al canal {voice_channel.name}")
            self.queues.setdefault(guild_id, [])
            self.is_playing.setdefault(guild_id, False)
            self.is_paused.setdefault(guild_id, False)
            self._transition_active.setdefault(guild_id, False)
            self.current_songs.setdefault(guild_id, None)
            self.volume.setdefault(guild_id, self.default_volume)
            return True
        except (VoiceReceiveUnavailableError, VoiceClientUpgradeBlockedError):
            raise
        except Exception as e:
            log_exception(f"Error al unirse al canal de voz {voice_channel.name}", e)
            return False

    def is_connected(self, guild_id: int | None = None) -> bool:
        if guild_id is None:
            return any(_probe_vc_connected(vc) is True for vc in self.voice_clients.values())
        vc = self.voice_clients.get(guild_id)
        return vc is not None and _probe_vc_connected(vc) is True

    def is_music_active(self, guild_id: int | None = None) -> bool:
        if guild_id is None:
            known_guilds = (
                set(self.voice_clients)
                | set(self.is_playing)
                | set(self.is_paused)
                | set(self.queues)
                | set(self._transition_active)
            )
            return any(self.is_music_active(gid) for gid in known_guilds)

        playback_is_music = self._vsm.get_playback_owner(guild_id) == "music"
        return bool(
            playback_is_music
            or self.is_playing.get(guild_id, False)
            or self.is_paused.get(guild_id, False)
            or self.current_songs.get(guild_id) is not None
            or self._transition_active.get(guild_id, False)
            or len(self.queues.get(guild_id, [])) > 0
        )

    def can_play_auxiliary_audio(self, guild_id: int) -> bool:
        vc = self.voice_clients.get(guild_id)
        if vc is None or _probe_vc_connected(vc) is not True:
            return False
        if self._vsm.is_voice_transitioning(guild_id) or self._vsm.has_unreconciled_voice_client(
            guild_id
        ):
            return False
        if self._vsm.get_playback_owner(guild_id) is not None:
            return False
        if self.is_music_active(guild_id):
            return False
        if vc.is_playing() or vc.is_paused():
            return False
        return True

    # ── Encolado, _play_next, _retry_gates y finalización en el loop ──

    def enqueue_song(self, guild_id: int, song: dict[str, Any]) -> bool:
        """
        Encola una canción para guild_id siguiendo la regla exacta de play_song():
        - Si ya existe ocupación lógica/física o transición activa: solo encola y retorna False.
        - Si está inactivo y sin transición activa: inicia _play_next(guild_id) y retorna True.
        """
        guild_id = self._require_guild_id(guild_id)
        self.queues.setdefault(guild_id, []).append(song)
        if (
            self.is_playing.get(guild_id, False)
            or self.is_paused.get(guild_id, False)
            or self._transition_active.get(guild_id, False)
        ):
            return False
        self._launch_play_next(guild_id)
        return True

    def _launch_play_next(self, guild_id: int) -> asyncio.Task[Any] | None:
        if self._transition_active.get(guild_id, False):
            return None
        self._transition_active[guild_id] = True
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._play_next(guild_id))
        self._play_next_tasks[guild_id] = task
        return task

    async def _wait_retry_gate(self, guild_id: int, op_token: MusicOpToken) -> bool:
        """Orden atómico síncrono check -> registro -> recheck sin await intermedio."""
        if not self._is_current(guild_id, op_token):
            return False
        gate_key = (guild_id, op_token.song_token)
        ev = asyncio.Event()
        self._retry_gates[gate_key] = ev
        if not self._is_current(guild_id, op_token):
            if self._retry_gates.get(gate_key) is ev:
                self._retry_gates.pop(gate_key, None)
            return False

        try:
            await asyncio.wait_for(ev.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        finally:
            if self._retry_gates.get(gate_key) is ev:
                self._retry_gates.pop(gate_key, None)

        return self._is_current(guild_id, op_token)

    async def play_song(self, query: str, user_id: int) -> str:
        try:
            member = None
            guild = None
            for g in self.bot.guilds:
                member = g.get_member(user_id)
                if member:
                    guild = g
                    break
            if not guild:
                return "No se pudo determinar a qué servidor perteneces."

            guild_id = guild.id
            if not self.is_connected(guild_id):
                if not member.voice:
                    return "Necesitas estar en un canal de voz primero."
                join_success = await self.join_voice_channel(member.voice.channel)
                if not join_success:
                    return "No pude unirme a tu canal de voz."

            search_result = await search_youtube(query)
            if not search_result:
                spotify_token = get_spotify_token()
                if spotify_token:
                    spotify_result = search_spotify_track(query, spotify_token)
                    if spotify_result:
                        search_query = f"{spotify_result['name']} {spotify_result['artist']}"
                        search_result = await search_youtube(search_query)
            if not search_result:
                return f"No pude encontrar resultados para: {query}"

            song = {
                "url": search_result["url"],
                "title": search_result["title"],
                "requester": user_id,
                "song_id": search_result["id"],
                "artist": search_result.get("artist", "Desconocido"),
            }
            await add_song_to_history(
                user_id, song["song_id"], song["title"], song["artist"]
            )

            started = self.enqueue_song(guild_id, song)
            if started:
                return f"▶️ Reproduciendo: **{song['title']}** - {song['artist']}"
            return f"🎵 Añadido a la cola: **{song['title']}** - {song['artist']}"
        except Exception as e:
            log_exception(f"Error al reproducir canción: {query}", e)
            return f"Ocurrió un error al intentar reproducir la canción: {str(e)}"

    async def _try_extract_info(self, url: str, retries: int = 3) -> dict[str, Any]:
        exceptions = []
        options_to_try = [
            config.YDL_OPTIONS,
            {**config.YDL_OPTIONS, "nocheckcertificate": True, "geo_bypass": True},
            {**config.YDL_OPTIONS, "format": "bestaudio", "retries": 10},
            {"format": "bestaudio/best", "quiet": True, "no_warnings": True, "retries": 5},
        ]
        for i, options in enumerate(options_to_try[:retries]):
            try:
                dl_tok = stt_queue_manager.begin_download()
                try:
                    with yt_dlp.YoutubeDL(options) as ydl:
                        logger.info(f"Intento {i+1}/{retries} para {url}")
                        info = ydl.extract_info(url, download=False)
                        if info:
                            return info
                finally:
                    stt_queue_manager.end_download(dl_tok)
            except Exception as e:
                exceptions.append(str(e))
                logger.warning(f"Error en intento {i+1}: {str(e)}")
                await asyncio.sleep(1)

        all_errors = " | ".join(exceptions)
        if is_cookie_error(all_errors):
            raise Exception(
                f"🍪 Las cookies de YouTube han expirado o son inválidas. "
                f"Ejecuta `scripts/refresh_cookies.sh` para renovarlas. (Error: {exceptions[-1]})"
            )
        raise Exception(
            f"No se pudo extraer información después de {retries} intentos: {all_errors}"
        )

    async def _play_next(self, guild_id: int, max_retries: int = 3) -> None:
        """Extrae y reproduce la siguiente canción con token de vigencia y guarda única."""
        self._transition_active[guild_id] = True
        try:
            q = self.queues.get(guild_id)
            if not q:
                self._active_op_tokens[guild_id] = None
                self.is_playing[guild_id] = False
                self.is_paused[guild_id] = False
                self.current_songs[guild_id] = None
                self._track_playback_tokens[guild_id] = None
                return

            op_gen = self._music_gen.get(guild_id, 0)
            song = q.pop(0)
            op_token = MusicOpToken(guild_id, op_gen, self._next_song_token())
            self._active_op_tokens[guild_id] = op_token
            self.current_songs[guild_id] = song
            self.is_playing[guild_id] = True
            self.is_paused[guild_id] = False

            cache_key = (guild_id, op_token.song_token)
            retry_count = 0

            while retry_count <= max_retries:
                if not self._is_current(guild_id, op_token):
                    self._in_use_cache_files.pop(cache_key, None)
                    return

                source_target: str | None = None
                source_ffmpeg_kwargs: dict[str, Any] = {}
                try:
                    song_id = song.get("song_id", "")
                    use_cache = True
                    cached_file = None

                    if use_cache and song_id in self.file_cache:
                        cached_path = self.file_cache[song_id]["path"]
                        if Path(cached_path).exists():
                            self._update_cache_access(song_id)
                            cached_file = cached_path
                            self._in_use_cache_files[cache_key] = Path(cached_file).resolve()

                    if cached_file:
                        source_target = cached_file
                    else:
                        cookies_path = config.COOKIES_PATH
                        custom_options = config.YDL_OPTIONS.copy()
                        if os.path.exists(cookies_path) and os.path.getsize(cookies_path) > 0:
                            custom_options["cookiefile"] = cookies_path

                        use_local_file = True
                        if use_local_file and use_cache:
                            cache_dir = Path(config.TEMP_AUDIO_DIR)
                            cache_dir.mkdir(parents=True, exist_ok=True)
                            custom_options["outtmpl"] = os.path.join(
                                config.TEMP_AUDIO_DIR, f"{song_id}.%(ext)s"
                            )
                            dl_tok = stt_queue_manager.begin_download()
                            try:
                                with yt_dlp.YoutubeDL(custom_options) as ydl:
                                    info = ydl.extract_info(song["url"], download=True)
                                    if not info:
                                        raise Exception("No se pudo obtener información del video")
                                    real_file = None
                                    if info.get("requested_downloads"):
                                        real_file = info["requested_downloads"][0].get("filepath")
                                    if not real_file or not os.path.exists(real_file):
                                        real_ext = info.get("ext", "mp3")
                                        real_file = os.path.join(
                                            config.TEMP_AUDIO_DIR, f"{song_id}.{real_ext}"
                                        )
                                    if not os.path.exists(real_file):
                                        real_file = ydl.prepare_filename(info)
                                    if not os.path.exists(real_file):
                                        raise Exception(
                                            f"No se pudo encontrar el archivo descargado. Intentó: {real_file}"
                                        )
                                    file_size = os.path.getsize(real_file)
                                    if file_size < 1024:
                                        raise Exception(
                                            f"El archivo descargado parece estar corrupto (tamaño: {file_size} bytes)"
                                        )
                                    download_path = real_file
                            finally:
                                stt_queue_manager.end_download(dl_tok)

                            self._in_use_cache_files[cache_key] = Path(download_path).resolve()
                            await self._add_to_cache(song_id, download_path)
                            if not self._is_current(guild_id, op_token):
                                self._in_use_cache_files.pop(cache_key, None)
                                return
                            source_target = download_path
                        else:
                            dl_tok = stt_queue_manager.begin_download()
                            try:
                                with yt_dlp.YoutubeDL(custom_options) as ydl:
                                    info = ydl.extract_info(song["url"], download=False)
                                    source_target = info["url"]
                            finally:
                                stt_queue_manager.end_download(dl_tok)
                            source_ffmpeg_kwargs = {
                                "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                                "options": '-vn -af "volume=0.5"',
                            }

                    if not self._is_current(guild_id, op_token):
                        self._in_use_cache_files.pop(cache_key, None)
                        return

                    expected_vc = self.voice_clients.get(guild_id)
                    if expected_vc is None or _probe_vc_connected(expected_vc) is not True:
                        raise VoiceClientUnavailableError(
                            f"[Guild {guild_id}] Sin VoiceClient conectado antes de acquire_music_playback."
                        )

                    # Preempción con doble validación antes y después sin await posterior a la revalidación
                    pb_token = await self._vsm.acquire_music_playback(
                        guild_id, expected_vc=expected_vc
                    )
                    if not self._is_current(guild_id, op_token):
                        self._vsm.release_playback(guild_id, playback_token=pb_token)
                        self._in_use_cache_files.pop(cache_key, None)
                        return

                    vc = self.voice_clients.get(guild_id)
                    if vc is not expected_vc or _probe_vc_connected(vc) is not True:
                        self._vsm.release_playback(guild_id, playback_token=pb_token)
                        self._in_use_cache_files.pop(cache_key, None)
                        raise VoiceClientUnavailableError(
                            f"[Guild {guild_id}] VoiceClient cambió o se desconectó al adquirir playback."
                        )

                    loop = asyncio.get_running_loop()

                    def _after_playback_thread(error: Exception | None) -> None:
                        # Ejecutado en hilo de audio de discord.py: PROHIBIDO mutar asyncio o state.
                        if loop.is_closed():
                            self._record_dropped_after_on_closed_loop(
                                guild_id,
                                op_token.song_token,
                                pb_token,
                                reason="loop_is_closed",
                            )
                            logger.warning(
                                "Fail-closed: event loop cerrado antes de despachar after (guild=%s, pb_token=%s); reconciliación de cierre incompleta.",
                                guild_id,
                                pb_token,
                            )
                            return
                        try:
                            loop.call_soon_threadsafe(
                                self._on_song_finished_in_loop,
                                error,
                                guild_id,
                                op_token,
                                pb_token,
                            )
                        except RuntimeError as exc:
                            self._record_dropped_after_on_closed_loop(
                                guild_id,
                                op_token.song_token,
                                pb_token,
                                reason=f"runtime_error:{exc}",
                            )
                            logger.warning(
                                "Fail-closed: call_soon_threadsafe falló sobre loop cerrado (guild=%s, pb_token=%s): %s; reconciliación de cierre incompleta.",
                                guild_id,
                                pb_token,
                                exc,
                            )

                    raw_source = None
                    audio_source = None
                    playback_started = False
                    start_exc: Exception | None = None
                    current_volume = self.volume.get(guild_id, self.default_volume)
                    try:
                        assert source_target is not None
                        raw_source = discord.FFmpegPCMAudio(source_target, **source_ffmpeg_kwargs)
                        audio_source = discord.PCMVolumeTransformer(
                            raw_source, volume=current_volume
                        )
                        self._track_playback_tokens[guild_id] = pb_token
                        vc.play(audio_source, after=_after_playback_thread)
                        playback_started = True
                    except Exception as exc:
                        start_exc = exc
                    finally:
                        if not playback_started:
                            source_to_clean = audio_source if audio_source is not None else raw_source
                            if source_to_clean is not None:
                                cleanup_fn = getattr(source_to_clean, "cleanup", None)
                                if callable(cleanup_fn):
                                    try:
                                        cleanup_fn()
                                    except Exception as cleanup_exc:
                                        logger.warning(
                                            "[Guild %s] Error limpiando fuente tras fallo de inicio: %s",
                                            guild_id,
                                            cleanup_exc,
                                        )
                            self._vsm.release_playback(guild_id, playback_token=pb_token)
                            if self._track_playback_tokens.get(guild_id) == pb_token:
                                self._track_playback_tokens[guild_id] = None
                            self._in_use_cache_files.pop(cache_key, None)

                    if start_exc is not None:
                        raise start_exc

                    logger.info(f"Reproduciendo en {guild_id}: {song.get('title', 'Canción')}")
                    return

                except Exception as exc:
                    self._in_use_cache_files.pop(cache_key, None)
                    log_exception(
                        f"Error reproduciendo {song.get('title', 'Canción')} en {guild_id} "
                        f"(intento {retry_count + 1}/{max_retries + 1})",
                        exc,
                    )
                    if not self._is_current(guild_id, op_token):
                        return

                    # Ante indisponibilidad física no tiene sentido reintentar 2s encima de cliente muerto
                    if isinstance(exc, VoiceClientUnavailableError):
                        if self._active_op_tokens.get(guild_id) == op_token:
                            self._active_op_tokens[guild_id] = None
                            self.is_playing[guild_id] = False
                            self.is_paused[guild_id] = False
                            self.current_songs[guild_id] = None
                        return

                    if retry_count >= max_retries:
                        await self._notify_error(
                            guild_id,
                            f"Error al reproducir {song.get('title', 'Canción')}: {str(exc)}",
                        )
                        if self._is_current(guild_id, op_token):
                            self._active_op_tokens[guild_id] = None
                            self.is_playing[guild_id] = False
                            self.is_paused[guild_id] = False
                            self.current_songs[guild_id] = None
                        return

                    still_valid = await self._wait_retry_gate(guild_id, op_token)
                    if not still_valid:
                        return
                    retry_count += 1
        finally:
            self._transition_active[guild_id] = False
            # Relanzar solo si se cumplen exactamente las 4 condiciones síncronas
            if (
                self._active_op_tokens.get(guild_id) is None
                and not self.is_playing.get(guild_id, False)
                and not self.is_paused.get(guild_id, False)
                and self.current_songs.get(guild_id) is None
                and bool(self.queues.get(guild_id))
                and (vc := self.voice_clients.get(guild_id)) is not None
                and _probe_vc_connected(vc) is True
                and not self._vsm.has_unreconciled_voice_client(guild_id)
                and not self._vsm.is_voice_transitioning(guild_id)
                and not self._transition_active.get(guild_id, False)
            ):
                self._launch_play_next(guild_id)
            elif not self.is_music_active(guild_id):
                self._vsm.notify_music_idle(guild_id)

    def _on_song_finished_in_loop(
        self,
        error: Exception | None,
        guild_id: int,
        op_token: MusicOpToken,
        pb_token: int,
    ) -> None:
        """
        Ejecutado exclusivamente en el hilo del event loop (vía loop.call_soon_threadsafe).
        Libera token y ruta protegida en _in_use_cache_files; actualiza estado solo si _is_current.
        """
        if error:
            log_exception(f"Error durante la reproducción en {guild_id}", error)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    self._notify_error(guild_id, f"Error durante la reproducción: {str(error)}")
                )
            except RuntimeError:
                pass

        self._vsm.release_playback(guild_id, playback_token=pb_token)
        if self._track_playback_tokens.get(guild_id) == pb_token:
            self._track_playback_tokens[guild_id] = None
        self._in_use_cache_files.pop((guild_id, op_token.song_token), None)

        if not self._is_current(guild_id, op_token):
            return

        self._active_op_tokens[guild_id] = None
        self.is_playing[guild_id] = False
        self.is_paused[guild_id] = False
        self.current_songs[guild_id] = None

        if (
            self.queues.get(guild_id)
            and not self._transition_active.get(guild_id, False)
            and (vc := self.voice_clients.get(guild_id)) is not None
            and _probe_vc_connected(vc) is True
            and not self._vsm.has_unreconciled_voice_client(guild_id)
            and not self._vsm.is_voice_transitioning(guild_id)
        ):
            self._launch_play_next(guild_id)
        elif not self.is_music_active(guild_id):
            self._vsm.notify_music_idle(guild_id)

    # ── Controles del reproductor (sin fallback guild_id=None) ──

    async def skip(self, guild_id: int | None = None) -> str:
        guild_id = self._require_guild_id(guild_id)
        if self._vsm.is_voice_transitioning(guild_id):
            return TRANSITION_BLOCKED_MSG

        vc = self.voice_clients.get(guild_id)
        if vc is None:
            return "No estoy conectado a un canal de voz."

        tok = self._active_op_tokens.get(guild_id)
        # Rama 1: canción actual con token vivo
        if tok is not None and self._is_current(guild_id, tok):
            self._active_op_tokens[guild_id] = None
            self.is_playing[guild_id] = False
            self.is_paused[guild_id] = False
            self.current_songs[guild_id] = None
            gate_ev = self._retry_gates.get((guild_id, tok.song_token))
            if gate_ev is not None and not gate_ev.is_set():
                gate_ev.set()

            if self._vsm.get_playback_owner(guild_id) == "music" and (
                vc.is_playing() or vc.is_paused()
            ):
                vc.stop()

            if not self._transition_active.get(guild_id, False) and self.queues.get(guild_id):
                self._launch_play_next(guild_id)
            return "⏭️ Saltando a la siguiente canción..."

        # Rama 2: canción actual ya invalidada y cola no vacía
        if self.queues.get(guild_id):
            self.queues[guild_id].pop(0)
            if not self._transition_active.get(guild_id, False) and self.queues.get(guild_id):
                self._launch_play_next(guild_id)
            return "⏭️ Saltando a la siguiente canción..."

        # Rama 3: no-op
        return "No hay nada reproduciéndose actualmente."

    async def pause(self, guild_id: int | None = None) -> str:
        guild_id = self._require_guild_id(guild_id)
        if self._vsm.is_voice_transitioning(guild_id):
            return TRANSITION_BLOCKED_MSG

        vc = self.voice_clients.get(guild_id)
        if vc is None:
            return "No estoy conectado a un canal de voz."

        if self.is_paused.get(guild_id, False):
            return "La reproducción ya está pausada."

        if self._transition_active.get(guild_id, False):
            if self.is_playing.get(guild_id, False):
                return "Hay una transición de canción en curso; inténtalo de nuevo en un momento."
            return "No hay nada reproduciéndose actualmente."

        if (
            self.is_playing.get(guild_id, False)
            and self._vsm.get_playback_owner(guild_id) == "music"
            and self._track_playback_tokens.get(guild_id) is not None
            and vc.is_playing()
        ):
            vc.pause()
            self.is_paused[guild_id] = True
            return "⏸️ Reproducción pausada."
        return "No hay nada reproduciéndose actualmente."

    async def resume(self, guild_id: int | None = None) -> str:
        guild_id = self._require_guild_id(guild_id)
        if self._vsm.is_voice_transitioning(guild_id):
            return TRANSITION_BLOCKED_MSG

        vc = self.voice_clients.get(guild_id)
        if vc is None:
            return "No estoy conectado a un canal de voz."

        if self.is_paused.get(guild_id, False):
            vc.resume()
            self.is_paused[guild_id] = False
            return "▶️ Reproducción reanudada."
        return "La reproducción no está pausada."

    async def set_volume(self, volume: float, guild_id: int | None = None) -> str:
        guild_id = self._require_guild_id(guild_id)
        try:
            vol = float(volume)
            if vol < 0.0:
                vol = 0.0
            elif vol > 1.0:
                vol = 1.0
        except (TypeError, ValueError):
            return "El volumen debe ser un número entre 0.0 y 1.0."

        self.volume[guild_id] = vol
        if not self._vsm.is_voice_transitioning(guild_id):
            vc = self.voice_clients.get(guild_id)
            if (
                vc is not None
                and self.is_playing.get(guild_id, False)
                and self._vsm.get_playback_owner(guild_id) == "music"
                and getattr(vc, "source", None) is not None
            ):
                vc.source.volume = vol
        return f"🔊 Volumen ajustado a {int(vol * 100)}%."

    async def stop(
        self, guild_id: int | None = None, *, on_disconnected_cb: Any = None
    ) -> str:
        """
        Invalida síncronamente operaciones musicales, detiene solo el reproductor musical y
        el sink de comandos de voz, y delega la desconexión diferida en VoiceSessionManager.
        Jamás corta TTS ni purga _in_use_cache_files.
        """
        guild_id = self._require_guild_id(guild_id)

        # Sección síncrona previa a cualquier await: invalidación atómica
        if guild_id in self.voice_command_mode:
            self.voice_command_mode[guild_id] = {}
        self.queues[guild_id] = []

        playback_owner = self._vsm.get_playback_owner(guild_id)
        vc = self.voice_clients.get(guild_id)
        was_music_busy = (
            playback_owner == "music"
            or self.is_playing.get(guild_id, False)
            or self.is_paused.get(guild_id, False)
        )
        self._invalidate_guild_operations(guild_id)

        if vc is not None and playback_owner != "tts" and was_music_busy:
            if vc.is_playing() or vc.is_paused():
                vc.stop()

        if guild_id in self.voice_receivers:
            receiver = self.voice_receivers.pop(guild_id)
            try:
                await receiver.cleanup()
            except Exception:
                pass

        if vc is None:
            return "No estoy conectado a un canal de voz."

        await self._vsm.request_deferred_disconnect(
            guild_id, vc, on_disconnected_cb=on_disconnected_cb
        )

        if (
            self._vsm.get_active_owner(guild_id) is None
            and self._vsm.get_playback_owner(guild_id) is None
            and self._vsm.voice_clients.get(guild_id) is None
        ):
            return "⏹️ Reproducción detenida y desconectado del canal de voz."

        logger.info(
            "[Guild %s] Reproducción musical detenida; desconexión diferida gestionada (sink='%s', playback='%s').",
            guild_id,
            self._vsm.get_active_owner(guild_id),
            self._vsm.get_playback_owner(guild_id),
        )
        return "⏹️ Reproducción musical detenida (la sesión de voz compartida permanece conectada)."

    async def get_queue(self, guild_id: int | None = None) -> str:
        guild_id = self._require_guild_id(guild_id)
        if not self.current_songs.get(guild_id) and not self.queues.get(guild_id):
            return "No hay canciones en la cola."

        queue_msg = "📋 **Cola de reproducción:**\n"
        if self.current_songs.get(guild_id):
            song = self.current_songs[guild_id]
            assert song is not None
            status = "⏸️ Pausado:" if self.is_paused.get(guild_id, False) else "▶️ Reproduciendo:"
            queue_msg += f"{status} **{song['title']}** - {song.get('artist', 'Desconocido')}\n\n"
        for i, song in enumerate(self.queues.get(guild_id, []), 1):
            queue_msg += f"{i}. **{song['title']}** - {song.get('artist', 'Desconocido')}\n"
        return queue_msg

    # ── Apagado, recomendaciones y comandos de voz ──────────────

    async def cleanup(self) -> None:
        self._vsm.begin_closing()
        if hasattr(self, "cleanup_task") and self.cleanup_task:
            self.cleanup_task.cancel()

        for gid in list(self._play_next_tasks):
            self._invalidate_guild_operations(gid)

        for gid, vc in list(self.voice_clients.items()):
            try:
                if vc is not None and _probe_vc_connected(vc) is True:
                    if vc.is_playing() or vc.is_paused():
                        vc.stop()
            except Exception as exc:
                logger.warning("[Guild %s] Error deteniendo vc en cleanup(): %s", gid, exc)

        logger.info("Recursos del reproductor de música liberados")

    async def _notify_error(self, guild_id: int, message: str) -> None:
        try:
            guild = self.bot.get_guild(guild_id)
            if not guild:
                return
            text_channels = guild.text_channels
            target_channel = None
            for channel in text_channels:
                if channel.name.lower() in ["general", "música", "music", "bot"]:
                    target_channel = channel
                    break
            if not target_channel and text_channels:
                target_channel = text_channels[0]
            if target_channel:
                await target_channel.send(f"❌ {message}")
        except Exception as e:
            log_exception("Error notificando error", e)

    async def get_recommendations(self, user_id: int, num_recommendations: int = 3) -> str:
        try:
            history = await get_user_history(user_id)
            if not history or len(history) < 3:
                return "Necesito más canciones en tu historial para generar recomendaciones personalizadas."
            recommendations = await self.recommendation_system.get_user_recommendations(
                user_id, history, num_recommendations
            )
            if not recommendations:
                return "No pude generar recomendaciones en este momento. Inténtalo más tarde."
            result = "🎵 **Recomendaciones basadas en tu historial:**\n"
            for i, song in enumerate(recommendations, 1):
                result += f"{i}. **{song['title']}** - {song['artist']}\n"
            return result
        except Exception as e:
            log_exception(f"Error obteniendo recomendaciones para {user_id}", e)
            return "Ocurrió un error al intentar obtener recomendaciones."

    def _on_voice_sink_revoked(self, guild_id: int) -> None:
        if guild_id in self.voice_command_mode:
            for uid in list(self.voice_command_mode[guild_id].keys()):
                self.voice_command_mode[guild_id][uid] = False
        logger.info(f"[Guild {guild_id}] Modo comandos de voz cedido al nuevo propietario del sink.")

    async def start_voice_command_mode(self, user_id: int) -> str:
        guild = None
        member = None
        for g in self.bot.guilds:
            m = g.get_member(user_id)
            if m:
                guild = g
                member = m
                break
        if not guild or not member:
            return "No se pudo determinar a qué servidor perteneces."

        guild_id = guild.id
        if not member.voice or not member.voice.channel:
            return "Necesitas estar en un canal de voz para usar comandos de voz."

        try:
            join_success = await self.join_voice_channel(member.voice.channel, require_receive=True)
        except (VoiceReceiveUnavailableError, VoiceClientUpgradeBlockedError) as e:
            return f"❌ {e}"
        if not join_success:
            return "No pude unirme a tu canal de voz."

        voice_client = self.voice_clients.get(guild_id)
        try:
            if (
                guild_id not in self.voice_receivers
                or self.voice_receivers[guild_id].voice_client is None
                or self.voice_receivers[guild_id].voice_client != voice_client
                or self.voice_receivers[guild_id]._sink is None
            ):
                if guild_id in self.voice_receivers:
                    await self.voice_receivers[guild_id].cleanup()
                self.voice_receivers[guild_id] = VoiceReceiver(
                    voice_client=voice_client,
                    sample_rate=48000,
                    guild_id=guild_id,
                    on_revoked=lambda gid=guild_id: self._on_voice_sink_revoked(gid),
                )
                await self.voice_receivers[guild_id].setup_receiver()
        except (VoiceReceiveUnavailableError, VoiceClientUpgradeBlockedError, VoiceClientUnavailableError) as e:
            return f"❌ {e}"

        self.voice_command_mode.setdefault(guild_id, {})
        if not self.voice_command_mode[guild_id].get(user_id, False):
            self.voice_command_mode[guild_id][user_id] = True
            asyncio.create_task(self._voice_command_loop(guild_id, user_id))

        return "🎤 Modo de comandos por voz activado. Puedes hablarme directamente."

    async def stop_voice_command_mode(self, guild_id: int | None = None, user_id: int | None = None) -> str:
        if guild_id is None and user_id is None:
            for gid in list(self.voice_command_mode.keys()):
                self.voice_command_mode[gid] = {}
                if gid in self.voice_receivers:
                    await self.voice_receivers[gid].cleanup()
            return "Modo de comandos por voz desactivado para todos los servidores."

        if guild_id is None and user_id is not None:
            found = False
            for gid in list(self.voice_command_mode.keys()):
                if user_id in self.voice_command_mode[gid]:
                    self.voice_command_mode[gid][user_id] = False
                    found = True
                    if not any(self.voice_command_mode[gid].values()) and gid in self.voice_receivers:
                        await self.voice_receivers[gid].cleanup()
            return "🎤 Modo de comandos por voz desactivado." if found else "No estabas en modo de comandos por voz."

        if user_id is None:
            if guild_id in self.voice_command_mode:
                self.voice_command_mode[guild_id] = {}
                if guild_id in self.voice_receivers:
                    await self.voice_receivers[guild_id].cleanup()
                return "Modo de comandos por voz desactivado para todo el servidor."
            return "El servidor no tenía activado el modo de comandos por voz."

        if guild_id in self.voice_command_mode and user_id in self.voice_command_mode[guild_id]:
            self.voice_command_mode[guild_id][user_id] = False
            if not any(self.voice_command_mode[guild_id].values()) and guild_id in self.voice_receivers:
                await self.voice_receivers[guild_id].cleanup()
            return "🎤 Modo de comandos por voz desactivado."

        return "No estabas en modo de comandos por voz."

    async def _voice_command_loop(self, guild_id: int, user_id: int) -> None:
        logger.info(f"Iniciando loop de captura de voz para usuario {user_id} en guild {guild_id}")
        try:
            voice_client = self.voice_clients.get(guild_id)
            if guild_id not in self.voice_receivers or self.voice_receivers[guild_id].voice_client is None:
                self.voice_receivers[guild_id] = VoiceReceiver(
                    voice_client=voice_client, sample_rate=48000, guild_id=guild_id
                )
                await self.voice_receivers[guild_id].setup_receiver()

            voice_receiver = self.voice_receivers[guild_id]
            await voice_receiver.start_recording(user_id)

            while (
                guild_id in self.voice_command_mode
                and user_id in self.voice_command_mode.get(guild_id, {})
                and self.voice_command_mode[guild_id].get(user_id, False)
            ):
                guild = self.bot.get_guild(guild_id)
                if not guild:
                    break
                member = guild.get_member(user_id)
                if not member or not member.voice or not member.voice.channel:
                    logger.info(
                        f"Usuario {user_id} ya no está en un canal de voz. Desactivando modo de voz."
                    )
                    self.voice_command_mode[guild_id][user_id] = False
                    break

                result = await capture_and_process_voice(
                    user_id,
                    guild_id,
                    voice_receiver,
                    is_cancelled_cb=lambda gid=guild_id, uid=user_id: not self.voice_command_mode.get(
                        gid, {}
                    ).get(uid, False),
                )

                if result:
                    if not self.voice_command_mode.get(guild_id, {}).get(user_id, False):
                        break
                    transcription, intent, entities = result
                    logger.info(f"Comando de voz detectado: {transcription} (intent: {intent})")

                    if intent in ("stop", "detener") and (
                        "escucha" in transcription.lower() or "listening" in transcription.lower()
                    ):
                        self.voice_command_mode[guild_id][user_id] = False
                        await self._notify_user(guild_id, user_id, "🎤 Modo de comandos por voz desactivado.")
                        break

                    if self.voice_command_handler:
                        response = await self.voice_command_handler.handle_voice_command(
                            user_id, guild_id, intent, entities, transcription
                        )
                        if response:
                            await self._notify_user(guild_id, user_id, response)
                    else:
                        await self._process_basic_voice_command(guild_id, user_id, intent, entities)

                await asyncio.sleep(0.1)

            await voice_receiver.stop_recording(user_id)
        except Exception as e:
            log_exception(f"Error en loop de captura de voz para {user_id}", e)
            if guild_id in self.voice_command_mode and user_id in self.voice_command_mode[guild_id]:
                self.voice_command_mode[guild_id][user_id] = False
            if guild_id in self.voice_receivers:
                await self.voice_receivers[guild_id].stop_recording(user_id)
            await self._notify_user(
                guild_id,
                user_id,
                "❌ Se produjo un error en el modo de comandos por voz. Intenta activarlo nuevamente.",
            )

    async def _process_basic_voice_command(
        self, guild_id: int, user_id: int, intent: str, entities: list[dict[str, Any]]
    ) -> None:
        try:
            response = None
            if intent == "play_song":
                song_entity = next((e for e in entities if e["type"] == "song"), None)
                query = song_entity["value"] if song_entity else ""
                if query:
                    response = await self.play_song(query, user_id)
            elif intent == "pause":
                response = await self.pause(guild_id)
            elif intent == "resume":
                response = await self.resume(guild_id)
            elif intent == "skip":
                response = await self.skip(guild_id)
            elif intent == "stop":
                response = await self.stop(guild_id)
            elif intent == "volume":
                vol_entity = next((e for e in entities if e["type"] == "volume_level"), None)
                if vol_entity:
                    volume_level = max(0, min(100, int(vol_entity["value"]))) / 100.0
                    response = await self.set_volume(volume_level, guild_id)
            elif intent == "queue":
                response = await self.get_queue(guild_id)
            elif intent == "help":
                response = (
                    "🎙️ **Comandos de voz:** reproduce, pausa, continúa, salta, "
                    "detén la música, volumen [0-100], muestra la cola."
                )
            if response:
                await self._notify_user(guild_id, user_id, response)
        except Exception as e:
            log_exception(f"Error procesando comando de voz básico: {intent}", e)

    def set_voice_text_channel(self, guild_id: int, channel_id: int) -> None:
        self._voice_text_channels[guild_id] = channel_id

    async def _notify_user(self, guild_id: int, user_id: int, message: str) -> None:
        try:
            guild = self.bot.get_guild(guild_id)
            if not guild:
                return
            channel_id = self._voice_text_channels.get(guild_id)
            if channel_id:
                channel = guild.get_channel(channel_id)
                if channel:
                    await channel.send(f"<@{user_id}> {message}")
                    return
            member = guild.get_member(user_id)
            if member:
                for channel in guild.text_channels:
                    perms = channel.permissions_for(member)
                    if perms.read_messages and perms.send_messages:
                        await channel.send(f"<@{user_id}> {message}")
                        return
        except Exception as e:
            log_exception(f"Error notificando al usuario {user_id}", e)
