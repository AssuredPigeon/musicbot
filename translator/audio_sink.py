# translator/audio_sink.py
#
# AudioSink de Discord para el modulo de traduccion.
# Captura el audio de usuarios especificos en el canal de voz.
# Separado del AudioSink del music bot.

import asyncio
import logging
import threading
import numpy as np
import discord
from utils.voice_session_manager import AudioSinkBase, VoiceDataType, voice_session_manager

logger = logging.getLogger("musicbot.translator.sink")

# Parametros de audio de Discord
DISCORD_SAMPLE_RATE = 48000   # Hz
DISCORD_CHANNELS = 2          # Stereo
BYTES_PER_SAMPLE = 2          # int16

# Parametros de captura
SILENCE_THRESHOLD = 0.005     # Energia minima para considerar voz
MIN_SPEECH_SECONDS = 0.5      # Duracion minima de habla valida
MAX_SPEECH_SECONDS = 8.0      # Cortar si el usuario habla mas de esto
SILENCE_SECONDS = 0.8         # Segundos de silencio para considerar fin de frase


class TranslatorAudioSink(AudioSinkBase):
    """
    Captura audio de multiples usuarios en el canal de voz.
    Cuando detecta el fin de una frase (silencio), llama al callback.

    Args:
        on_speech: callback async(user_id, pcm_bytes) -> None
        active_users: conjunto de user_ids a escuchar (None = todos)
    """

    def __init__(
        self,
        on_speech,
        active_users: set[int] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ):
        super().__init__()
        self.on_speech = on_speech
        self.active_users = active_users  # None = escuchar a todos
        if loop is not None:
            self._loop: asyncio.AbstractEventLoop | None = loop
        else:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None

        # Buffer por usuario: {user_id: [bytes, ...]} protegido entre hilos
        self._buffers: dict[int, list[bytes]] = {}
        # Contador de frames de silencio por usuario
        self._silence_frames: dict[int, int] = {}
        self._lock = threading.Lock()
        self._cleaned_up = False
        # Frames por segundo (Discord envia 50 paquetes/s de 20ms cada uno)
        self._frames_per_second = 50
        self._silence_frame_threshold = int(SILENCE_SECONDS * self._frames_per_second)
        self._max_frames = int(MAX_SPEECH_SECONDS * self._frames_per_second)

    # ── voice_recv.AudioSink interface ───────────────────────

    def wants_opus(self) -> bool:
        return False  # Queremos PCM decodificado

    def write(self, user: discord.User | None, data: VoiceDataType):
        if user is None or self._cleaned_up:
            return
        uid = user.id

        # Filtrar por usuarios activos
        if self.active_users is not None and uid not in self.active_users:
            return

        pcm = getattr(data, "pcm", None)
        if pcm is None:
            pcm = getattr(data, "data", None)
        if not pcm:
            return

        # Calcular energia del frame para VAD
        frame = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        energy = float(np.mean(frame ** 2))

        frames_to_emit: list[bytes] | None = None
        with self._lock:
            if self._cleaned_up:
                return
            if energy < SILENCE_THRESHOLD:
                # Frame de silencio
                self._silence_frames[uid] = self._silence_frames.get(uid, 0) + 1

                # Si tenemos suficiente habla acumulada y ahora hay silencio -> emitir
                buf = self._buffers.get(uid, [])
                if buf and self._silence_frames[uid] >= self._silence_frame_threshold:
                    frames_to_emit = self._buffers.pop(uid, [])
                    self._silence_frames[uid] = 0
            else:
                # Frame con voz
                self._silence_frames[uid] = 0
                if uid not in self._buffers:
                    self._buffers[uid] = []
                self._buffers[uid].append(pcm)

                # Cortar si supera el maximo
                if len(self._buffers[uid]) >= self._max_frames:
                    frames_to_emit = self._buffers.pop(uid, [])
                    self._silence_frames[uid] = 0

        if frames_to_emit:
            self._emit_frames(uid, frames_to_emit)

    def cleanup(self):
        with self._lock:
            if self._cleaned_up:
                return
            self._cleaned_up = True
            self._buffers.clear()
            self._silence_frames.clear()

    # ── Privado ──────────────────────────────────────────────

    def _emit(self, user_id: int):
        """Llama al callback con el audio acumulado y limpia el buffer."""
        with self._lock:
            buf = self._buffers.pop(user_id, [])
            self._silence_frames[user_id] = 0
        if buf:
            self._emit_frames(user_id, buf)

    def _emit_frames(self, user_id: int, buf: list[bytes]):
        """Valida duracion minima y programa el callback de forma thread-safe en el loop."""
        if not buf or self._cleaned_up:
            return

        frames = len(buf)
        duration = frames / self._frames_per_second
        if duration < MIN_SPEECH_SECONDS:
            return

        pcm_bytes = b"".join(buf)

        loop = self._loop
        if loop is None:
            logger.error(
                "TranslatorAudioSink: no hay event loop asociado para despachar audio del usuario %s.",
                user_id,
            )
            return

        if loop.is_closed():
            voice_session_manager.record_dropped_after_on_closed_loop(
                0, user_id, None, reason="sink_loop_is_closed"
            )
            logger.error(
                "TranslatorAudioSink: event loop cerrado; no se puede despachar audio del usuario %s.",
                user_id,
            )
            return

        def _schedule_task():
            if self._cleaned_up:
                return
            try:
                loop.create_task(self.on_speech(user_id, pcm_bytes))
            except Exception as exc:
                logger.error(
                    "TranslatorAudioSink: error creando tarea on_speech para %s: %s",
                    user_id,
                    exc,
                )

        try:
            loop.call_soon_threadsafe(_schedule_task)
        except RuntimeError as exc:
            voice_session_manager.record_dropped_after_on_closed_loop(
                0, user_id, None, reason=f"sink_runtime_error:{exc}"
            )
            logger.error(
                "TranslatorAudioSink: fallo en call_soon_threadsafe para %s: %s",
                user_id,
                exc,
            )
