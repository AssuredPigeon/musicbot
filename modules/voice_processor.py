# modules/voice_processor.py
#
# Pipeline de transcripción de audio.
#
# Motor primario : Whisper local  (siempre disponible, gratis)
# Motor fallback : OpenAI API     (activado via WHISPER_USE_API=true en .env)
#
# Flujo:
#   1.  smart_record_audio() → WAV 16kHz mono
#   2.  transcribe_audio()   → texto
#   3.  IntentProcessor      → intent + entities

import os
import logging
import asyncio
import tempfile
from concurrent.futures import ThreadPoolExecutor

import config
from utils.error_handler import log_exception
from modules.intent_processor import IntentProcessor

logger = logging.getLogger('musicbot.voice')

# Executor para operaciones CPU-intensivas (Whisper local)
executor = ThreadPoolExecutor(max_workers=2)

intent_processor = IntentProcessor()

# ──────────────────────────────────────────────────────────────
# Carga condicional de motores
# ──────────────────────────────────────────────────────────────

_whisper_local_model = None
_openai_client = None

def _load_local_whisper():
    """Carga el modelo Whisper local en memoria (solo una vez)."""
    global _whisper_local_model
    if _whisper_local_model is None:
        try:
            import whisper as _whisper
            logger.info(f"Cargando modelo Whisper local '{config.WHISPER_MODEL_SIZE}'...")
            _whisper_local_model = _whisper.load_model(config.WHISPER_MODEL_SIZE)
            logger.info("Modelo Whisper local cargado.")
        except ImportError:
            logger.error("openai-whisper no está instalado. Instálalo con: pip install openai-whisper")
        except Exception as e:
            logger.error(f"No se pudo cargar el modelo Whisper local: {e}")
    return _whisper_local_model


def _load_openai_client():
    """Carga el cliente de OpenAI para la API (solo una vez)."""
    global _openai_client
    if _openai_client is None:
        api_key = config.OPENAI_API_KEY
        if not api_key:
            logger.warning("OPENAI_API_KEY no definida. El fallback de API no estará disponible.")
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


# Carga al importar el módulo según configuración
if config.WHISPER_USE_API:
    logger.info("Modo transcripción: OpenAI API (WHISPER_USE_API=true)")
    _load_openai_client()
else:
    logger.info("Modo transcripción: Whisper local")
    _load_local_whisper()


# ──────────────────────────────────────────────────────────────
# Transcripción
# ──────────────────────────────────────────────────────────────

async def _transcribe_with_local(audio_path: str, language: str = "es") -> str | None:
    """
    Transcribe con el modelo Whisper local en un thread separado
    para no bloquear el event loop.
    """
    model = _load_local_whisper()
    if model is None:
        return None

    def _run():
        result = model.transcribe(
            audio_path,
            language=language,
            # Prompt inicial para mejorar la detección de comandos en español
            initial_prompt="Reproduce, pausa, salta, detén, volumen, cola, ayuda.",
            fp16=False,  # Más compatible en CPU
        )
        return result["text"].strip()

    try:
        text = await asyncio.get_event_loop().run_in_executor(executor, _run)
        return text if text else None
    except Exception as e:
        log_exception("Error en transcripción local", e)
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
                prompt="Reproduce, pausa, salta, detén, volumen, cola, ayuda.",
            )
        text = response.text.strip()
        return text if text else None
    except Exception as e:
        log_exception("Error en transcripción via OpenAI API", e)
        return None


async def transcribe_audio(audio_path: str, language: str = "es") -> str | None:
    """
    Selecciona el motor de transcripción según configuración:
      - WHISPER_USE_API=false → Whisper local
      - WHISPER_USE_API=true  → OpenAI API (con fallback a local si falla)
    """
    if not os.path.exists(audio_path):
        logger.warning(f"Archivo de audio no encontrado: {audio_path}")
        return None

    if config.WHISPER_USE_API:
        logger.debug("Usando OpenAI API para transcripción.")
        text = await _transcribe_with_api(audio_path, language)
        if text:
            return text
        # Fallback a local si la API falla
        logger.warning("API falló, intentando con Whisper local como fallback...")
        return await _transcribe_with_local(audio_path, language)

    logger.debug("Usando Whisper local para transcripción.")
    return await _transcribe_with_local(audio_path, language)


# ──────────────────────────────────────────────────────────────
# Filtros de calidad de transcripción
# ──────────────────────────────────────────────────────────────

# Texto que Whisper genera cuando no hay audio o hay ruido de fondo
_INVALID_TRANSCRIPTIONS = {
    "", "gracias", "thank you", "thanks", "música", "music",
    "subtítulos realizados por la comunidad de amara.org",
    "suscríbete al canal", "subtitles by the amara.org community",
}

def _is_valid_transcription(text: str) -> bool:
    """
    Filtra transcripciones vacías o alucinaciones comunes de Whisper.
    """
    if not text:
        return False
    cleaned = text.lower().strip(" .,!?")
    if cleaned in _INVALID_TRANSCRIPTIONS:
        logger.debug(f"Transcripción filtrada (alucinación): '{text}'")
        return False
    if len(cleaned) < 3:
        return False
    return True


# ──────────────────────────────────────────────────────────────
# Función principal del pipeline
# ──────────────────────────────────────────────────────────────

async def process_audio_file(audio_path: str, user_id: int) -> dict | None:
    """
    Pipeline completo: archivo WAV → transcripción → intent + entities.

    Returns:
        dict con keys "transcription", "intent", "entities" o None si falla.
    """
    if not os.path.exists(audio_path):
        logger.warning(f"Archivo de audio no encontrado: {audio_path}")
        return None

    try:
        transcription = await transcribe_audio(audio_path)

        # Limpiar archivo temporal
        try:
            os.remove(audio_path)
        except Exception:
            pass

        if not _is_valid_transcription(transcription):
            logger.info(f"Transcripción inválida o vacía para {user_id}: '{transcription}'")
            return None

        logger.info(f"[Usuario {user_id}] Transcripción: '{transcription}'")

        # Detectar intención y entidades
        intent_result = await intent_processor.process_text(transcription)
        logger.info(f"[Usuario {user_id}] Intent: {intent_result['intent']} | Entities: {intent_result['entities']}")

        return {
            "transcription": transcription,
            "intent": intent_result["intent"],
            "entities": intent_result["entities"],
        }

    except Exception as e:
        log_exception(f"Error procesando audio para usuario {user_id}", e)
        return None


async def capture_and_process_voice(user_id: int, guild_id: int, voice_receiver) -> tuple | None:
    """
    Captura voz real usando el VoiceReceiver y la procesa con Whisper.

    Returns:
        (transcription, intent, entities) o None si no hubo voz reconocible.
    """
    try:
        audio_path = await voice_receiver.smart_record_audio(user_id)

        if not audio_path:
            return None

        result = await process_audio_file(audio_path, user_id)

        if not result:
            return None

        return result["transcription"], result["intent"], result["entities"]

    except Exception as e:
        log_exception(f"Error capturando/procesando voz para usuario {user_id}", e)
        return None
