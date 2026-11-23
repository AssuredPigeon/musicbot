# modules/transcription_server.py
#
# Endpoint HTTP interno para procesar transcripciones de voz hacia comandos del bot.
#
# Seguridad y robustez (Bloque 0A v3.1):
#   - Enlazado exclusivamente a loopback (127.0.0.1:5050).
#   - Autenticación fail-closed mediante TRANSCRIPTION_API_TOKEN (X-Transcription-Token).
#   - Límite de payload HTTP (16 KB) y de texto (300 chars), validación estricta de tipos.
#   - Pasa por IntentProcessor y aplica lista blanca ALLOWED_HTTP_INTENTS.
#   - Comunicación thread-safe con bot.loop via asyncio.run_coroutine_threadsafe(timeout=8.0).
#   - Idempotencia por request_id + compuerta atómica de cancelación para evitar duplicados tras timeout.

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import hmac
import logging
import os
import threading
import time
from flask import Flask, jsonify, request

import config
from modules.intent_processor import IntentProcessor
from modules.voice_commands import VoiceCommandHandler

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # 16 KB máx.

logger = logging.getLogger("musicbot.transcription_server")

bot = None
music_player = None
voice_command_handler = None
intent_processor = IntentProcessor()

ALLOWED_HTTP_INTENTS = frozenset({
    "play_song",
    "pause",
    "resume",
    "skip",
    "stop",
    "volume",
    "queue",
    "help",
})

MAX_TRANSCRIPTION_LEN = 300
REQUEST_TIMEOUT_S = 8.0
LEDGER_TTL_S = 300.0
LEDGER_MAX_ENTRIES = 500

_ledger_lock = threading.Lock()
_request_ledger: dict[str, dict] = {}


def _get_configured_token() -> str:
    cfg_tok = getattr(config, "TRANSCRIPTION_API_TOKEN", "") or ""
    env_tok = os.getenv("TRANSCRIPTION_API_TOKEN", "") or ""
    return (cfg_tok or env_tok).strip()


def _prune_ledger(now: float) -> None:
    expired = [k for k, v in _request_ledger.items() if (now - v["updated_at"]) > LEDGER_TTL_S]
    for k in expired:
        _request_ledger.pop(k, None)
    if len(_request_ledger) > LEDGER_MAX_ENTRIES:
        oldest = sorted(_request_ledger.items(), key=lambda kv: kv[1]["updated_at"])
        for k, _ in oldest[: len(_request_ledger) - LEDGER_MAX_ENTRIES]:
            _request_ledger.pop(k, None)


def reset_request_ledger() -> None:
    """Limpia el registro de idempotencia (útil para tests)."""
    with _ledger_lock:
        _request_ledger.clear()


async def _process_and_handle(
    user_id: int,
    guild_id: int,
    transcription: str,
    cancel_event: threading.Event,
    gate_lock: threading.Lock,
    exec_state: dict,
) -> dict:
    intent_result = await intent_processor.process_text(transcription)
    intent = intent_result.get("intent", "unknown")
    entities = intent_result.get("entities", [])

    if intent not in ALLOWED_HTTP_INTENTS:
        return {
            "accepted": False,
            "status": "rejected_intent",
            "intent": intent,
            "message": f"Intención no permitida o no reconocida: {intent}",
        }

    # Compuerta atómica: si el endpoint ya expiró antes de entrar, abortar sin ejecutar acción
    with gate_lock:
        if cancel_event.is_set():
            exec_state["aborted_before_execution"] = True
            return {
                "accepted": False,
                "status": "aborted_before_execution",
                "intent": intent,
            }
        exec_state["started_execution"] = True

    reply = await voice_command_handler.handle_voice_command(
        user_id, guild_id, intent, entities, transcription
    )
    return {
        "accepted": True,
        "status": "executed",
        "intent": intent,
        "reply": reply,
    }


@app.errorhandler(413)
def _payload_too_large(_error):
    return jsonify({"error": "Payload excede el límite permitido (16 KB)."}), 413


@app.route("/transcription", methods=["POST"])
def handle_transcription():
    expected_token = _get_configured_token()
    if not expected_token:
        return jsonify({"error": "Endpoint deshabilitado: TRANSCRIPTION_API_TOKEN no configurado."}), 503

    provided_token = request.headers.get("X-Transcription-Token", "")
    if not isinstance(provided_token, str) or not provided_token:
        return jsonify({"error": "No autorizado."}), 401
    try:
        provided_token_bytes = provided_token.encode("utf-8")
        expected_token_bytes = expected_token.encode("utf-8")
    except (UnicodeError, TypeError):
        return jsonify({"error": "No autorizado."}), 401
    if not hmac.compare_digest(provided_token_bytes, expected_token_bytes):
        return jsonify({"error": "No autorizado."}), 401

    if not request.is_json:
        return jsonify({"error": "Content-Type debe ser application/json."}), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON inválido."}), 400

    user_id = data.get("user_id")
    guild_id = data.get("guild_id")
    transcription = data.get("transcription")

    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return jsonify({"error": "user_id debe ser un entero positivo."}), 400
    if isinstance(guild_id, bool) or not isinstance(guild_id, int) or guild_id <= 0:
        return jsonify({"error": "guild_id debe ser un entero positivo."}), 400
    if not isinstance(transcription, str) or not transcription.strip():
        return jsonify({"error": "transcription debe ser texto no vacío."}), 400

    transcription_clean = transcription.strip()
    if len(transcription_clean) > MAX_TRANSCRIPTION_LEN:
        return jsonify({"error": f"transcription excede {MAX_TRANSCRIPTION_LEN} caracteres."}), 400

    req_id = request.headers.get("X-Request-ID")
    if req_id is None:
        req_id = data.get("request_id")
    if not isinstance(req_id, str) or not req_id.strip() or len(req_id.strip()) > 128:
        return jsonify({
            "error": (
                "request_id es obligatorio (header X-Request-ID o campo JSON request_id, "
                "cadena no vacía de hasta 128 caracteres)."
            )
        }), 400
    req_id = req_id.strip()

    payload_hash = hashlib.sha256(
        f"{user_id}:{guild_id}:{transcription_clean}".encode("utf-8")
    ).hexdigest()

    now = time.monotonic()
    with _ledger_lock:
        _prune_ledger(now)
        existing = _request_ledger.get(req_id)
        if existing is not None:
            if existing.get("payload_hash") != payload_hash:
                return jsonify({
                    "error": "request_id ya fue utilizado con un contenido diferente.",
                    "request_id": req_id,
                    "status": "payload_mismatch",
                }), 409
            st = existing["state"]
            if st == "COMPLETED":
                resp = dict(existing["payload"])
                resp["deduplicated"] = True
                return jsonify(resp), existing["http_code"]
            if st == "IN_PROGRESS":
                return jsonify({
                    "error": "Solicitud ya en progreso.",
                    "request_id": req_id,
                    "status": "in_progress",
                }), 409
            if st == "TIMED_OUT_UNCERTAIN":
                return jsonify({
                    "error": "Timeout previo con estado incierto; no se re-ejecuta para evitar duplicados.",
                    "request_id": req_id,
                    "status": "timeout_uncertain",
                }), 409
            if st == "ABORTED":
                return jsonify({
                    "error": "Solicitud previa abortada por timeout antes de ejecutar.",
                    "request_id": req_id,
                    "status": "timeout_aborted",
                }), 409
        _request_ledger[req_id] = {
            "state": "IN_PROGRESS",
            "updated_at": now,
            "payload_hash": payload_hash,
        }

    if (
        bot is None
        or voice_command_handler is None
        or getattr(bot, "loop", None) is None
        or not bot.loop.is_running()
    ):
        with _ledger_lock:
            _request_ledger.pop(req_id, None)
        return jsonify({"error": "Bot no disponible o event loop inactivo."}), 503

    cancel_event = threading.Event()
    gate_lock = threading.Lock()
    exec_state = {"started_execution": False, "aborted_before_execution": False}

    try:
        future = asyncio.run_coroutine_threadsafe(
            _process_and_handle(
                user_id, guild_id, transcription_clean, cancel_event, gate_lock, exec_state
            ),
            bot.loop,
        )
        outcome = future.result(timeout=REQUEST_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        with gate_lock:
            cancel_event.set()
            started = exec_state["started_execution"]
        future.cancel()

        ledger_state = "TIMED_OUT_UNCERTAIN" if started else "ABORTED"
        status_label = "timeout_uncertain" if started else "timeout_aborted"
        payload = {
            "error": (
                "Timeout alcanzado durante la ejecución; el resultado es incierto."
                if started
                else "Timeout alcanzado; comando abortado antes de ejecutarse."
            ),
            "status": status_label,
            "request_id": req_id,
        }
        with _ledger_lock:
            _request_ledger[req_id] = {
                "state": ledger_state,
                "updated_at": time.monotonic(),
                "payload": payload,
                "http_code": 504,
                "payload_hash": payload_hash,
            }
        return jsonify(payload), 504
    except Exception:
        logger.exception("Error procesando petición en /transcription")
        with _ledger_lock:
            _request_ledger.pop(req_id, None)
        return jsonify({"error": "Error interno procesando transcripción."}), 500

    http_code = 200 if outcome.get("accepted") else 400
    payload = dict(outcome)
    payload["request_id"] = req_id
    with _ledger_lock:
        _request_ledger[req_id] = {
            "state": "COMPLETED",
            "updated_at": time.monotonic(),
            "payload": payload,
            "http_code": http_code,
            "payload_hash": payload_hash,
        }
    return jsonify(payload), http_code


def run_server():
    app.run(host="127.0.0.1", port=5050, use_reloader=False)


def start_transcription_server(bot_ref, music_player_ref, voice_command_handler_ref=None):
    global bot, music_player, voice_command_handler
    bot = bot_ref
    music_player = music_player_ref
    handler = (
        voice_command_handler_ref
        or getattr(music_player_ref, "voice_command_handler", None)
        or voice_command_handler
        or VoiceCommandHandler(bot, music_player)
    )
    if (
        music_player_ref is not None
        and getattr(music_player_ref, "voice_command_handler", None) is None
        and hasattr(music_player_ref, "register_voice_command_handler")
    ):
        music_player_ref.register_voice_command_handler(handler)
    voice_command_handler = handler

    thread = threading.Thread(target=run_server, name="transcription-http", daemon=True)
    thread.start()
