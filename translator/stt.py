# translator/stt.py
#
# Speech-to-Text para el modulo de traduccion.
# Usa el administrador centralizado STTQueueManager (instancia única de Whisper compartida
# con musicbot, evitando duplicar ~348 MB de RAM y el KeyError concurrente).

from __future__ import annotations

import logging
from typing import Callable

import numpy as np

from utils.stt_queue import STTStatus, stt_queue_manager

logger = logging.getLogger("musicbot.translator.stt")

# Frases que Whisper "alucina" con audio de silencio o ruido
HALLUCINATION_PATTERNS = {
    "thank you", "thanks for watching", "suscribete", "gracias",
    "like and subscribe", "see you next time", "bye", "...", ".",
}


async def transcribe(
    audio_data: np.ndarray,
    initial_prompt: str = "",
    is_cancelled_cb: Callable[[], bool] | None = None,
) -> dict:
    """
    Transcribe audio y detecta el idioma automaticamente usando la cola STT compartida.

    Returns:
        dict: {
            "text": str,
            "language": str,
            "confidence": float,
            "status": str
        }
    """
    duration_s = float(len(audio_data)) / 16000.0 if hasattr(audio_data, "__len__") else None
    result = await stt_queue_manager.transcribe(
        audio_data,
        language=None,  # auto-detect
        task="transcribe",
        initial_prompt=initial_prompt or None,
        hallucination_set=HALLUCINATION_PATTERNS,
        min_chars=2,
        audio_duration_s=duration_s,
        is_cancelled_cb=is_cancelled_cb,
    )

    if result.status == STTStatus.OK:
        return {
            "text": result.text,
            "language": result.language,
            "confidence": result.confidence,
            "status": result.status.value,
        }

    if result.status in (
        STTStatus.DROPPED_QUEUE_FULL,
        STTStatus.DROPPED_STALE,
        STTStatus.DROPPED_CANCELLED,
        STTStatus.DROPPED_OVERLOAD,
    ):
        logger.info("STT traductor descartado (%s): %s", result.status.value, result.error or "")

    return {
        "text": "",
        "language": result.language,
        "confidence": 0.0,
        "status": result.status.value,
    }


def preprocess_audio(raw_pcm: bytes, sample_rate: int = 48000) -> np.ndarray:
    """
    Convierte PCM raw de Discord (48kHz stereo int16) a float32 16kHz mono.
    """
    audio_int16 = np.frombuffer(raw_pcm, dtype=np.int16)

    if len(audio_int16) % 2 == 0:
        audio_mono = audio_int16.reshape(-1, 2).mean(axis=1)
    else:
        audio_mono = audio_int16.astype(np.float32)

    audio_float = audio_mono.astype(np.float32) / 32768.0

    if sample_rate == 48000:
        audio_16k = audio_float[::3]
    else:
        audio_16k = audio_float

    return audio_16k
