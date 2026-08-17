# translator/translate.py
#
# Motor de traduccion con glosario personal adaptativo.
#
# Motor: deep-translator (Google Translate) — gratis, sin API key
# Mejora: glosario personal por usuario que corrige traducciones incorrectas

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from translator.profile import apply_glossary

logger = logging.getLogger("musicbot.translator.translate")
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="translator-tr")

# Codigos de idioma de Google Translate (compatibles con deep-translator)
GOOGLE_LANG_MAP = {
    "es": "es", "en": "en", "fr": "fr", "de": "de",
    "it": "it", "pt": "pt", "ja": "ja", "ko": "ko",
    "zh": "zh-CN", "ru": "ru", "ar": "ar", "nl": "nl",
    "pl": "pl", "tr": "tr",
}


async def translate_text(
    text: str,
    target_lang: str,
    source_lang: str = "auto",
    user_id: int = None,
) -> dict:
    """
    Traduce texto aplicando el glosario personal del usuario.

    Flujo:
      1. Aplicar glosario "pre" al original (reemplazar antes de traducir)
      2. Traducir con Google Translate
      3. Aplicar glosario "post" al resultado (forzar terminos en idioma destino)

    Args:
        text:        Texto a traducir
        target_lang: Codigo de idioma destino ("es", "en", etc.)
        source_lang: Codigo de idioma origen ("auto" para deteccion automatica)
        user_id:     ID del usuario para aplicar su glosario personal

    Returns:
        dict: {
            "translated": str,
            "source_lang": str,   -> idioma detectado por Google
            "applied_glossary": bool
        }
    """
    if not text or not text.strip():
        return {"translated": "", "source_lang": source_lang, "applied_glossary": False}

    glossary_applied = False

    # 1. Aplicar glosario PRE-traduccion (reemplazar palabras clave antes de enviar a Google)
    processed_text = text
    if user_id:
        processed_text = await apply_glossary(user_id, text, target_lang, direction="pre")
        glossary_applied = processed_text != text

    # 2. Traducir con Google (en executor para no bloquear el loop)
    tgt = GOOGLE_LANG_MAP.get(target_lang, target_lang)
    src = GOOGLE_LANG_MAP.get(source_lang, source_lang) if source_lang != "auto" else "auto"

    def _translate():
        from deep_translator import GoogleTranslator
        translator = GoogleTranslator(source=src, target=tgt)
        return translator.translate(processed_text)

    try:
        translated = await asyncio.get_event_loop().run_in_executor(_executor, _translate)
    except Exception as e:
        logger.error(f"Error en GoogleTranslator: {e}")
        translated = text  # Fallback: devolver original

    # 3. Aplicar glosario POST-traduccion (forzar terminos en idioma destino)
    if user_id and translated:
        translated_post = await apply_glossary(user_id, translated, target_lang, direction="post")
        if translated_post != translated:
            glossary_applied = True
            translated = translated_post

    return {
        "translated": translated or text,
        "source_lang": source_lang,
        "applied_glossary": glossary_applied,
    }


async def detect_language(text: str) -> str:
    """
    Detecta el idioma de un texto usando Google Translate.
    Util como segundo nivel de validacion despues de Whisper.
    """
    def _detect():
        from deep_translator import GoogleTranslator
        translator = GoogleTranslator(source="auto", target="en")
        # La deteccion se hace internamente; extraemos source del resultado
        result = translator.translate(text[:100])
        return translator.source

    try:
        return await asyncio.get_event_loop().run_in_executor(_executor, _detect)
    except Exception:
        return "unknown"
