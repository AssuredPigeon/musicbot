# utils/cookie_manager.py
#
# Gestión de cookies de YouTube para yt-dlp.
#
# Problema:
#   Las cookies de YouTube (exportadas manualmente con Cookie-Editor)
#   tienen sesiones que expiran. Cuando expiran, yt-dlp empieza a recibir
#   errores HTTP 403 o "Sign in to confirm you're not a bot".
#
# Solución implementada:
#   1. Verificar la fecha de expiración de las cookies al arrancar el bot
#   2. Detectar errores de cookie en tiempo de ejecución y notificar al owner
#   3. Intentar sin cookies como fallback (calidad reducida, posible rate-limit)
#
# Renovación manual (desde tu PC local):
#   Ejecuta: scripts/refresh_cookies.sh
#   Ese script extrae cookies frescas del navegador y las sube al servidor.

import os
import time
import logging
import asyncio
from pathlib import Path

import config
from utils.error_handler import log_exception

logger = logging.getLogger('musicbot.cookies')

# ──────────────────────────────────────────────────────────────
# Lectura y validación del archivo de cookies
# ──────────────────────────────────────────────────────────────

def parse_cookies_expiry(cookies_path: str) -> list[dict]:
    """
    Lee el archivo cookies.txt (formato Netscape) y devuelve las cookies
    con sus fechas de expiración.

    Returns:
        Lista de dicts: [{"name": ..., "expires": unix_timestamp}, ...]
    """
    cookies = []
    if not os.path.exists(cookies_path):
        return cookies

    try:
        with open(cookies_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                # Ignorar comentarios y líneas vacías
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) >= 7:
                    try:
                        expires = int(parts[4])
                        name = parts[5]
                        cookies.append({"name": name, "expires": expires})
                    except (ValueError, IndexError):
                        pass
    except Exception as e:
        logger.error(f"Error leyendo cookies: {e}")

    return cookies


def check_cookies_health(cookies_path: str) -> dict:
    """
    Evalúa el estado de salud del archivo de cookies.

    Returns:
        dict con:
          - "status": "ok" | "expiring_soon" | "expired" | "missing"
          - "days_until_expiry": días para la cookie crítica más próxima a expirar
          - "message": descripción del estado
    """
    if not os.path.exists(cookies_path) or os.path.getsize(cookies_path) == 0:
        return {
            "status": "missing",
            "days_until_expiry": 0,
            "message": "❌ cookies.txt no existe o está vacío. YouTube puede bloquear peticiones.",
        }

    cookies = parse_cookies_expiry(cookies_path)
    if not cookies:
        return {
            "status": "missing",
            "days_until_expiry": 0,
            "message": "⚠️ No se pudieron leer las cookies del archivo.",
        }

    now = time.time()

    # Filtrar cookies con expiración real (>0 = no de sesión)
    timed_cookies = [c for c in cookies if c["expires"] > 0]

    if not timed_cookies:
        # Solo cookies de sesión, sin fecha definida
        return {
            "status": "ok",
            "days_until_expiry": 999,
            "message": "✅ Cookies de sesión activas (sin fecha de expiración fija).",
        }

    # La cookie que expira antes
    soonest = min(timed_cookies, key=lambda c: c["expires"])
    days_left = (soonest["expires"] - now) / 86400

    if days_left < 0:
        return {
            "status": "expired",
            "days_until_expiry": int(days_left),
            "message": (
                f"🔴 Las cookies de YouTube han EXPIRADO hace {abs(int(days_left))} días. "
                "Ejecuta `scripts/refresh_cookies.sh` para renovarlas."
            ),
        }
    elif days_left < 7:
        return {
            "status": "expiring_soon",
            "days_until_expiry": int(days_left),
            "message": (
                f"🟡 Las cookies de YouTube expiran en {int(days_left)} días. "
                "Considera renovarlas pronto con `scripts/refresh_cookies.sh`."
            ),
        }
    else:
        return {
            "status": "ok",
            "days_until_expiry": int(days_left),
            "message": f"✅ Cookies de YouTube válidas ({int(days_left)} días restantes).",
        }


# ──────────────────────────────────────────────────────────────
# Detección de errores de cookie en tiempo de ejecución
# ──────────────────────────────────────────────────────────────

# Mensajes de error típicos que indican cookies expiradas/inválidas
COOKIE_ERROR_PATTERNS = [
    "sign in to confirm",
    "bot",
    "403",
    "http error 403",
    "private video",
    "video unavailable",
    "this video is not available",
    "confirm your age",
    "cookies",
]

def is_cookie_error(error_message: str) -> bool:
    """
    Detecta si un error de yt-dlp es probablemente por cookies expiradas.
    """
    msg = error_message.lower()
    return any(pattern in msg for pattern in COOKIE_ERROR_PATTERNS)


# ──────────────────────────────────────────────────────────────
# Tarea de monitoreo periódico
# ──────────────────────────────────────────────────────────────

class CookieMonitor:
    """
    Tarea de background que verifica el estado de las cookies
    y notifica al owner del bot cuando están a punto de expirar.
    """

    def __init__(self, bot, check_interval_hours: int = 24):
        self.bot = bot
        self.check_interval = check_interval_hours * 3600
        self._task: asyncio.Task | None = None
        self._last_status = None

    def start(self):
        """Inicia la tarea de monitoreo en segundo plano."""
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("CookieMonitor iniciado.")

    def stop(self):
        if self._task:
            self._task.cancel()

    async def _monitor_loop(self):
        # Verificar inmediatamente al arrancar
        await self._check_and_notify()
        while True:
            await asyncio.sleep(self.check_interval)
            await self._check_and_notify()

    async def _check_and_notify(self):
        try:
            health = check_cookies_health(config.COOKIES_PATH)
            status = health["status"]

            logger.info(f"[CookieMonitor] {health['message']}")

            # Solo notificar si el estado cambió o es crítico
            if status in ("expired", "expiring_soon") and status != self._last_status:
                await self._notify_owners(health["message"])

            self._last_status = status

        except Exception as e:
            log_exception("Error en CookieMonitor", e)

    async def _notify_owners(self, message: str):
        """Manda un DM a los owners del bot sobre el estado de las cookies."""
        for owner_id in config.OWNER_ID:
            try:
                user = await self.bot.fetch_user(owner_id)
                if user:
                    await user.send(
                        f"🍪 **[MusicBot - Aviso de Cookies]**\n{message}\n\n"
                        f"Para renovar:\n```bash\nbash scripts/refresh_cookies.sh\n```"
                    )
                    logger.info(f"Notificación de cookies enviada al owner {owner_id}")
            except Exception as e:
                logger.warning(f"No se pudo notificar al owner {owner_id}: {e}")
