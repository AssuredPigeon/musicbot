# translator/tts.py
#
# Text-to-Speech usando edge-tts (Microsoft Edge, gratis, 400+ voces).
# Genera audio MP3 en memoria y lo reproduce directamente en Discord.

import asyncio
import io
import logging
import os
import tempfile

logger = logging.getLogger("musicbot.translator.tts")


async def synthesize(text: str, voice: str) -> str | None:
    """
    Genera audio a partir de texto usando edge-tts.

    Args:
        text:  Texto a sintetizar
        voice: Nombre de la voz de edge-tts (ej: "es-ES-AlvaroNeural")

    Returns:
        str: Ruta al archivo temporal MP3, o None si falla.
        IMPORTANTE: El llamador es responsable de borrar el archivo.
    """
    if not text or not text.strip():
        return None

    try:
        import edge_tts

        # Crear archivo temporal para el audio
        tmp = tempfile.NamedTemporaryFile(
            suffix=".mp3", delete=False, prefix="translator_tts_"
        )
        tmp_path = tmp.name
        tmp.close()

        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(tmp_path)

        # Verificar que se generó correctamente
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
            return tmp_path
        else:
            logger.warning(f"edge-tts no generó audio para: '{text[:50]}'")
            return None

    except ImportError:
        logger.error("edge-tts no instalado. Ejecuta: pip install edge-tts")
        return None
    except Exception as e:
        logger.error(f"Error en edge-tts: {e}")
        return None


async def list_voices_for_lang(lang_code: str) -> list[dict]:
    """
    Lista las voces disponibles de edge-tts para un idioma.

    Returns:
        Lista de dicts: [{"Name": ..., "Gender": ..., "Locale": ...}]
    """
    try:
        import edge_tts
        voices = await edge_tts.list_voices()
        # Filtrar por codigo de idioma (ej: "es" filtra es-ES, es-MX, es-AR, etc.)
        filtered = [
            v for v in voices
            if v.get("Locale", "").lower().startswith(lang_code.lower())
        ]
        return filtered
    except Exception as e:
        logger.error(f"Error listando voces de edge-tts: {e}")
        return []
