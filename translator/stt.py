# translator/stt.py
#
# Speech-to-Text para el modulo de traduccion.
# Usa Whisper local (mismo que el music bot pero independiente).
#
# Diferencia clave: aqui usamos initial_prompt con el historial
# del usuario para mejorar la precision de transcripcion con el tiempo.

import asyncio
import logging
import tempfile
import os
import numpy as np
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("musicbot.translator.stt")

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="translator-whisper")
_whisper_model = None
_model_lock = asyncio.Lock()

WHISPER_MODEL_SIZE = "base"  # tiny / base / small / medium

# Frases que Whisper "alucina" con audio de silencio o ruido
HALLUCINATION_PATTERNS = {
    "thank you", "thanks for watching", "suscribete", "gracias",
    "like and subscribe", "see you next time", "bye", "...", ".",
}


def _load_model():
    global _whisper_model
    if _whisper_model is None:
        import whisper
        logger.info(f"Cargando modelo Whisper '{WHISPER_MODEL_SIZE}' para el traductor...")
        _whisper_model = whisper.load_model(WHISPER_MODEL_SIZE)
        logger.info("Modelo Whisper del traductor listo.")
    return _whisper_model


async def transcribe(audio_data: np.ndarray, initial_prompt: str = "") -> dict:
    """
    Transcribe audio y detecta el idioma automaticamente.

    Args:
        audio_data:     Array numpy float32 a 16kHz mono
        initial_prompt: Historial del usuario para sesgar el modelo (aprendizaje)

    Returns:
        dict: {
            "text": str,       -> transcripcion
            "language": str,   -> idioma detectado ("en", "es", etc.)
            "confidence": float
        }
    """
    async with _model_lock:
        model = await asyncio.get_event_loop().run_in_executor(
            _executor, _load_model
        )

    def _run():
        result = model.transcribe(
            audio_data,
            task="transcribe",
            language=None,          # auto-detect
            initial_prompt=initial_prompt or None,
            fp16=False,
            verbose=False,
        )
        return result

    try:
        result = await asyncio.get_event_loop().run_in_executor(_executor, _run)
        text = result.get("text", "").strip()
        lang = result.get("language", "unknown")

        # Filtrar alucinaciones
        if text.lower() in HALLUCINATION_PATTERNS or len(text) < 2:
            return {"text": "", "language": lang, "confidence": 0.0}

        # Estimar confianza (promedio de log-prob de los segmentos)
        segments = result.get("segments", [])
        if segments:
            avg_logprob = sum(s.get("avg_logprob", -1) for s in segments) / len(segments)
            confidence = max(0.0, min(1.0, 1.0 + avg_logprob))
        else:
            confidence = 0.5

        return {"text": text, "language": lang, "confidence": confidence}

    except Exception as e:
        logger.error(f"Error en transcripcion Whisper del traductor: {e}")
        return {"text": "", "language": "unknown", "confidence": 0.0}


def preprocess_audio(raw_pcm: bytes, sample_rate: int = 48000) -> np.ndarray:
    """
    Convierte PCM raw de Discord (48kHz stereo int16) a float32 16kHz mono.
    Igual que en el music bot pero independiente.
    """
    audio_int16 = np.frombuffer(raw_pcm, dtype=np.int16)

    # Stereo -> mono
    if len(audio_int16) % 2 == 0:
        audio_mono = audio_int16.reshape(-1, 2).mean(axis=1)
    else:
        audio_mono = audio_int16.astype(np.float32)

    # int16 -> float32 normalizado [-1, 1]
    audio_float = audio_mono.astype(np.float32) / 32768.0

    # Resamplear 48kHz -> 16kHz (tomar 1 de cada 3 muestras)
    if sample_rate == 48000:
        audio_16k = audio_float[::3]
    else:
        audio_16k = audio_float

    return audio_16k
