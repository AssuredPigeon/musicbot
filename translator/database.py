# translator/database.py
#
# Base de datos SQLite para el modulo de traduccion.
# Completamente separada de la DB principal del music bot.
#
# Tablas:
#   translator_settings  -> idioma preferido por usuario
#   translator_glossary  -> glosario personal (correcciones aprendidas)
#   translator_history   -> historial de traducciones (para contexto ML)

import aiosqlite
import logging
import os
from datetime import datetime

logger = logging.getLogger("musicbot.translator.db")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "translator.db")

# ──────────────────────────────────────────────────────────────
# Inicialización
# ──────────────────────────────────────────────────────────────

CREATE_SETTINGS = """
CREATE TABLE IF NOT EXISTS translator_settings (
    user_id       INTEGER PRIMARY KEY,
    target_lang   TEXT    NOT NULL DEFAULT 'es',
    source_lang   TEXT    NOT NULL DEFAULT 'auto',
    voice_name    TEXT,
    active        INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT,
    updated_at    TEXT
)
"""

CREATE_GLOSSARY = """
CREATE TABLE IF NOT EXISTS translator_glossary (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    original      TEXT    NOT NULL,
    translation   TEXT    NOT NULL,
    src_lang      TEXT    NOT NULL DEFAULT 'auto',
    tgt_lang      TEXT    NOT NULL,
    usage_count   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT
)
"""

CREATE_HISTORY = """
CREATE TABLE IF NOT EXISTS translator_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    guild_id       INTEGER NOT NULL,
    original       TEXT    NOT NULL,
    detected_lang  TEXT,
    translated     TEXT,
    target_lang    TEXT,
    corrected      INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT
)
"""

async def init_db():
    """Crea las tablas si no existen."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_SETTINGS)
        await db.execute(CREATE_GLOSSARY)
        await db.execute(CREATE_HISTORY)
        await db.commit()
    logger.info("Translator DB inicializada.")


# ──────────────────────────────────────────────────────────────
# Settings de usuario
# ──────────────────────────────────────────────────────────────

async def get_user_settings(user_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM translator_settings WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            # Defaults
            return {
                "user_id": user_id,
                "target_lang": "es",
                "source_lang": "auto",
                "voice_name": None,
                "active": 0,
            }


async def save_user_settings(user_id: int, **kwargs):
    now = datetime.utcnow().isoformat()
    existing = await get_user_settings(user_id)
    merged = {**existing, **kwargs, "updated_at": now}

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO translator_settings
                (user_id, target_lang, source_lang, voice_name, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                target_lang  = excluded.target_lang,
                source_lang  = excluded.source_lang,
                voice_name   = excluded.voice_name,
                active       = excluded.active,
                updated_at   = excluded.updated_at
            """,
            (
                user_id,
                merged.get("target_lang", "es"),
                merged.get("source_lang", "auto"),
                merged.get("voice_name"),
                merged.get("active", 0),
                merged.get("created_at", now),
                merged.get("updated_at", now),
            ),
        )
        await db.commit()


# ──────────────────────────────────────────────────────────────
# Glosario personal
# ──────────────────────────────────────────────────────────────

async def add_glossary_entry(
    user_id: int, original: str, translation: str, tgt_lang: str, src_lang: str = "auto"
):
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        # Si ya existe, actualiza la traduccion
        await db.execute(
            """
            INSERT INTO translator_glossary (user_id, original, translation, src_lang, tgt_lang, usage_count, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT DO NOTHING
            """,
            (user_id, original.lower().strip(), translation, src_lang, tgt_lang, now),
        )
        # Si existia, actualiza solo la traduccion
        await db.execute(
            """
            UPDATE translator_glossary
            SET translation = ?, src_lang = ?
            WHERE user_id = ? AND LOWER(original) = ? AND tgt_lang = ?
            """,
            (translation, src_lang, user_id, original.lower().strip(), tgt_lang),
        )
        await db.commit()


async def get_glossary(user_id: int, tgt_lang: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM translator_glossary WHERE user_id = ? AND tgt_lang = ? ORDER BY usage_count DESC",
            (user_id, tgt_lang),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def increment_glossary_usage(user_id: int, original: str, tgt_lang: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE translator_glossary
            SET usage_count = usage_count + 1
            WHERE user_id = ? AND LOWER(original) = ? AND tgt_lang = ?
            """,
            (user_id, original.lower().strip(), tgt_lang),
        )
        await db.commit()


async def remove_glossary_entry(user_id: int, original: str, tgt_lang: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM translator_glossary WHERE user_id = ? AND LOWER(original) = ? AND tgt_lang = ?",
            (user_id, original.lower().strip(), tgt_lang),
        )
        await db.commit()


# ──────────────────────────────────────────────────────────────
# Historial de traducciones
# ──────────────────────────────────────────────────────────────

async def save_translation(
    user_id: int,
    guild_id: int,
    original: str,
    detected_lang: str,
    translated: str,
    target_lang: str,
):
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO translator_history
                (user_id, guild_id, original, detected_lang, translated, target_lang, corrected, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (user_id, guild_id, original, detected_lang, translated, target_lang, now),
        )
        await db.commit()


async def get_recent_phrases(user_id: int, limit: int = 5) -> list[str]:
    """Devuelve las ultimas frases del usuario (para contexto de Whisper)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT original FROM translator_history WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]


async def get_translation_history(user_id: int, limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT original, detected_lang, translated, target_lang, created_at
            FROM translator_history
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
