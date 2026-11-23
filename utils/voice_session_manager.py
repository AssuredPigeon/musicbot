# utils/voice_session_manager.py
#
# Administrador centralizado de conexiones VoiceClient, AudioSinks, arbitraje de
# reproducción y desconexiones diferidas por servidor (Bloque 0A v3.3.13).

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

import discord

logger = logging.getLogger("musicbot.voice_session")

CONNECT_INTERNAL_TIMEOUT_S = 10.0
CONNECT_OUTER_TIMEOUT_S = 12.0
MOVE_TIMEOUT_S = 10.0

try:
    from discord.ext import voice_recv  # type: ignore

    HAS_VOICE_RECV = True
    AudioSinkBase = voice_recv.AudioSink
    VoiceDataType = voice_recv.VoiceData
    VoiceRecvClientCls = voice_recv.VoiceRecvClient
except Exception as _exc:  # pragma: no cover - fallback defensivo
    HAS_VOICE_RECV = False
    AudioSinkBase = object
    VoiceDataType = Any
    VoiceRecvClientCls = None
    logger.warning("discord-ext-voice-recv no disponible (%s). Captura de voz deshabilitada.", _exc)


class VoiceReceiveUnavailableError(RuntimeError):
    """Se lanza cuando se solicita recepción de audio pero voice_recv no está instalado."""


class VoiceClientUpgradeBlockedError(RuntimeError):
    """Se lanza cuando hay música activa en un VoiceClient estándar que no puede reconectarse sin cortarla."""


class VoiceClientUnavailableError(RuntimeError):
    """Se lanza cuando no existe un VoiceClient conectado, válido y disponible para operar."""


class VoiceClientRecoveryError(RuntimeError):
    """Se lanza cuando fallan las postcondiciones o la reconciliación forzada de un VoiceClient."""


class PlaybackPreemptionTimeoutError(RuntimeError):
    """Se lanza cuando expira preempt_timeout y el cliente físico sigue ocupado."""


class PlaybackStateInconsistencyError(RuntimeError):
    """Se lanza cuando el estado físico/token de reproducción presenta una inconsistencia sin evento."""


@dataclass
class DeferredDisconnectRecord:
    task: asyncio.Task[Any] | None
    dc_token: int
    vc: Any
    state: Literal["PENDING", "RUNNING", "COMPLETED"]
    done_event: asyncio.Event
    aborted_event: asyncio.Event


@dataclass
class DisconnectFinalizationResult:
    confirmed: bool
    still_connected: bool | None


def _probe_vc_connected(vc: Any) -> bool | None:
    """
    Sonda triestado obligatoria de conectividad física.
    - False: desconexión física confirmada.
    - True: conectado confirmado.
    - None: estado indeterminado (None, no booleano o excepción), jamás colapsado a False.
    """
    if vc is None:
        return False
    try:
        fn = vc.is_connected
        res = fn() if callable(fn) else fn
    except Exception:
        return None
    return res if isinstance(res, bool) else None


class VoiceSessionManager:
    """
    Coordina VoiceClient autoritativo único, AudioSink, árbitro de reproducción
    y ciclo de vida/reconciliación de conexiones por guild.
    """

    def __init__(self) -> None:
        # Diccionario autoritativo único de VoiceClients por guild (mutado solo bajo _get_lock)
        self.voice_clients: dict[int, Any] = {}

        # Propiedad de AudioSink
        self._active_tokens: dict[int, int] = {}
        self._active_owners: dict[int, str] = {}
        self._active_sinks: dict[int, Any] = {}
        self._revoke_callbacks: dict[int, Callable[[], None]] = {}

        # Monótono global de tokens (playback, sink, disconnect)
        self._token_counter: int = 0

        # Jerarquía acíclica estricta: jamás se anidan _locks y _playback_locks
        self._locks: dict[int, asyncio.Lock] = {}
        self._playback_locks: dict[int, asyncio.Lock] = {}

        # Árbitro de reproducción
        self._playback_owners: dict[int, str] = {}
        self._playback_tokens: dict[int, int] = {}
        self._playback_done_events: dict[int, asyncio.Event] = {}
        self._music_waiting_preempt: dict[int, int] = {}

        # Transiciones, reconciliación y desconexiones diferidas
        self._voice_transitioning: dict[int, bool] = {}
        self._unreconciled_voice_clients: dict[
            int, list[tuple[Any, Literal["FAILED", "CANCELLED_OR_INDETERMINATE"]]]
        ] = {}
        self._deferred_disconnects: dict[int, tuple[int, Any, Callable[[Any], Any] | None]] = {}
        self._deferred_disconnect_tasks: dict[int, DeferredDisconnectRecord] = {}
        self._finalizer_tasks: set[asyncio.Task[Any]] = set()
        self._lifecycle_tasks: set[asyncio.Task[Any]] = set()
        self._finalizer_errors: list[BaseException] = []
        self._closing: bool = False

        # Confinamiento al hilo del event loop y métricas fail-closed thread-safe
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_id: int | None = None
        self._closed_loop_metrics_lock = threading.Lock()
        self.dropped_after_callbacks_on_closed_loop: int = 0
        self._incomplete_close_reconciliations: list[
            tuple[int, int | None, int | None, str, float]
        ] = []

        # Callbacks hacia MusicPlayer / observadores
        self._vc_lifecycle_callbacks: dict[str, Callable[..., Any]] = {}
        self._is_music_active_cb: Callable[[int], bool] | None = None

    # ── Utilidades de hilo, locks y callbacks ────────────────────

    @staticmethod
    def _require_guild_id(guild_id: int | None) -> int:
        if guild_id is None or isinstance(guild_id, bool) or not isinstance(guild_id, int):
            raise ValueError(f"guild_id inválido o None: {guild_id!r}")
        return guild_id

    def _next_token(self) -> int:
        self._token_counter += 1
        return self._token_counter

    def _get_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._locks:
            self._locks[guild_id] = asyncio.Lock()
        return self._locks[guild_id]

    def _get_playback_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._playback_locks:
            self._playback_locks[guild_id] = asyncio.Lock()
        return self._playback_locks[guild_id]

    def _bind_or_assert_loop_thread(self) -> int:
        tid = threading.get_ident()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise AssertionError(
                "Operación de VoiceSessionManager ejecutada fuera del hilo del event loop"
            ) from exc

        if self._loop is None or self._loop.is_closed() or self._loop is running:
            self._loop = running
            self._loop_thread_id = tid
        elif self._loop_thread_id is not None and tid != self._loop_thread_id:
            raise AssertionError(
                f"Operación en hilo ajeno al event loop ({tid} != {self._loop_thread_id})"
            )
        return tid

    def record_dropped_after_on_closed_loop(
        self,
        guild_id: int,
        song_token: int | None,
        pb_token: int | None,
        *,
        reason: str,
    ) -> None:
        """Registro fail-closed thread-safe invocable desde hilos secundarios o el event loop."""
        entry = (guild_id, song_token, pb_token, reason, time.monotonic())
        with self._closed_loop_metrics_lock:
            self.dropped_after_callbacks_on_closed_loop += 1
            self._incomplete_close_reconciliations.append(entry)

    def get_incomplete_close_reconciliations(
        self,
    ) -> list[tuple[int, int | None, int | None, str, float]]:
        with self._closed_loop_metrics_lock:
            return list(self._incomplete_close_reconciliations)

    def register_vc_lifecycle_callback(
        self, cb: Callable[..., Any], *, key: str = "default"
    ) -> None:
        self._vc_lifecycle_callbacks[key] = cb

    def unregister_vc_lifecycle_callback(self, key: str = "default") -> None:
        self._vc_lifecycle_callbacks.pop(key, None)

    def set_music_active_callback(self, cb: Callable[[int], bool] | None) -> None:
        self._is_music_active_cb = cb

    def _is_music_active(self, guild_id: int) -> bool:
        if self._is_music_active_cb is None:
            return False
        try:
            return bool(self._is_music_active_cb(guild_id))
        except Exception:
            return False

    def _emit_vc_lifecycle_change(
        self,
        guild_id: int,
        *,
        vc: Any = None,
        physically_disconnected: bool = False,
    ) -> None:
        self._bind_or_assert_loop_thread()
        assert self._get_lock(guild_id).locked()
        for cb in list(self._vc_lifecycle_callbacks.values()):
            try:
                cb(guild_id, vc=vc, physically_disconnected=physically_disconnected)
            except Exception as exc:
                logger.warning(
                    "[Guild %s] Error en callback on_vc_lifecycle_change: %s", guild_id, exc
                )

    # ── Consultas de solo lectura ────────────────────────────────

    def get_playback_owner(self, guild_id: int) -> str | None:
        return self._playback_owners.get(guild_id)

    def get_playback_token(self, guild_id: int) -> int | None:
        return self._playback_tokens.get(guild_id)

    def get_active_owner(self, guild_id: int) -> str | None:
        return self._active_owners.get(guild_id)

    def get_active_token(self, guild_id: int) -> int | None:
        return self._active_tokens.get(guild_id)

    def is_voice_transitioning(self, guild_id: int) -> bool:
        return bool(self._voice_transitioning.get(guild_id, False))

    def has_unreconciled_voice_client(self, guild_id: int) -> bool:
        return guild_id in self._unreconciled_voice_clients

    def is_closing(self) -> bool:
        return self._closing

    # ── Gestión interna de _unreconciled_voice_clients bajo lock ─

    def _register_unreconciled_locked(
        self,
        guild_id: int,
        vc: Any,
        reason: Literal["FAILED", "CANCELLED_OR_INDETERMINATE"],
    ) -> None:
        assert self._get_lock(guild_id).locked()
        if vc is None:
            return
        entries = self._unreconciled_voice_clients.setdefault(guild_id, [])
        for idx, (entry_vc, prev_reason) in enumerate(entries):
            if entry_vc is vc:
                if reason == "CANCELLED_OR_INDETERMINATE" and prev_reason != reason:
                    entries[idx] = (vc, reason)
                return
        entries.append((vc, reason))

    def _remove_unreconciled_locked(self, guild_id: int, vc: Any) -> None:
        assert self._get_lock(guild_id).locked()
        entries = self._unreconciled_voice_clients.get(guild_id)
        if not entries:
            return
        filtered = [(entry_vc, r) for entry_vc, r in entries if entry_vc is not vc]
        if filtered:
            self._unreconciled_voice_clients[guild_id] = filtered
        else:
            self._unreconciled_voice_clients.pop(guild_id, None)

    # ── Árbitro de reproducción por servidor ─────────────────────

    def _issue_playback_token(
        self,
        guild_id: int,
        owner: str,
        *,
        expected_vc: Any = None,
    ) -> int | None:
        self._bind_or_assert_loop_thread()
        current_vc = self.voice_clients.get(guild_id)
        dc_rec = self._deferred_disconnect_tasks.get(guild_id)

        # Rama A: apagado, transición, cliente ausente/desconectado/indeterminado/obsoleto o desconexión
        if (
            self._closing
            or self._voice_transitioning.get(guild_id, False)
            or current_vc is None
            or _probe_vc_connected(current_vc) is not True
            or (expected_vc is not None and current_vc is not expected_vc)
            or guild_id in self._unreconciled_voice_clients
            or guild_id in self._deferred_disconnects
            or (dc_rec is not None and dc_rec.state in ("PENDING", "RUNNING"))
        ):
            if owner == "music":
                raise VoiceClientUnavailableError(
                    f"[Guild {guild_id}] VoiceClient no disponible para reproducción musical."
                )
            logger.debug("[Guild %s] Adquisición TTS rechazada en Rama A.", guild_id)
            return None

        # Rama B: cliente físicamente ocupado sin token activo
        if _vc_is_physically_busy(current_vc):
            logger.error(
                "[Guild %s] Inconsistencia física: VoiceClient ocupado sin playback_token activo (owner=%s).",
                guild_id,
                owner,
            )
            if owner == "tts":
                return None
            stop_fn = getattr(current_vc, "stop", None)
            if callable(stop_fn):
                try:
                    stop_fn()
                except Exception as exc:
                    logger.warning(
                        "[Guild %s] Error deteniendo fuente inconsistente en vc: %s", guild_id, exc
                    )
            raise PlaybackStateInconsistencyError(
                f"[Guild {guild_id}] VoiceClient físicamente ocupado sin token de reproducción."
            )

        # Rama C: cliente conectado, válido y físicamente libre
        new_pb_token = self._next_token()
        self._playback_done_events[new_pb_token] = asyncio.Event()
        self._playback_tokens[guild_id] = new_pb_token
        self._playback_owners[guild_id] = owner
        return new_pb_token

    def _force_revoke_playback(self, guild_id: int, token: int) -> None:
        self._bind_or_assert_loop_thread()
        if self._playback_tokens.get(guild_id) == token:
            self._playback_tokens.pop(guild_id, None)
            self._playback_owners.pop(guild_id, None)
        ev = self._playback_done_events.pop(token, None)
        if ev is not None and not ev.is_set():
            ev.set()

    def _resolve_missing_event_inconsistency(
        self,
        guild_id: int,
        active_token: int,
        expected_vc: Any,
        deadline: float,
    ) -> int:
        self._bind_or_assert_loop_thread()
        remaining = max(0.0, deadline - time.monotonic())
        owner = self._playback_owners.get(guild_id)
        current_vc = self.voice_clients.get(guild_id)
        logger.error(
            "[Guild %s] Inconsistencia de estado interno: reserva activa token=%s (owner=%s) sin done_event (remaining=%.3fs).",
            guild_id,
            active_token,
            owner,
            remaining,
        )
        if _probe_vc_connected(current_vc) is True and _vc_is_physically_busy(current_vc):
            raise PlaybackStateInconsistencyError(
                f"[Guild {guild_id}] Reserva token={active_token} sin done_event mientras vc sigue ocupado."
            )
        logger.warning(
            "[Guild %s] Recuperación controlada de reserva huérfana sin evento token=%s (owner=%s).",
            guild_id,
            active_token,
            owner,
        )
        self._force_revoke_playback(guild_id, active_token)
        pb_tok = self._issue_playback_token(guild_id, "music", expected_vc=expected_vc)
        assert pb_tok is not None
        return pb_tok

    def try_acquire_tts_playback(
        self,
        guild_id: int,
        expected_vc: Any = None,
        can_play_cb: Callable[[], bool] | None = None,
    ) -> int | None:
        """Reserva síncrona de salida para TTS; jamás agenda cancelaciones fire-and-forget."""
        self._bind_or_assert_loop_thread()
        guild_id = self._require_guild_id(guild_id)

        pb_lock = self._playback_locks.get(guild_id)
        if pb_lock is not None and pb_lock.locked():
            return None
        if self._music_waiting_preempt.get(guild_id, 0) > 0:
            return None
        if self._playback_owners.get(guild_id) is not None or self._playback_tokens.get(guild_id) is not None:
            return None
        if can_play_cb is not None and not can_play_cb():
            return None
        return self._issue_playback_token(guild_id, "tts", expected_vc=expected_vc)

    async def acquire_tts_playback(
        self,
        guild_id: int,
        *,
        expected_vc: Any = None,
        can_play_cb: Callable[[], bool] | None = None,
    ) -> int | None:
        """Confirma cancelación de PENDING bajo _get_lock fuera de _get_playback_lock y reserva TTS."""
        self._bind_or_assert_loop_thread()
        guild_id = self._require_guild_id(guild_id)
        await self.cancel_deferred_disconnect(guild_id)
        return self.try_acquire_tts_playback(
            guild_id, expected_vc=expected_vc, can_play_cb=can_play_cb
        )

    async def acquire_music_playback(
        self,
        guild_id: int,
        expected_vc: Any = None,
        preempt_timeout: float = 1.0,
    ) -> int:
        """
        Reserva el canal de salida para música en 2 fases sin deadlock ni anidamiento de locks.
        """
        self._bind_or_assert_loop_thread()
        guild_id = self._require_guild_id(guild_id)
        deadline = time.monotonic() + max(0.0, float(preempt_timeout))

        self._music_waiting_preempt[guild_id] = self._music_waiting_preempt.get(guild_id, 0) + 1
        try:
            while True:
                # Cancelar desconexión diferida pendiente FUERA de _get_playback_lock
                await self.cancel_deferred_disconnect(guild_id)

                done_ev: asyncio.Event | None = None
                prev_token: int | None = None
                async with self._get_playback_lock(guild_id):
                    prev_token = self._playback_tokens.get(guild_id)
                    current_vc = self.voice_clients.get(guild_id)

                    if prev_token is None:
                        pb_tok = self._issue_playback_token(
                            guild_id, "music", expected_vc=expected_vc
                        )
                        assert pb_tok is not None
                        return pb_tok

                    if (
                        self._playback_owners.get(guild_id) == "tts"
                        and _probe_vc_connected(current_vc) is True
                        and _vc_is_physically_busy(current_vc)
                    ):
                        try:
                            current_vc.stop()
                        except Exception as exc:
                            logger.warning(
                                "[Guild %s] Error interrumpiendo TTS por prioridad musical: %s",
                                guild_id,
                                exc,
                            )

                    done_ev = self._playback_done_events.get(prev_token)
                    if done_ev is None:
                        return self._resolve_missing_event_inconsistency(
                            guild_id, prev_token, expected_vc, deadline
                        )

                # Espera fuera de _get_playback_lock
                timed_out = False
                remaining = max(0.0, deadline - time.monotonic())
                if not done_ev.is_set() and remaining > 0.0:
                    try:
                        await asyncio.wait_for(done_ev.wait(), timeout=remaining)
                    except asyncio.TimeoutError:
                        timed_out = True
                elif not done_ev.is_set() and time.monotonic() >= deadline:
                    timed_out = True

                # Cancelar nueva solicitud diferida llegada durante la espera FUERA de _get_playback_lock
                await self.cancel_deferred_disconnect(guild_id)

                async with self._get_playback_lock(guild_id):
                    cur_token = self._playback_tokens.get(guild_id)
                    current_vc = self.voice_clients.get(guild_id)

                    if cur_token is None:
                        pb_tok = self._issue_playback_token(
                            guild_id, "music", expected_vc=expected_vc
                        )
                        assert pb_tok is not None
                        return pb_tok

                    if timed_out or time.monotonic() >= deadline:
                        if _probe_vc_connected(current_vc) is True and _vc_is_physically_busy(current_vc):
                            raise PlaybackPreemptionTimeoutError(
                                f"[Guild {guild_id}] Timeout ({preempt_timeout}s) con VoiceClient aún ocupado (token={cur_token})."
                            )
                        cur_owner = self._playback_owners.get(guild_id)
                        logger.warning(
                            "[Guild %s] Timeout (%ss) expirado; revocando reserva huérfana token=%s (owner=%s).",
                            guild_id,
                            preempt_timeout,
                            cur_token,
                            cur_owner,
                        )
                        self._force_revoke_playback(guild_id, cur_token)
                        pb_tok = self._issue_playback_token(
                            guild_id, "music", expected_vc=expected_vc
                        )
                        assert pb_tok is not None
                        return pb_tok

                    if cur_token == prev_token:
                        return self._resolve_missing_event_inconsistency(
                            guild_id, cur_token, expected_vc, deadline
                        )
                    next_ev = self._playback_done_events.get(cur_token)
                    if next_ev is None:
                        return self._resolve_missing_event_inconsistency(
                            guild_id, cur_token, expected_vc, deadline
                        )
        finally:
            new_val = max(0, self._music_waiting_preempt.get(guild_id, 1) - 1)
            if new_val == 0:
                self._music_waiting_preempt.pop(guild_id, None)
                if guild_id in self._deferred_disconnects and not self._closing:
                    self._schedule_evaluate_deferred_disconnect(guild_id)
            else:
                self._music_waiting_preempt[guild_id] = new_val

    def release_playback(
        self,
        guild_id: int,
        playback_token: int | None = None,
        owner: str | None = None,
    ) -> bool:
        """
        Libera reserva de reproducción síncronamente en el hilo del event loop sin adquirir locks.
        Idempotente: segunda llamada o token no coincidente retorna False sin efecto.
        """
        self._bind_or_assert_loop_thread()
        if guild_id is None or playback_token is None:
            return False
        if self._playback_tokens.get(guild_id) != playback_token:
            return False
        if owner is not None and self._playback_owners.get(guild_id) != owner:
            return False

        self._playback_tokens.pop(guild_id, None)
        self._playback_owners.pop(guild_id, None)
        ev = self._playback_done_events.pop(playback_token, None)
        if ev is not None and not ev.is_set():
            ev.set()

        if guild_id in self._deferred_disconnects and not self._closing:
            self._schedule_evaluate_deferred_disconnect(guild_id)
        return True

    def notify_music_idle(self, guild_id: int) -> None:
        self._bind_or_assert_loop_thread()
        if guild_id in self._deferred_disconnects and not self._closing:
            self._schedule_evaluate_deferred_disconnect(guild_id)

    # ── Propiedad exclusiva de AudioSink ───────────────────────────

    @staticmethod
    def supports_voice_receive(voice_client: Any) -> bool:
        if voice_client is None or not HAS_VOICE_RECV or VoiceRecvClientCls is None:
            return False
        return isinstance(voice_client, VoiceRecvClientCls) or (
            hasattr(voice_client, "listen")
            and hasattr(voice_client, "is_listening")
            and hasattr(voice_client, "stop_listening")
        )

    @staticmethod
    def _safe_stop_and_cleanup(voice_client: Any, sink: Any) -> None:
        if voice_client is not None:
            try:
                is_listening = getattr(voice_client, "is_listening", None)
                if callable(is_listening) and is_listening():
                    voice_client.stop_listening()
            except Exception as exc:
                logger.warning("Error al detener escucha en VoiceClient: %s", exc)

        if sink is not None:
            try:
                cleanup = getattr(sink, "cleanup", None)
                if callable(cleanup):
                    cleanup()
            except Exception as exc:
                logger.warning("Error limpiando AudioSink saliente: %s", exc)

    async def acquire_sink(
        self,
        guild_id: int,
        voice_client: Any,
        owner_mode: str,
        sink: Any,
        on_revoked: Callable[[], None] | None = None,
    ) -> int:
        self._bind_or_assert_loop_thread()
        guild_id = self._require_guild_id(guild_id)

        async with self._get_lock(guild_id):
            assert self._get_lock(guild_id).locked()
            if self._closing or self._voice_transitioning.get(guild_id, False):
                raise VoiceClientUnavailableError(
                    f"[Guild {guild_id}] VoiceClient no disponible para instalar AudioSink."
                )

            self._cancel_deferred_disconnect_locked(guild_id)

            rec = self._deferred_disconnect_tasks.get(guild_id)
            if rec is not None and rec.state == "RUNNING":
                if rec.aborted_event.is_set() or rec.done_event.is_set():
                    self._reconcile_aborted_record_locked(guild_id, rec)
                else:
                    raise VoiceClientUnavailableError(
                        f"[Guild {guild_id}] Desconexión RUNNING en progreso."
                    )

            vc_state = _probe_vc_connected(voice_client)
            if (
                guild_id in self._unreconciled_voice_clients
                or self.voice_clients.get(guild_id) is not voice_client
                or vc_state is not True
            ):
                if voice_client is self.voice_clients.get(guild_id) and vc_state is None:
                    self._register_unreconciled_locked(
                        guild_id, voice_client, "CANCELLED_OR_INDETERMINATE"
                    )
                    self._emit_vc_lifecycle_change(
                        guild_id, vc=voice_client, physically_disconnected=False
                    )
                raise VoiceClientUnavailableError(
                    f"[Guild {guild_id}] VoiceClient inválido, desconectado o pendiente de reconciliación."
                )

            if not self.supports_voice_receive(voice_client):
                raise VoiceReceiveUnavailableError(
                    "El VoiceClient conectado no soporta recepción de audio (`VoiceRecvClient`)."
                )

            old_token = self._active_tokens.pop(guild_id, None)
            old_owner = self._active_owners.pop(guild_id, None)
            old_sink = self._active_sinks.pop(guild_id, None)
            old_revoke_cb = self._revoke_callbacks.pop(guild_id, None)

            if old_token is not None:
                logger.info(
                    "[Guild %s] Reemplazando sink activo '%s' (token=%s) por '%s'.",
                    guild_id,
                    old_owner,
                    old_token,
                    owner_mode,
                )
                self._safe_stop_and_cleanup(voice_client, old_sink)
                if old_revoke_cb is not None:
                    try:
                        old_revoke_cb()
                    except Exception as exc:
                        logger.warning(
                            "Error en callback de revocación para '%s': %s", old_owner, exc
                        )

            token = self._next_token()
            self._active_tokens[guild_id] = token
            self._active_owners[guild_id] = owner_mode
            self._active_sinks[guild_id] = sink
            if on_revoked is not None:
                self._revoke_callbacks[guild_id] = on_revoked

            voice_client.listen(sink)
            logger.info("[Guild %s] AudioSink '%s' adquirido (token=%s).", guild_id, owner_mode, token)
            return token

    def release_sink(
        self, guild_id: int, session_token: int | None, voice_client: Any = None
    ) -> bool:
        self._bind_or_assert_loop_thread()
        if session_token is None or session_token != self._active_tokens.get(guild_id):
            return False

        owner = self._active_owners.pop(guild_id, None)
        self._active_tokens.pop(guild_id, None)
        sink = self._active_sinks.pop(guild_id, None)
        self._revoke_callbacks.pop(guild_id, None)

        self._safe_stop_and_cleanup(voice_client, sink)
        logger.info("[Guild %s] AudioSink '%s' liberado (token=%s).", guild_id, owner, session_token)

        if not self._active_owners.get(guild_id) and guild_id in self._deferred_disconnects and not self._closing:
            self._schedule_evaluate_deferred_disconnect(guild_id)
        return True

    # ── Tareas en segundo plano, desconexión diferida y finalizadores ─

    def _track_lifecycle_task(self, task: asyncio.Task[Any]) -> None:
        self._lifecycle_tasks.add(task)
        task.add_done_callback(self._on_lifecycle_task_done)

    def _on_lifecycle_task_done(self, task: asyncio.Task[Any]) -> None:
        self._lifecycle_tasks.discard(task)
        if task.cancelled():
            if not self._closing:
                err = asyncio.CancelledError("Lifecycle task cancelada fuera de cierre")
                self._finalizer_errors.append(err)
                logger.error("Lifecycle task cancelada inesperadamente fuera de cierre.")
            return
        exc = task.exception()
        if exc is not None:
            self._finalizer_errors.append(exc)
            logger.error("Excepción inesperada en lifecycle task: %s", exc, exc_info=exc)

    def _on_finalizer_done(self, task: asyncio.Task[Any]) -> None:
        self._finalizer_tasks.discard(task)
        if task.cancelled():
            if not self._closing:
                err = asyncio.CancelledError("Finalizer task cancelada fuera de cierre")
                self._finalizer_errors.append(err)
                logger.error("Finalizer task cancelada anómalamente fuera de cierre.")
            return
        exc = task.exception()
        if exc is not None:
            self._finalizer_errors.append(exc)
            logger.error("Excepción inesperada en finalizer task: %s", exc, exc_info=exc)

    def _schedule_evaluate_deferred_disconnect(self, guild_id: int) -> None:
        if self._closing:
            return
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._evaluate_deferred_disconnect_under_lock(guild_id))
        self._track_lifecycle_task(task)

    def _schedule_aborted_finalizer_recovery(
        self, guild_id: int, record: DeferredDisconnectRecord
    ) -> None:
        if self._closing:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._recover_aborted_finalizer_under_lock(guild_id, record))
        self._track_lifecycle_task(task)

    async def _recover_aborted_finalizer_under_lock(
        self, guild_id: int, record: DeferredDisconnectRecord
    ) -> None:
        if self._closing:
            return
        async with self._get_lock(guild_id):
            assert self._get_lock(guild_id).locked()
            if self._closing:
                return
            self._reconcile_aborted_record_locked(guild_id, record)

    def _reconcile_aborted_record_locked(
        self, guild_id: int, record: DeferredDisconnectRecord
    ) -> None:
        self._bind_or_assert_loop_thread()
        assert self._get_lock(guild_id).locked()

        if record.state == "COMPLETED" or self._deferred_disconnect_tasks.get(guild_id) is not record:
            record.state = "COMPLETED"
            record.done_event.set()
            return

        if record.state == "PENDING":
            pending = self._deferred_disconnects.get(guild_id)
            if pending is not None and pending[0] == record.dc_token:
                self._deferred_disconnects.pop(guild_id, None)
            record.state = "COMPLETED"
            if self._deferred_disconnect_tasks.get(guild_id) is record:
                self._deferred_disconnect_tasks.pop(guild_id, None)
            record.done_event.set()
            return

        # record.state == "RUNNING"
        still_connected = _probe_vc_connected(record.vc)
        if still_connected is False:
            self._emit_vc_lifecycle_change(guild_id, vc=record.vc, physically_disconnected=True)
            if self.voice_clients.get(guild_id) is record.vc:
                self.voice_clients.pop(guild_id, None)
            self._remove_unreconciled_locked(guild_id, record.vc)
        else:
            self._emit_vc_lifecycle_change(guild_id, vc=record.vc, physically_disconnected=False)
            self._register_unreconciled_locked(guild_id, record.vc, "CANCELLED_OR_INDETERMINATE")

        self._deferred_disconnects.pop(guild_id, None)
        record.state = "COMPLETED"
        if self._deferred_disconnect_tasks.get(guild_id) is record:
            self._deferred_disconnect_tasks.pop(guild_id, None)
        record.done_event.set()

    def _disconnect_conditions_clear_locked(
        self, guild_id: int, dc_token: int, vc: Any
    ) -> tuple[bool, bool, bool | None]:
        """Retorna (listo_para_programar, debe_descartar_pending, vc_state)."""
        assert self._get_lock(guild_id).locked()
        vc_state = _probe_vc_connected(vc)
        should_discard = bool(
            self._closing
            or self._voice_transitioning.get(guild_id, False)
            or guild_id in self._unreconciled_voice_clients
            or vc is not self.voice_clients.get(guild_id)
            or vc_state is not True
        )
        if should_discard:
            return False, True, vc_state

        blockers = bool(
            self._active_owners.get(guild_id)
            or self._playback_owners.get(guild_id) is not None
            or self._music_waiting_preempt.get(guild_id, 0) > 0
            or self._is_music_active(guild_id)
        )
        if blockers:
            return False, False, vc_state

        existing_rec = self._deferred_disconnect_tasks.get(guild_id)
        if existing_rec is not None and existing_rec.state in ("PENDING", "RUNNING"):
            return False, False, vc_state

        return True, False, vc_state

    def _schedule_deferred_disconnect_task_locked(
        self,
        guild_id: int,
        dc_token: int,
        vc: Any,
        on_disconnected_cb: Callable[[Any], Any] | None,
    ) -> DeferredDisconnectRecord:
        assert self._get_lock(guild_id).locked()
        loop = asyncio.get_running_loop()
        record = DeferredDisconnectRecord(
            task=None,
            dc_token=dc_token,
            vc=vc,
            state="PENDING",
            done_event=asyncio.Event(),
            aborted_event=asyncio.Event(),
        )
        task = loop.create_task(
            self._run_deferred_disconnect(guild_id, record, dc_token, vc, on_disconnected_cb)
        )
        record.task = task

        def _consume_bg_exc(t: asyncio.Task[Any]) -> None:
            if not t.cancelled():
                _ = t.exception()

        task.add_done_callback(_consume_bg_exc)
        self._deferred_disconnect_tasks[guild_id] = record
        return record

    async def _evaluate_deferred_disconnect_under_lock(self, guild_id: int) -> None:
        self._bind_or_assert_loop_thread()
        if self._closing:
            return
        async with self._get_lock(guild_id):
            assert self._get_lock(guild_id).locked()
            existing_rec = self._deferred_disconnect_tasks.get(guild_id)
            if (
                existing_rec is not None
                and existing_rec.state == "RUNNING"
                and (existing_rec.aborted_event.is_set() or existing_rec.done_event.is_set())
            ):
                self._reconcile_aborted_record_locked(guild_id, existing_rec)

            pending = self._deferred_disconnects.get(guild_id)
            if pending is None:
                return
            dc_token, pending_vc, on_cb = pending
            ready, discard, vc_state = self._disconnect_conditions_clear_locked(
                guild_id, dc_token, pending_vc
            )
            if discard:
                if pending_vc is self.voice_clients.get(guild_id) and vc_state is None:
                    self._register_unreconciled_locked(
                        guild_id, pending_vc, "CANCELLED_OR_INDETERMINATE"
                    )
                    self._emit_vc_lifecycle_change(
                        guild_id, vc=pending_vc, physically_disconnected=False
                    )
                self._deferred_disconnects.pop(guild_id, None)
                return
            if ready:
                self._schedule_deferred_disconnect_task_locked(guild_id, dc_token, pending_vc, on_cb)

    def _cancel_deferred_disconnect_locked(
        self, guild_id: int, *, max_dc_token: int | None = None
    ) -> None:
        assert self._get_lock(guild_id).locked()
        pending = self._deferred_disconnects.get(guild_id)
        if pending is not None and (max_dc_token is None or pending[0] <= max_dc_token):
            self._deferred_disconnects.pop(guild_id, None)

        rec = self._deferred_disconnect_tasks.get(guild_id)
        if rec is None:
            return
        if rec.state == "PENDING" and (max_dc_token is None or rec.dc_token <= max_dc_token):
            rec.state = "COMPLETED"
            self._deferred_disconnect_tasks.pop(guild_id, None)
            rec.done_event.set()
            if rec.task is not None and not rec.task.done():
                rec.task.cancel()
        elif rec.state == "RUNNING" and (rec.aborted_event.is_set() or rec.done_event.is_set()):
            self._reconcile_aborted_record_locked(guild_id, rec)

    async def cancel_deferred_disconnect(
        self, guild_id: int, *, max_dc_token: int | None = None
    ) -> None:
        self._bind_or_assert_loop_thread()
        guild_id = self._require_guild_id(guild_id)
        async with self._get_lock(guild_id):
            self._cancel_deferred_disconnect_locked(guild_id, max_dc_token=max_dc_token)

    async def request_deferred_disconnect(
        self,
        guild_id: int,
        vc: Any,
        on_disconnected_cb: Callable[[Any], Any] | None = None,
    ) -> None:
        self._bind_or_assert_loop_thread()
        guild_id = self._require_guild_id(guild_id)
        if self._closing:
            return

        async with self._get_lock(guild_id):
            assert self._get_lock(guild_id).locked()
            existing = self._deferred_disconnect_tasks.get(guild_id)
            if (
                existing is not None
                and existing.state == "RUNNING"
                and (existing.aborted_event.is_set() or existing.done_event.is_set())
            ):
                self._reconcile_aborted_record_locked(guild_id, existing)

            vc_state = _probe_vc_connected(vc)
            if (
                self._closing
                or self._voice_transitioning.get(guild_id, False)
                or guild_id in self._unreconciled_voice_clients
                or vc is not self.voice_clients.get(guild_id)
                or vc_state is not True
            ):
                if vc is self.voice_clients.get(guild_id) and vc_state is None:
                    self._register_unreconciled_locked(guild_id, vc, "CANCELLED_OR_INDETERMINATE")
                    self._emit_vc_lifecycle_change(guild_id, vc=vc, physically_disconnected=False)
                return

            dc_token = self._next_token()
            self._deferred_disconnects[guild_id] = (dc_token, vc, on_disconnected_cb)

            existing = self._deferred_disconnect_tasks.get(guild_id)
            if existing is not None and existing.state == "PENDING":
                existing.state = "COMPLETED"
                self._deferred_disconnect_tasks.pop(guild_id, None)
                existing.done_event.set()
                if existing.task is not None and not existing.task.done():
                    existing.task.cancel()
                ready, _, _ = self._disconnect_conditions_clear_locked(guild_id, dc_token, vc)
                if ready:
                    self._schedule_deferred_disconnect_task_locked(
                        guild_id, dc_token, vc, on_disconnected_cb
                    )
            elif existing is not None and existing.state == "RUNNING":
                # No cancelar RUNNING; permanece en _deferred_disconnects hasta Fase 2
                return
            else:
                ready, _, _ = self._disconnect_conditions_clear_locked(guild_id, dc_token, vc)
                if ready:
                    self._schedule_deferred_disconnect_task_locked(
                        guild_id, dc_token, vc, on_disconnected_cb
                    )

    def _spawn_disconnect_finalizer(
        self,
        guild_id: int,
        record: DeferredDisconnectRecord,
        vc: Any,
        *,
        on_disconnected_cb: Callable[[Any], Any] | None,
        phase1_committed: bool,
        disconnect_cancelled_err: BaseException | None,
        disconnect_exc: Exception | None,
        is_force_reconcile: bool,
        prev_unreconciled_reason: str | None = None,
    ) -> asyncio.Task[DisconnectFinalizationResult]:
        loop = asyncio.get_running_loop()
        fin_task = loop.create_task(
            self._finalize_disconnect_under_lock(
                guild_id,
                record,
                vc,
                on_disconnected_cb=on_disconnected_cb,
                phase1_committed=phase1_committed,
                disconnect_cancelled_err=disconnect_cancelled_err,
                disconnect_exc=disconnect_exc,
                is_force_reconcile=is_force_reconcile,
                prev_unreconciled_reason=prev_unreconciled_reason,
            )
        )
        self._finalizer_tasks.add(fin_task)
        fin_task.add_done_callback(self._on_finalizer_done)
        return fin_task

    async def _finalize_disconnect_under_lock(
        self,
        guild_id: int,
        record: DeferredDisconnectRecord,
        vc: Any,
        *,
        on_disconnected_cb: Callable[[Any], Any] | None = None,
        phase1_committed: bool,
        disconnect_cancelled_err: BaseException | None,
        disconnect_exc: Exception | None,
        is_force_reconcile: bool,
        prev_unreconciled_reason: str | None = None,
    ) -> DisconnectFinalizationResult:
        self._bind_or_assert_loop_thread()
        confirmed_clean = False
        still_connected: bool | None = None

        try:
            async with self._get_lock(guild_id):
                assert self._get_lock(guild_id).locked()
                try:
                    if (
                        record.state == "COMPLETED"
                        or self._deferred_disconnect_tasks.get(guild_id) is not record
                    ):
                        record.state = "COMPLETED"
                        record.done_event.set()
                        return DisconnectFinalizationResult(confirmed=False, still_connected=None)

                    if not phase1_committed:
                        pending = self._deferred_disconnects.get(guild_id)
                        if pending is not None and pending[0] == record.dc_token:
                            self._deferred_disconnects.pop(guild_id, None)
                        return DisconnectFinalizationResult(confirmed=False, still_connected=None)

                    still_connected = _probe_vc_connected(vc)
                    physically_disconnected = still_connected is False

                    if is_force_reconcile:
                        self._emit_vc_lifecycle_change(
                            guild_id, vc=vc, physically_disconnected=physically_disconnected
                        )
                        if physically_disconnected:
                            if self.voice_clients.get(guild_id) is vc:
                                self.voice_clients.pop(guild_id, None)
                            self._remove_unreconciled_locked(guild_id, vc)
                            confirmed_clean = (
                                disconnect_cancelled_err is None and disconnect_exc is None
                            )
                        else:
                            outcome: Literal["FAILED", "CANCELLED_OR_INDETERMINATE"] = (
                                "CANCELLED_OR_INDETERMINATE"
                                if (disconnect_cancelled_err is not None or still_connected is None)
                                else (
                                    "CANCELLED_OR_INDETERMINATE"
                                    if prev_unreconciled_reason == "CANCELLED_OR_INDETERMINATE"
                                    else "FAILED"
                                )
                            )
                            self._register_unreconciled_locked(guild_id, vc, outcome)
                    else:
                        if disconnect_cancelled_err is not None or still_connected is None:
                            self._emit_vc_lifecycle_change(
                                guild_id, vc=vc, physically_disconnected=physically_disconnected
                            )
                            if physically_disconnected:
                                if self.voice_clients.get(guild_id) is vc:
                                    self.voice_clients.pop(guild_id, None)
                                self._remove_unreconciled_locked(guild_id, vc)
                                logger.warning(
                                    "[Guild %s] Desconexión cancelada/indeterminada pero física confirmada False.",
                                    guild_id,
                                )
                            else:
                                self._register_unreconciled_locked(
                                    guild_id, vc, "CANCELLED_OR_INDETERMINATE"
                                )
                                logger.error(
                                    "[Guild %s] Desconexión en estado CANCELLED_OR_INDETERMINATE (still_connected=%s).",
                                    guild_id,
                                    still_connected,
                                )
                        elif disconnect_exc is not None or still_connected is True:
                            self._emit_vc_lifecycle_change(
                                guild_id, vc=vc, physically_disconnected=physically_disconnected
                            )
                            if physically_disconnected:
                                if self.voice_clients.get(guild_id) is vc:
                                    self.voice_clients.pop(guild_id, None)
                                self._remove_unreconciled_locked(guild_id, vc)
                                logger.error(
                                    "[Guild %s] vc.disconnect() lanzó excepción aunque físico confirmó False: %s",
                                    guild_id,
                                    disconnect_exc,
                                )
                            else:
                                self._register_unreconciled_locked(guild_id, vc, "FAILED")
                                logger.error(
                                    "[Guild %s] Desconexión FAILED (exc=%s, still_connected=%s).",
                                    guild_id,
                                    disconnect_exc,
                                    still_connected,
                                )
                        else:
                            confirmed_clean = True
                            self._emit_vc_lifecycle_change(
                                guild_id, vc=vc, physically_disconnected=True
                            )
                            if self.voice_clients.get(guild_id) is vc:
                                self.voice_clients.pop(guild_id, None)
                            self._remove_unreconciled_locked(guild_id, vc)

                    record.state = "COMPLETED"
                    if self._deferred_disconnect_tasks.get(guild_id) is record:
                        self._deferred_disconnect_tasks.pop(guild_id, None)

                    next_pending = self._deferred_disconnects.get(guild_id)
                    if next_pending is not None:
                        next_dc_token, next_vc, next_cb = next_pending
                        cur_vc = self.voice_clients.get(guild_id)
                        next_vc_state = _probe_vc_connected(next_vc)
                        next_vc_connected = (
                            cur_vc is not None and next_vc is cur_vc and next_vc_state is True
                        )

                        if (
                            guild_id in self._unreconciled_voice_clients
                            or self._voice_transitioning.get(guild_id, False)
                            or disconnect_cancelled_err is not None
                            or self._closing
                        ):
                            self._deferred_disconnects.pop(guild_id, None)
                            logger.warning(
                                "[Guild %s] Descartando next_pending tras cierre/reconciliación.",
                                guild_id,
                            )
                        elif cur_vc is not None and next_vc is cur_vc and next_vc_state is None:
                            self._register_unreconciled_locked(
                                guild_id, next_vc, "CANCELLED_OR_INDETERMINATE"
                            )
                            self._emit_vc_lifecycle_change(
                                guild_id, vc=next_vc, physically_disconnected=False
                            )
                            self._deferred_disconnects.pop(guild_id, None)
                            logger.error(
                                "[Guild %s] next_vc quedó en estado indeterminado; registrado para reconciliación.",
                                guild_id,
                            )
                        elif not next_vc_connected:
                            self._deferred_disconnects.pop(guild_id, None)
                        elif (
                            not self._active_owners.get(guild_id)
                            and self._playback_owners.get(guild_id) is None
                            and self._music_waiting_preempt.get(guild_id, 0) == 0
                            and not self._is_music_active(guild_id)
                        ):
                            self._schedule_deferred_disconnect_task_locked(
                                guild_id, next_dc_token, next_vc, next_cb
                            )
                finally:
                    record.state = "COMPLETED"
                    if self._deferred_disconnect_tasks.get(guild_id) is record:
                        self._deferred_disconnect_tasks.pop(guild_id, None)
                    record.done_event.set()
        except asyncio.CancelledError:
            record.aborted_event.set()
            record.done_event.set()
            if self._closing:
                logger.warning(
                    "[Guild %s] Finalizador cancelado durante cierre controlado; prohibido lanzar recuperación.",
                    guild_id,
                )
            else:
                logger.error(
                    "[Guild %s] Cancelación anómala de finalizador fuera de cierre; programando recuperación.",
                    guild_id,
                )
                self._schedule_aborted_finalizer_recovery(guild_id, record)
            raise

        if confirmed_clean and on_disconnected_cb is not None:
            try:
                cb_res = on_disconnected_cb(vc)
                if asyncio.iscoroutine(cb_res):
                    await cb_res
            except Exception as cb_exc:
                logger.warning("[Guild %s] Error en on_disconnected_cb: %s", guild_id, cb_exc)

        return DisconnectFinalizationResult(
            confirmed=confirmed_clean, still_connected=still_connected
        )

    async def _run_deferred_disconnect(
        self,
        guild_id: int,
        record: DeferredDisconnectRecord,
        dc_token: int,
        vc: Any,
        on_disconnected_cb: Callable[[Any], Any] | None,
    ) -> None:
        disconnect_cancelled_err: BaseException | None = None
        disconnect_exc: Exception | None = None
        phase1_committed = False
        phase1_aborted_cleanly = False
        fin_task: asyncio.Task[DisconnectFinalizationResult] | None = None

        try:
            async with self._get_lock(guild_id):
                assert self._get_lock(guild_id).locked()
                pending = self._deferred_disconnects.get(guild_id)
                pending_vc = pending[1] if pending is not None else None
                pending_state = _probe_vc_connected(pending_vc) if pending_vc is not None else False
                if (
                    self._closing
                    or self._voice_transitioning.get(guild_id, False)
                    or guild_id in self._unreconciled_voice_clients
                    or record.state != "PENDING"
                    or self._deferred_disconnect_tasks.get(guild_id) is not record
                    or pending is None
                    or pending[0] != dc_token
                    or pending_vc is not self.voice_clients.get(guild_id)
                    or pending_state is not True
                    or self._active_owners.get(guild_id)
                    or self._playback_owners.get(guild_id) is not None
                    or self._music_waiting_preempt.get(guild_id, 0) > 0
                    or self._is_music_active(guild_id)
                ):
                    if (
                        pending is not None
                        and pending[0] == dc_token
                        and (
                            self._closing
                            or self._voice_transitioning.get(guild_id, False)
                            or guild_id in self._unreconciled_voice_clients
                            or pending_vc is not self.voice_clients.get(guild_id)
                            or pending_state is not True
                        )
                    ):
                        if pending_vc is self.voice_clients.get(guild_id) and pending_state is None:
                            self._register_unreconciled_locked(
                                guild_id, pending_vc, "CANCELLED_OR_INDETERMINATE"
                            )
                            self._emit_vc_lifecycle_change(
                                guild_id, vc=pending_vc, physically_disconnected=False
                            )
                        self._deferred_disconnects.pop(guild_id, None)
                    record.state = "COMPLETED"
                    if self._deferred_disconnect_tasks.get(guild_id) is record:
                        self._deferred_disconnect_tasks.pop(guild_id, None)
                    record.done_event.set()
                    phase1_aborted_cleanly = True
                    return

                self._deferred_disconnects.pop(guild_id, None)
                record.state = "RUNNING"
                phase1_committed = True

            # Ejecución de red FUERA de _get_lock(guild_id)
            try:
                await vc.disconnect()
            except Exception as exc:
                disconnect_exc = exc
        except asyncio.CancelledError as exc:
            disconnect_cancelled_err = exc
            raise
        finally:
            if not phase1_aborted_cleanly:
                if fin_task is None:
                    fin_task = self._spawn_disconnect_finalizer(
                        guild_id,
                        record,
                        vc,
                        on_disconnected_cb=on_disconnected_cb,
                        phase1_committed=phase1_committed,
                        disconnect_cancelled_err=disconnect_cancelled_err,
                        disconnect_exc=disconnect_exc,
                        is_force_reconcile=False,
                    )
                await asyncio.shield(fin_task)

        if disconnect_exc is not None:
            raise disconnect_exc

    # ── Conexión, upgrade y reconciliación de VoiceClient ────────

    @staticmethod
    async def _invoke_connect(voice_channel: Any) -> Any:
        connect_kwargs: dict[str, Any] = {
            "timeout": CONNECT_INTERNAL_TIMEOUT_S,
            "reconnect": True,
        }
        if HAS_VOICE_RECV and VoiceRecvClientCls is not None:
            connect_kwargs["cls"] = VoiceRecvClientCls

        try:
            return await voice_channel.connect(**connect_kwargs)
        except TypeError:
            if "cls" in connect_kwargs:
                return await voice_channel.connect(cls=connect_kwargs["cls"])
            return await voice_channel.connect()

    async def ensure_voice_client(
        self,
        voice_channel: Any,
        *,
        require_receive: bool = False,
        is_music_active_cb: Callable[[int], bool] | None = None,
    ) -> Any:
        self._bind_or_assert_loop_thread()
        if require_receive and not HAS_VOICE_RECV:
            raise VoiceReceiveUnavailableError(
                "La extensión de recepción de audio (`discord-ext-voice-recv`) no está "
                "disponible en este entorno. La música funciona, pero `/listen` y `/translate` "
                "están deshabilitados."
            )

        guild = voice_channel.guild
        guild_id = self._require_guild_id(guild.id)

        while True:
            wait_ev: asyncio.Event | None = None
            disconnect_vc: Any = None
            disconnect_force_flag = False
            force_record: DeferredDisconnectRecord | None = None
            disconnect_outcome: str | None = None

            async with self._get_lock(guild_id):
                assert self._get_lock(guild_id).locked()
                if self._closing:
                    raise VoiceClientUnavailableError(
                        f"[Guild {guild_id}] VoiceSessionManager en cierre."
                    )

                self._cancel_deferred_disconnect_locked(guild_id)

                running_rec = self._deferred_disconnect_tasks.get(guild_id)
                if running_rec is not None and running_rec.state == "RUNNING":
                    if running_rec.aborted_event.is_set() or running_rec.done_event.is_set():
                        self._reconcile_aborted_record_locked(guild_id, running_rec)
                        continue
                    wait_ev = running_rec.done_event
                elif (unrec_list := self._unreconciled_voice_clients.get(guild_id)):
                    old_vc, disconnect_outcome = unrec_list[0]
                    old_connected = _probe_vc_connected(old_vc)
                    if old_connected is False:
                        self._emit_vc_lifecycle_change(
                            guild_id, vc=old_vc, physically_disconnected=True
                        )
                        if self.voice_clients.get(guild_id) is old_vc:
                            self.voice_clients.pop(guild_id, None)
                        self._remove_unreconciled_locked(guild_id, old_vc)
                        continue
                    else:
                        if old_connected is None:
                            disconnect_outcome = "CANCELLED_OR_INDETERMINATE"
                            self._register_unreconciled_locked(
                                guild_id, old_vc, "CANCELLED_OR_INDETERMINATE"
                            )
                        disconnect_vc = old_vc
                        disconnect_force_flag = True
                        force_record = DeferredDisconnectRecord(
                            task=asyncio.current_task(),
                            dc_token=self._next_token(),
                            vc=old_vc,
                            state="RUNNING",
                            done_event=asyncio.Event(),
                            aborted_event=asyncio.Event(),
                        )
                        self._deferred_disconnect_tasks[guild_id] = force_record
                else:
                    existing = self.voice_clients.get(guild_id) or getattr(
                        guild, "voice_client", None
                    )
                    existing_state = _probe_vc_connected(existing)
                    if existing is not None and existing_state is None:
                        self._register_unreconciled_locked(
                            guild_id, existing, "CANCELLED_OR_INDETERMINATE"
                        )
                        self._emit_vc_lifecycle_change(
                            guild_id, vc=existing, physically_disconnected=False
                        )
                        continue

                    if existing is not None and existing_state is True:
                        if require_receive and not self.supports_voice_receive(existing):
                            active_cb = is_music_active_cb or self._is_music_active_cb
                            music_busy = bool(active_cb and active_cb(guild_id))
                            if music_busy:
                                raise VoiceClientUpgradeBlockedError(
                                    "El canal ya está reproduciendo música sobre un VoiceClient estándar "
                                    "sin recepción de audio. Detén la música antes de activar `/listen` o `/translate`."
                                )
                            logger.info(
                                "[Guild %s] Reconectando VoiceClient estándar -> VoiceRecvClient...",
                                guild_id,
                            )
                            disconnect_vc = existing
                            disconnect_force_flag = False
                            disconnect_outcome = "UPGRADE"
                            force_record = DeferredDisconnectRecord(
                                task=asyncio.current_task(),
                                dc_token=self._next_token(),
                                vc=existing,
                                state="RUNNING",
                                done_event=asyncio.Event(),
                                aborted_event=asyncio.Event(),
                            )
                            self._deferred_disconnect_tasks[guild_id] = force_record
                        else:
                            current_ch = getattr(existing, "channel", None)
                            moved = False
                            try:
                                if current_ch is None or current_ch.id != voice_channel.id:
                                    self._voice_transitioning[guild_id] = True
                                    moved = True
                                    try:
                                        await asyncio.wait_for(
                                            existing.move_to(voice_channel), timeout=MOVE_TIMEOUT_S
                                        )
                                    except (Exception, asyncio.CancelledError) as exc:
                                        guild_vc = getattr(guild, "voice_client", None)
                                        candidates = [existing]
                                        if guild_vc is not None and guild_vc is not existing:
                                            candidates.append(guild_vc)
                                        for cand in candidates:
                                            cand_connected = _probe_vc_connected(cand)
                                            if cand_connected is False:
                                                self._emit_vc_lifecycle_change(
                                                    guild_id, vc=cand, physically_disconnected=True
                                                )
                                                if self.voice_clients.get(guild_id) is cand:
                                                    self.voice_clients.pop(guild_id, None)
                                                self._remove_unreconciled_locked(guild_id, cand)
                                            else:
                                                reason: Literal[
                                                    "FAILED", "CANCELLED_OR_INDETERMINATE"
                                                ] = (
                                                    "CANCELLED_OR_INDETERMINATE"
                                                    if (
                                                        isinstance(
                                                            exc,
                                                            (asyncio.CancelledError, asyncio.TimeoutError),
                                                        )
                                                        or cand_connected is None
                                                    )
                                                    else "FAILED"
                                                )
                                                self._register_unreconciled_locked(
                                                    guild_id, cand, reason
                                                )
                                                self._emit_vc_lifecycle_change(
                                                    guild_id, vc=cand, physically_disconnected=False
                                                )
                                        raise

                                guild_vc = getattr(guild, "voice_client", None)
                                if guild_vc is not existing:
                                    candidates = [existing]
                                    if guild_vc is not None:
                                        candidates.append(guild_vc)
                                    any_non_false = False
                                    for cand in candidates:
                                        cand_connected = _probe_vc_connected(cand)
                                        if cand_connected is False:
                                            if self.voice_clients.get(guild_id) is cand:
                                                self.voice_clients.pop(guild_id, None)
                                            self._remove_unreconciled_locked(guild_id, cand)
                                        else:
                                            any_non_false = True
                                            self._register_unreconciled_locked(
                                                guild_id, cand, "CANCELLED_OR_INDETERMINATE"
                                            )
                                    lifecycle_vc = guild_vc if guild_vc is not None else existing
                                    self._emit_vc_lifecycle_change(
                                        guild_id,
                                        vc=lifecycle_vc,
                                        physically_disconnected=not any_non_false,
                                    )
                                    raise VoiceClientRecoveryError(
                                        f"[Guild {guild_id}] Divergencia guild.voice_client tras move_to()."
                                    )

                                effective_vc = existing
                                eff_connected = _probe_vc_connected(effective_vc)
                                eff_channel = getattr(effective_vc, "channel", None)
                                if (
                                    eff_connected is not True
                                    or eff_channel is None
                                    or eff_channel.id != voice_channel.id
                                ):
                                    if eff_connected is False:
                                        self._emit_vc_lifecycle_change(
                                            guild_id, vc=effective_vc, physically_disconnected=True
                                        )
                                        if self.voice_clients.get(guild_id) is effective_vc:
                                            self.voice_clients.pop(guild_id, None)
                                        self._remove_unreconciled_locked(guild_id, effective_vc)
                                    else:
                                        eff_reason: Literal[
                                            "FAILED", "CANCELLED_OR_INDETERMINATE"
                                        ] = (
                                            "CANCELLED_OR_INDETERMINATE"
                                            if eff_connected is None
                                            else "FAILED"
                                        )
                                        self._register_unreconciled_locked(
                                            guild_id, effective_vc, eff_reason
                                        )
                                        self._emit_vc_lifecycle_change(
                                            guild_id, vc=effective_vc, physically_disconnected=False
                                        )
                                    raise VoiceClientRecoveryError(
                                        f"[Guild {guild_id}] Postcondición de move_to() no satisfecha."
                                    )

                                prev_vc = self.voice_clients.get(guild_id)
                                if effective_vc is not prev_vc:
                                    self._emit_vc_lifecycle_change(
                                        guild_id, vc=effective_vc, physically_disconnected=False
                                    )
                                self.voice_clients[guild_id] = effective_vc
                                return effective_vc
                            finally:
                                if moved:
                                    assert self._get_lock(guild_id).locked()
                                    self._voice_transitioning.pop(guild_id, None)
                    else:
                        # existing is None or existing_state is False
                        if existing is not None:
                            if self.voice_clients.get(guild_id) is existing:
                                self._emit_vc_lifecycle_change(
                                    guild_id, vc=existing, physically_disconnected=True
                                )
                                self.voice_clients.pop(guild_id, None)
                            self._remove_unreconciled_locked(guild_id, existing)

                        # Revisar instancia residual en guild.voice_client antes de connect()
                        guild_vc = getattr(guild, "voice_client", None)
                        if guild_vc is not None and guild_vc is not existing:
                            guild_vc_state = _probe_vc_connected(guild_vc)
                            if guild_vc_state is False:
                                if self.voice_clients.get(guild_id) is guild_vc:
                                    self.voice_clients.pop(guild_id, None)
                                self._remove_unreconciled_locked(guild_id, guild_vc)
                                self._emit_vc_lifecycle_change(
                                    guild_id, vc=guild_vc, physically_disconnected=True
                                )
                            else:
                                res_reason: Literal["FAILED", "CANCELLED_OR_INDETERMINATE"] = (
                                    "CANCELLED_OR_INDETERMINATE"
                                    if guild_vc_state is None
                                    else "FAILED"
                                )
                                self._register_unreconciled_locked(guild_id, guild_vc, res_reason)
                                self._emit_vc_lifecycle_change(
                                    guild_id, vc=guild_vc, physically_disconnected=False
                                )
                                continue

                        self._voice_transitioning[guild_id] = True
                        try:
                            conn_task = asyncio.create_task(self._invoke_connect(voice_channel))
                            try:
                                new_vc = await asyncio.wait_for(
                                    conn_task, timeout=CONNECT_OUTER_TIMEOUT_S
                                )
                            except asyncio.TimeoutError:
                                outer_expired = conn_task.cancelled() or not conn_task.done()
                                if outer_expired:
                                    logger.error(
                                        "[Guild %s] Deadline externo (%ss) excedido para cancelar connect(); limpieza interna de discord.py no garantizada.",
                                        guild_id,
                                        CONNECT_OUTER_TIMEOUT_S,
                                    )
                                else:
                                    logger.warning(
                                        "[Guild %s] Timeout interno de discord.py (%ss) en connect().",
                                        guild_id,
                                        CONNECT_INTERNAL_TIMEOUT_S,
                                    )
                                orphan_vc = getattr(guild, "voice_client", None)
                                if orphan_vc is not None:
                                    orphan_conn = _probe_vc_connected(orphan_vc)
                                    if orphan_conn is False:
                                        self._emit_vc_lifecycle_change(
                                            guild_id, vc=orphan_vc, physically_disconnected=True
                                        )
                                        if self.voice_clients.get(guild_id) is orphan_vc:
                                            self.voice_clients.pop(guild_id, None)
                                        self._remove_unreconciled_locked(guild_id, orphan_vc)
                                    else:
                                        orp_reason: Literal[
                                            "FAILED", "CANCELLED_OR_INDETERMINATE"
                                        ] = (
                                            "CANCELLED_OR_INDETERMINATE"
                                            if (outer_expired or orphan_conn is None)
                                            else "FAILED"
                                        )
                                        self._register_unreconciled_locked(
                                            guild_id, orphan_vc, orp_reason
                                        )
                                        self._emit_vc_lifecycle_change(
                                            guild_id, vc=orphan_vc, physically_disconnected=False
                                        )
                                raise
                            except (Exception, asyncio.CancelledError) as exc:
                                if not conn_task.done():
                                    conn_task.cancel()
                                    with contextlib.suppress(Exception, asyncio.CancelledError):
                                        await conn_task
                                orphan_vc = getattr(guild, "voice_client", None)
                                if orphan_vc is not None:
                                    orphan_conn = _probe_vc_connected(orphan_vc)
                                    if orphan_conn is False:
                                        self._emit_vc_lifecycle_change(
                                            guild_id, vc=orphan_vc, physically_disconnected=True
                                        )
                                        if self.voice_clients.get(guild_id) is orphan_vc:
                                            self.voice_clients.pop(guild_id, None)
                                        self._remove_unreconciled_locked(guild_id, orphan_vc)
                                    else:
                                        orp_reason2: Literal[
                                            "FAILED", "CANCELLED_OR_INDETERMINATE"
                                        ] = (
                                            "CANCELLED_OR_INDETERMINATE"
                                            if (
                                                isinstance(exc, asyncio.CancelledError)
                                                or orphan_conn is None
                                            )
                                            else "FAILED"
                                        )
                                        self._register_unreconciled_locked(
                                            guild_id, orphan_vc, orp_reason2
                                        )
                                        self._emit_vc_lifecycle_change(
                                            guild_id, vc=orphan_vc, physically_disconnected=False
                                        )
                                raise

                            # Postcondición simétrica obligatoria de connect() + condición Opción A
                            guild_vc = getattr(guild, "voice_client", None)
                            new_connected = _probe_vc_connected(new_vc)
                            new_channel = (
                                getattr(new_vc, "channel", None) if new_vc is not None else None
                            )
                            if (
                                new_vc is None
                                or new_connected is not True
                                or guild_vc is not new_vc
                                or new_channel is None
                                or new_channel.id != voice_channel.id
                            ):
                                candidates = []
                                if new_vc is not None:
                                    candidates.append(new_vc)
                                if guild_vc is not None and guild_vc is not new_vc:
                                    candidates.append(guild_vc)
                                any_non_false = False
                                for cand in candidates:
                                    cand_connected = _probe_vc_connected(cand)
                                    if cand_connected is False:
                                        if self.voice_clients.get(guild_id) is cand:
                                            self.voice_clients.pop(guild_id, None)
                                        self._remove_unreconciled_locked(guild_id, cand)
                                    else:
                                        any_non_false = True
                                        post_reason: Literal[
                                            "FAILED", "CANCELLED_OR_INDETERMINATE"
                                        ] = (
                                            "CANCELLED_OR_INDETERMINATE"
                                            if (cand_connected is None or guild_vc is not new_vc)
                                            else "FAILED"
                                        )
                                        self._register_unreconciled_locked(
                                            guild_id, cand, post_reason
                                        )
                                lifecycle_vc = new_vc if new_vc is not None else guild_vc
                                # Condición obligatoria Opción A: jamás reportar physically_disconnected=True con lista vacía
                                physically_disconnected = bool(candidates) and not any_non_false
                                self._emit_vc_lifecycle_change(
                                    guild_id,
                                    vc=lifecycle_vc,
                                    physically_disconnected=physically_disconnected,
                                )
                                raise VoiceClientRecoveryError(
                                    f"[Guild {guild_id}] Postcondición de connect() no satisfecha."
                                )

                            prev_vc = self.voice_clients.get(guild_id)
                            if prev_vc is not new_vc:
                                self._emit_vc_lifecycle_change(
                                    guild_id, vc=new_vc, physically_disconnected=False
                                )
                            self.voice_clients[guild_id] = new_vc
                            return new_vc
                        finally:
                            assert self._get_lock(guild_id).locked()
                            self._voice_transitioning.pop(guild_id, None)

            if wait_ev is not None:
                await wait_ev.wait()
                continue

            if disconnect_vc is not None:
                assert force_record is not None
                rec_exc: Exception | None = None
                rec_cancelled: BaseException | None = None
                fin_task: asyncio.Task[DisconnectFinalizationResult] | None = None
                fin_res = DisconnectFinalizationResult(confirmed=False, still_connected=None)
                try:
                    try:
                        if disconnect_force_flag:
                            await disconnect_vc.disconnect(force=True)
                        else:
                            await disconnect_vc.disconnect()
                    except Exception as exc:
                        rec_exc = exc
                except asyncio.CancelledError as exc:
                    rec_cancelled = exc
                    raise
                finally:
                    if fin_task is None:
                        fin_task = self._spawn_disconnect_finalizer(
                            guild_id,
                            force_record,
                            disconnect_vc,
                            on_disconnected_cb=None,
                            phase1_committed=True,
                            disconnect_cancelled_err=rec_cancelled,
                            disconnect_exc=rec_exc,
                            is_force_reconcile=True,
                            prev_unreconciled_reason=disconnect_outcome,
                        )
                    fin_res = await asyncio.shield(fin_task)

                if rec_exc is not None or fin_res.still_connected is not False:
                    raise VoiceClientRecoveryError(
                        f"[Guild {guild_id}] Falló reconciliación de VoiceClient saliente."
                    )
                continue

    # ── Apagado y limpieza ───────────────────────────────────────

    def begin_closing(self) -> None:
        self._closing = True

    async def aclose(self, *, timeout: float = 5.0) -> None:
        self._closing = True
        pending = list(self._lifecycle_tasks | self._finalizer_tasks)
        if not pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), timeout=timeout
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning(
                "Timeout o cancelación drenando tareas en aclose(); cancelando %d pendientes.",
                len(pending),
            )
            for t in pending:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def can_disconnect(
        self,
        guild_id: int,
        *,
        requester_mode: str,
        is_music_active_cb: Callable[[int], bool] | None = None,
    ) -> bool:
        if requester_mode != "music" and is_music_active_cb is not None and is_music_active_cb(guild_id):
            return False
        if self._playback_owners.get(guild_id) == "tts" or self._music_waiting_preempt.get(guild_id, 0) > 0:
            return False
        active_owner = self._active_owners.get(guild_id)
        if active_owner is not None and active_owner != requester_mode:
            return False
        return True

    def reset_guild(self, guild_id: int) -> None:
        """Limpia estado de seguimiento para un guild en pruebas aisladas."""
        self.voice_clients.pop(guild_id, None)
        self._active_tokens.pop(guild_id, None)
        self._active_owners.pop(guild_id, None)
        self._active_sinks.pop(guild_id, None)
        self._revoke_callbacks.pop(guild_id, None)
        self._playback_owners.pop(guild_id, None)
        old_pb_tok = self._playback_tokens.pop(guild_id, None)
        if old_pb_tok is not None:
            ev = self._playback_done_events.pop(old_pb_tok, None)
            if ev is not None and not ev.is_set():
                ev.set()
        self._music_waiting_preempt.pop(guild_id, None)
        self._voice_transitioning.pop(guild_id, None)
        self._unreconciled_voice_clients.pop(guild_id, None)
        self._deferred_disconnects.pop(guild_id, None)
        rec = self._deferred_disconnect_tasks.pop(guild_id, None)
        if rec is not None:
            rec.state = "COMPLETED"
            rec.done_event.set()
            if rec.task is not None and not rec.task.done():
                rec.task.cancel()


def _vc_is_physically_busy(vc: Any) -> bool:
    is_playing_fn = getattr(vc, "is_playing", None)
    is_paused_fn = getattr(vc, "is_paused", None)
    playing = bool(is_playing_fn() if callable(is_playing_fn) else is_playing_fn)
    paused = bool(is_paused_fn() if callable(is_paused_fn) else is_paused_fn)
    return playing or paused


voice_session_manager = VoiceSessionManager()


def get_voice_session_manager() -> VoiceSessionManager:
    return voice_session_manager
