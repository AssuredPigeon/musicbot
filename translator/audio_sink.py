# translator/audio_sink.py
#
# AudioSink de Discord para el modulo de traduccion.
# Captura el audio de usuarios especificos en el canal de voz.
# Separado del AudioSink del music bot.

import asyncio
import logging
import numpy as np
import discord

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


class TranslatorAudioSink(discord.AudioSink):
    """
    Captura audio de multiples usuarios en el canal de voz.
    Cuando detecta el fin de una frase (silencio), llama al callback.

    Args:
        on_speech: callback async(user_id, pcm_bytes) -> None
        active_users: conjunto de user_ids a escuchar (None = todos)
    """

    def __init__(self, on_speech, active_users: set[int] | None = None):
        self.on_speech = on_speech
        self.active_users = active_users  # None = escuchar a todos

        # Buffer por usuario: {user_id: [bytes, ...]}
        self._buffers: dict[int, list[bytes]] = {}
        # Contador de frames de silencio por usuario
        self._silence_frames: dict[int, int] = {}
        # Frames por segundo (Discord envia 50 paquetes/s de 20ms cada uno)
        self._frames_per_second = 50
        self._silence_frame_threshold = int(SILENCE_SECONDS * self._frames_per_second)
        self._max_frames = int(MAX_SPEECH_SECONDS * self._frames_per_second)

    # ── discord.AudioSink interface ──────────────────────────

    def wants_opus(self) -> bool:
        return False  # Queremos PCM decodificado

    def write(self, user: discord.User, data: discord.VoiceData):
        if user is None:
            return
        uid = user.id

        # Filtrar por usuarios activos
        if self.active_users is not None and uid not in self.active_users:
            return

        pcm = data.pcm
        if not pcm:
            return

        # Calcular energia del frame para VAD
        frame = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        energy = float(np.mean(frame ** 2))

        if energy < SILENCE_THRESHOLD:
            # Frame de silencio
            self._silence_frames[uid] = self._silence_frames.get(uid, 0) + 1

            # Si tenemos suficiente habla acumulada y ahora hay silencio -> emitir
            buf = self._buffers.get(uid, [])
            if (
                buf
                and self._silence_frames[uid] >= self._silence_frame_threshold
            ):
                self._emit(uid)
        else:
            # Frame con voz
            self._silence_frames[uid] = 0
            if uid not in self._buffers:
                self._buffers[uid] = []
            self._buffers[uid].append(pcm)

            # Cortar si supera el maximo
            if len(self._buffers[uid]) >= self._max_frames:
                self._emit(uid)

    def cleanup(self):
        self._buffers.clear()
        self._silence_frames.clear()

    # ── Privado ──────────────────────────────────────────────

    def _emit(self, user_id: int):
        """Llama al callback con el audio acumulado y limpia el buffer."""
        buf = self._buffers.pop(user_id, [])
        self._silence_frames[user_id] = 0

        if not buf:
            return

        # Validar duracion minima
        frames = len(buf)
        duration = frames / self._frames_per_second
        if duration < MIN_SPEECH_SECONDS:
            return

        pcm_bytes = b"".join(buf)

        # Programar el callback en el event loop
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(self.on_speech(user_id, pcm_bytes))
        except RuntimeError:
            pass
