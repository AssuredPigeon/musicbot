# translator/profile.py
#
# Perfil adaptativo por usuario.
#
# Capa de aprendizaje ligera:
#   1. Context window  -> ultimas N frases del usuario como prompt de Whisper
#                         mejora la transcripcion usando su vocabulario habitual
#   2. Glosario propio -> palabras/frases con traduccion corregida por el usuario
#                         se aplican antes y despues de traducir con Google
#   3. Vocab extraction-> terminos frecuentes que se detectan automaticamente
#                         y se añaden al prompt de Whisper para mejor precision
#
# Con el tiempo el sistema aprende:
#   - El acento/vocabulario del usuario (mejora STT)
#   - Sus correcciones de traduccion (mejora TR)
#   - Sus frases frecuentes (mejora coherencia)

import re
import logging
from collections import Counter
from translator.database import (
    get_user_settings,
    get_recent_phrases,
    get_glossary,
    increment_glossary_usage,
)

logger = logging.getLogger("musicbot.translator.profile")

# ──────────────────────────────────────────────────────────────
# Idiomas soportados
# ──────────────────────────────────────────────────────────────

SUPPORTED_LANGUAGES = {
    "es": "Spanish",
    "en": "English",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "ru": "Russian",
    "ar": "Arabic",
    "nl": "Dutch",
    "pl": "Polish",
    "tr": "Turkish",
}

# Voces de edge-tts por idioma (naturales y gratuitas)
LANGUAGE_VOICES = {
    "es": "es-ES-AlvaroNeural",
    "en": "en-US-GuyNeural",
    "fr": "fr-FR-HenriNeural",
    "de": "de-DE-ConradNeural",
    "it": "it-IT-DiegoNeural",
    "pt": "pt-BR-AntonioNeural",
    "ja": "ja-JP-KeitaNeural",
    "ko": "ko-KR-InJoonNeural",
    "zh": "zh-CN-YunxiNeural",
    "ru": "ru-RU-DmitryNeural",
    "ar": "ar-SA-HamedNeural",
    "nl": "nl-NL-MaartenNeural",
    "pl": "pl-PL-MarekNeural",
    "tr": "tr-TR-AhmetNeural",
}

def get_voice_for_lang(lang_code: str) -> str:
    return LANGUAGE_VOICES.get(lang_code, "en-US-GuyNeural")

def get_lang_name(lang_code: str) -> str:
    return SUPPORTED_LANGUAGES.get(lang_code, lang_code.upper())

def is_valid_lang(lang_code: str) -> bool:
    return lang_code in SUPPORTED_LANGUAGES


# ──────────────────────────────────────────────────────────────
# Contexto adaptativo para Whisper
# ──────────────────────────────────────────────────────────────

async def build_whisper_prompt(user_id: int) -> str:
    """
    Construye un prompt inicial para Whisper basado en el historial del usuario.

    Whisper acepta un 'initial_prompt' que sesga el modelo hacia ese vocabulario.
    Con las frases recientes del usuario, Whisper aprende su jerga y acento.

    Returns:
        str: Prompt de hasta 224 tokens (limite de Whisper)
    """
    recent = await get_recent_phrases(user_id, limit=8)
    if not recent:
        return ""

    # Extraer palabras unicas frecuentes (vocabulario del usuario)
    all_text = " ".join(recent)
    words = re.findall(r"\b\w{3,}\b", all_text.lower())
    freq = Counter(words)

    # Top 20 palabras mas usadas por el usuario
    top_words = [w for w, _ in freq.most_common(20)]

    # Construir prompt: ultimas 3 frases + top words
    context_phrases = recent[:3]
    prompt = ". ".join(context_phrases)
    if top_words:
        prompt += ". " + ", ".join(top_words)

    # Whisper trunca a ~224 tokens (~1000 chars)
    return prompt[:900]


# ──────────────────────────────────────────────────────────────
# Glosario personal: aplicar correcciones conocidas
# ──────────────────────────────────────────────────────────────

async def apply_glossary(
    user_id: int,
    text: str,
    tgt_lang: str,
    direction: str = "pre",
) -> str:
    """
    Aplica el glosario personal del usuario al texto.

    direction="pre"  -> aplicar al texto ORIGINAL antes de traducir
                        (reemplaza terminos que Google traduce mal)
    direction="post" -> aplicar al texto TRADUCIDO despues de traducir
                        (fuerza terminos especificos en el idioma destino)

    Args:
        user_id:   ID del usuario
        text:      Texto a procesar
        tgt_lang:  Idioma destino (para filtrar entradas del glosario)
        direction: "pre" o "post"

    Returns:
        Texto con reemplazos del glosario aplicados
    """
    glossary = await get_glossary(user_id, tgt_lang)
    if not glossary:
        return text

    modified = text
    for entry in glossary:
        original = entry["original"]
        replacement = entry["translation"]

        # Reemplazar con boundary de palabra (case-insensitive)
        pattern = re.compile(r"\b" + re.escape(original) + r"\b", re.IGNORECASE)
        if pattern.search(modified):
            modified = pattern.sub(replacement, modified)
            # Registrar uso del glosario (aprendizaje implicito)
            await increment_glossary_usage(user_id, original, tgt_lang)

    return modified


# ──────────────────────────────────────────────────────────────
# Extraer vocabulario automaticamente (aprendizaje pasivo)
# ──────────────────────────────────────────────────────────────

def extract_domain_terms(texts: list[str]) -> list[str]:
    """
    Extrae terminos de dominio especifico de una lista de frases.

    Util para detectar automaticamente: nombres de juegos, personajes,
    habilidades, slang de gaming, etc. que los traductores genericos
    suelen traducir incorrectamente.

    Heuristica: palabras con mayuscula inicial que NO son inicio de
    oracion, o palabras no reconocidas por el diccionario comun.
    """
    all_text = " ".join(texts)
    # Palabras con mayuscula que aparecen en medio de oracion
    proper_nouns = re.findall(r"(?<![.!?]\s)(?<!\A)\b[A-Z][a-z]{2,}\b", all_text)
    freq = Counter(proper_nouns)
    # Solo los que aparecen mas de 1 vez (probablemente relevantes)
    return [w for w, count in freq.most_common(15) if count > 1]
