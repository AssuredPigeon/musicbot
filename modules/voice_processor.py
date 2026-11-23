# modules/voice_processor.py
#
# Pipeline de transcripción de audio.
#
# Motor primario : Cola STT centralizada (utils/stt_queue.py) con instancia única lazy de Whisper
# Motor fallback : OpenAI API (activado via WHISPER_USE_API=true en .env)
#
# Flujo:
#   1. smart_record_audio() -> WAV 16kHz mono
#   2. transcribe_audio()   -> STTQueueManager -> texto
#   3. IntentProcessor      -> intent + entities

from __future__ import annotations

import os
import logging
from typing import Callable

import config
from utils.error_handler import log_exception
from utils.stt_queue import STTStatus, stt_queue_manager
from modules.intent_processor import IntentProcessor

logger = logging.getLogger('musicbot.voice')

intent_processor = IntentProcessor()

_VOICE_COMMANDS_PROMPT = "Reproduce, pausa, salta, detén, volumen, cola, ayuda."

_INVALID_TRANSCRIPTIONS = {
    "", "gracias", "thank you", "thanks", "música", "music",
    "subtítulos realizados por la comunidad de amara.org",
    "suscríbete al canal", "subtitles by the amara.org community",
}

_openai_client = None


def _load_openai_client():
    """Carga el cliente de OpenAI para la API (solo una vez, bajo demanda)."""
    global _openai_client
    if _openai_client is None:
        api_key = config.OPENAI_API_KEY
        if not api_key:
            logger.warning("OPENAI_API_KEY no definida. El modo API no estará disponible.")
            return None
        try:
            from openai import AsyncOpenAI
            _openai_client = AsyncOpenAI(api_key=api_key)
            logger.info("Cliente OpenAI inicializado.")
        except ImportError:
            logger.error("openai no está instalado. Instálalo con: pip install openai")
        except Exception as e:
            logger.error(f"No se pudo inicializar el cliente OpenAI: {e}")
    return _openai_client


async def _transcribe_with_local(
    audio_path: str,
    language: str = "es",
    is_cancelled_cb: Callable[[], bool] | None = None,
) -> str | None:
    """
    Transcribe usando el administrador centralizado STTQueueManager (instancia única lazy).
    """
    result = await stt_queue_manager.transcribe(
        audio_path,
        language=language,
        task="transcribe",
        initial_prompt=_VOICE_COMMANDS_PROMPT,
        hallucination_set=_INVALID_TRANSCRIPTIONS,
        min_chars=3,
        is_cancelled_cb=is_cancelled_cb,
    )

    if result.status == STTStatus.OK:
        return result.text

    if result.status in (
        STTStatus.DROPPED_QUEUE_FULL,
        STTStatus.DROPPED_STALE,
        STTStatus.DROPPED_CANCELLED,
        STTStatus.DROPPED_OVERLOAD,
    ):
        logger.info("Transcripción descartada por cola STT (%s): %s", result.status.value, result.error or "")
    elif result.status == STTStatus.ERROR:
        logger.error("Error STT local: %s", result.error)
    return None


async def _transcribe_with_api(audio_path: str, language: str = "es") -> str | None:
    """
    Transcribe usando la API de OpenAI Whisper (whisper-1).
    Solo se usa si WHISPER_USE_API=true en .env.
    """
    client = _load_openai_client()
    if client is None:
        return None

    try:
        with open(audio_path, "rb") as audio_file:
            response = await client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language=language,
                prompt=_VOICE_COMMANDS_PROMPT,
            )
        text = response.text.strip()
        return text if text else None
    except Exception as e:
        log_exception("Error en transcripción via OpenAI API", e)
        return None


async def transcribe_audio(
    audio_path: str,
    language: str = "es",
    is_cancelled_cb: Callable[[], bool] | None = None,
) -> str | None:
    """
    Selecciona el motor de transcripción según configuración:
      - WHISPER_USE_API=false -> STTQueueManager (Whisper local único)
      - WHISPER_USE_API=true  -> OpenAI API (con fallback a STTQueueManager local si falla)
    """
    if not os.path.exists(audio_path):
        logger.warning(f"Archivo de audio no encontrado: {audio_path}")
        return None

    if config.WHISPER_USE_API:
        logger.debug("Usando OpenAI API para transcripción.")
        text = await _transcribe_with_api(audio_path, language)
        if text:
            return text
        logger.warning("API falló, intentando con cola STT local como fallback...")
        return await _transcribe_with_local(audio_path, language=language, is_cancelled_cb=is_cancelled_cb)

    logger.debug("Usando STTQueueManager local para transcripción.")
    return await _transcribe_with_local(audio_path, language=language, is_cancelled_cb=is_cancelled_cb)


def _is_valid_transcription(text: str | None) -> bool:
    """Filtra transcripciones vacías o alucinaciones comunes de Whisper."""
    if not text:
        return False
    cleaned = text.lower().strip(" .,!?")
    if cleaned in _INVALID_TRANSCRIPTIONS:
        logger.debug(f"Transcripción filtrada (alucinación): '{text}'")
        return False
    if len(cleaned) < 3:
        return False
    return True


async def process_audio_file(
    audio_path: str,
    user_id: int,
    is_cancelled_cb: Callable[[], bool] | None = None,
) -> dict | None:
    """
    Pipeline completo: archivo WAV -> STTQueueManager -> intent + entities.
    """
    if not os.path.exists(audio_path):
        logger.warning(f"Archivo de audio no encontrado: {audio_path}")
        return None

    try:
        transcription = await transcribe_audio(audio_path, is_cancelled_cb=is_cancelled_cb)

        try:
            os.remove(audio_path)
        except Exception:
            pass

        if is_cancelled_cb is not None and is_cancelled_cb():
            logger.info("Comando descartado para usuario %s: sesión cancelada.", user_id)
            return None

        if not _is_valid_transcription(transcription):
            logger.info(f"Transcripción inválida o vacía para {user_id}: '{transcription}'")
            return None

        logger.info(f"[Usuario {user_id}] Transcripción: '{transcription}'")
        intent_result = await intent_processor.process_text(transcription)
        logger.info(
            f"[Usuario {user_id}] Intent: {intent_result['intent']} | Entities: {intent_result['entities']}"
        )

        return {
            "transcription": transcription,
            "intent": intent_result["intent"],
            "entities": intent_result["entities"],
        }

    except Exception as e:
        log_exception(f"Error procesando audio para usuario {user_id}", e)
        return None


async def capture_and_process_voice(
    user_id: int,
    guild_id: int,
    voice_receiver,
    is_cancelled_cb: Callable[[], bool] | None = None,
) -> tuple | None:
    """Captura voz real usando el VoiceReceiver y la procesa con la cola STT única."""
    try:
        audio_path = await voice_receiver.smart_record_audio(user_id)
        if not audio_path:
            return None

        result = await process_audio_file(audio_path, user_id, is_cancelled_cb=is_cancelled_cb)
        if not result:
            return None

        return result["transcription"], result["intent"], result["entities"]

    except Exception as e:
        log_exception(f"Error capturando/procesando voz para usuario {user_id}", e)
        return None
