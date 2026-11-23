# modules/voice_receiver.py
#
# Captura de audio real desde Discord usando AudioSink nativo de discord.py.
# Reemplaza la simulación anterior (numpy random) con paquetes Opus reales.
#
# Flujo:
#   1. Bot se une al canal de voz
#   2. Se instala un DiscordAudioSink en el VoiceClient
#   3. Discord.py entrega paquetes de audio por usuario (PCM 48kHz stereo)
#   4. smart_record_audio acumula y detecta actividad de voz con VAD simple
#   5. El audio final se guarda como WAV mono 16kHz listo para Whisper

import os
import wave
import struct
import asyncio
import tempfile
import threading
import logging
import numpy as np
import discord

from utils.audio_utils import detect_voice_activity, save_audio_to_file
from utils.voice_session_manager import (
    AudioSinkBase,
    HAS_VOICE_RECV,
    VoiceDataType,
    VoiceReceiveUnavailableError,
    voice_session_manager,
)

logger = logging.getLogger('musicbot.voice_receiver')

# ──────────────────────────────────────────────────────────────
# AudioSink: recibe los paquetes PCM de Discord por usuario
# ──────────────────────────────────────────────────────────────

class DiscordAudioSink(AudioSinkBase):
    """
    Sink que acumula paquetes PCM 48 kHz / stereo / int16 por user_id.
    discord-ext-voice-recv llama a write() cada vez que llega un paquete de voz.
    """

    def __init__(self):
        super().__init__()
        self._buffers: dict[int, bytearray] = {}  # {user_id: raw_pcm_bytes}
        self._lock = threading.Lock()
        self._cleaned_up = False

    def write(self, user: discord.User | None, data: VoiceDataType) -> None:
        if user is None or self._cleaned_up:
            return
        pcm = getattr(data, "pcm", None)
        if pcm is None:
            pcm = getattr(data, "data", None)
        if not pcm:
            return
        uid = user.id
        with self._lock:
            if self._cleaned_up:
                return
            if uid not in self._buffers:
                self._buffers[uid] = bytearray()
            self._buffers[uid].extend(pcm)

    def wants_opus(self) -> bool:
        # Queremos PCM decodificado, no Opus crudo
        return False

    def cleanup(self) -> None:
        with self._lock:
            if self._cleaned_up:
                return
            self._cleaned_up = True
            self._buffers.clear()

    async def get_audio(self, user_id: int) -> bytes | None:
        """Retorna y limpia el buffer acumulado de un usuario."""
        with self._lock:
            data = self._buffers.pop(user_id, None)
            return bytes(data) if data else None


# ──────────────────────────────────────────────────────────────
# VoiceReceiver: interfaz principal para el MusicPlayer
# ──────────────────────────────────────────────────────────────

class VoiceReceiver:
    """
    Gestiona la captura de voz de Discord para uno o varios usuarios.

    Uso típico:
        receiver = VoiceReceiver(voice_client, sample_rate=48000)
        await receiver.setup_receiver()          # instala el sink
        path = await receiver.smart_record_audio(user_id)
        await receiver.cleanup()
    """

    # Discord entrega PCM 48 kHz, stereo, int16
    DISCORD_SAMPLE_RATE = 48_000
    DISCORD_CHANNELS = 2
    BYTES_PER_SAMPLE = 2  # int16

    def __init__(
        self,
        voice_client: discord.VoiceClient | None = None,
        sample_rate: int = 48_000,
        guild_id: int | None = None,
        on_revoked=None,
    ):
        self.voice_client = voice_client
        self.guild_id = guild_id or getattr(getattr(voice_client, "guild", None), "id", None)
        self.sample_rate = sample_rate          # tasa de entrada de Discord
        self.whisper_sample_rate = 16_000       # Whisper espera 16 kHz
        self.temp_dir = tempfile.gettempdir()
        self._sink: DiscordAudioSink | None = None
        self._session_token: int | None = None
        self._on_revoked = on_revoked

    # ── Ciclo de vida ──────────────────────────────────────────

    async def setup_receiver(self):
        """Instala el AudioSink en el VoiceRecvClient activo via VoiceSessionManager."""
        if not HAS_VOICE_RECV:
            raise VoiceReceiveUnavailableError(
                "discord-ext-voice-recv no está disponible; recepción de audio deshabilitada."
            )
        if self.voice_client is None:
            logger.warning("setup_receiver: no hay VoiceClient disponible todavía.")
            return

        guild_id = self.guild_id or getattr(getattr(self.voice_client, "guild", None), "id", None)
        self._sink = DiscordAudioSink()
        try:
            if guild_id is not None:
                self._session_token = await voice_session_manager.acquire_sink(
                    guild_id=guild_id,
                    voice_client=self.voice_client,
                    owner_mode="voice_commands",
                    sink=self._sink,
                    on_revoked=self._handle_revoked,
                )
            else:
                self.voice_client.listen(self._sink)
            logger.info("AudioSink instalado correctamente en el VoiceClient.")
        except Exception as e:
            logger.error(f"No se pudo instalar el AudioSink: {e}")
            self._sink.cleanup()
            self._sink = None
            self._session_token = None
            raise

    def _handle_revoked(self):
        """Llamado por VoiceSessionManager si otra sesión (ej. /translate) toma el sink."""
        self._session_token = None
        if self._sink:
            self._sink.cleanup()
            self._sink = None
        if callable(self._on_revoked):
            self._on_revoked()

    async def cleanup(self):
        """Detiene la captura y libera recursos de forma aislada e idempotente."""
        guild_id = self.guild_id or getattr(getattr(self.voice_client, "guild", None), "id", None)
        if guild_id is not None and self._session_token is not None:
            token = self._session_token
            self._session_token = None
            voice_session_manager.release_sink(guild_id, token, self.voice_client)
        elif self._sink:
            if self.voice_client and getattr(self.voice_client, "is_listening", lambda: False)():
                self.voice_client.stop_listening()
            self._sink.cleanup()
        self._sink = None

    # Alias requeridos por el resto del código
    async def start_recording(self, user_id: int):
        """No-op: la captura es continua mientras el sink esté activo."""

    async def stop_recording(self, user_id: int):
        """No-op: la captura se detiene con cleanup()."""

    # ── Captura inteligente ────────────────────────────────────

    async def smart_record_audio(
        self,
        user_id: int,
        max_record_time: float = 15.0,
        silence_threshold: float = 0.01,
        min_speech_duration: float = 0.3,
        chunk_seconds: float = 0.5,
    ) -> str | None:
        """
        Acumula audio del usuario hasta detectar silencio o alcanzar max_record_time.

        Returns:
            Ruta al archivo WAV mono 16 kHz listo para Whisper, o None si no hubo voz.
        """
        if self._sink is None:
            logger.warning(f"smart_record_audio: sink no disponible para usuario {user_id}. "
                           "¿Se llamó setup_receiver()?")
            return None

        logger.info(f"Iniciando grabación dinámica para usuario {user_id}")

        audio_collected: list[np.ndarray] = []
        total_time = 0.0
        chunk_samples = int(self.DISCORD_SAMPLE_RATE * chunk_seconds) * self.DISCORD_CHANNELS

        while total_time < max_record_time:
            await asyncio.sleep(chunk_seconds)
            total_time += chunk_seconds

            raw = await self._sink.get_audio(user_id)
            if not raw:
                # Sin datos: silencio o usuario mudo
                if audio_collected:
                    logger.info(f"Silencio detectado tras voz para {user_id}. Finalizando.")
                    break
                continue

            # Convertir bytes → int16 numpy
            n_samples = len(raw) // self.BYTES_PER_SAMPLE
            block = np.frombuffer(raw[:n_samples * self.BYTES_PER_SAMPLE], dtype=np.int16)

            # Pasar a mono
            block_mono = block.reshape(-1, self.DISCORD_CHANNELS).mean(axis=1).astype(np.int16)

            # VAD simple
            has_voice = await asyncio.to_thread(
                detect_voice_activity,
                block_mono,
                threshold=silence_threshold,
                min_speech_duration=min_speech_duration,
            )

            if has_voice:
                logger.debug(f"Voz detectada en chunk para {user_id}.")
                audio_collected.append(block_mono)
            elif audio_collected:
                logger.info(f"Silencio post-voz para {user_id}. Finalizando.")
                break

        if not audio_collected:
            logger.info(f"No se detectó voz para usuario {user_id}.")
            return None

        # Concatenar y remuestrear a 16 kHz para Whisper
        audio_48k = np.concatenate(audio_collected)
        audio_16k = self._resample(audio_48k, self.DISCORD_SAMPLE_RATE, self.whisper_sample_rate)

        audio_path = os.path.join(self.temp_dir, f"user_{user_id}_temp.wav")
        save_audio_to_file(audio_16k, sample_rate=self.whisper_sample_rate, file_path=audio_path)

        logger.info(f"Audio guardado en: {audio_path} "
                    f"({len(audio_16k)/self.whisper_sample_rate:.1f}s, 16kHz mono)")
        return audio_path

    # ── Utilidades internas ────────────────────────────────────

    @staticmethod
    def _resample(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
        """
        Remuestreo simple por interpolación lineal.
        Para producción se puede cambiar por librosa.resample o scipy.signal.resample.
        """
        if orig_sr == target_sr:
            return audio
        ratio = target_sr / orig_sr
        n_samples_out = int(len(audio) * ratio)
        indices = np.linspace(0, len(audio) - 1, n_samples_out)
        audio_resampled = np.interp(indices, np.arange(len(audio)), audio).astype(np.int16)
        return audio_resampled
