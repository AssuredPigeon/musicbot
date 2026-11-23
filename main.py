# main.py — punto de entrada principal
import asyncio
import logging

import config
from data.database import init_orm
from modules.bot_client import (
    auto_message_system,
    bot,
    music_player,
    voice_command_handler,
)
from modules.transcription_server import start_transcription_server
from tortoise import Tortoise
from utils.error_handler import log_exception, setup_logging
from utils.stt_queue import stt_queue_manager
from utils.voice_session_manager import voice_session_manager

# Configurar logging
setup_logging()
logger = logging.getLogger('musicbot')

# Iniciar el servidor Flask interno (127.0.0.1:5050) con la instancia única de MusicPlayer y VoiceCommandHandler
start_transcription_server(bot, music_player, voice_command_handler)


async def main():
    try:
        logger.info("Iniciando MusicBot...")

        await init_orm()

        # Cargar mensajes automáticos antes de iniciar el bot
        logger.info("Cargando sistema de mensajes automáticos...")
        await auto_message_system.load_auto_messages()

        # Iniciar el bot
        await bot.start(config.BOT_TOKEN)
    except Exception as e:
        log_exception("Error crítico en el bot", e)
    finally:
        # Protocolo de apagado ordenado en 4 pasos antes de cerrar el event loop:
        voice_session_manager.begin_closing()

        # Cierre limpio de la cola/executor STT antes de cerrar conexiones
        try:
            shutdown_res = await stt_queue_manager.shutdown(timeout=10.0)
            logger.info("Resultado de cierre STTQueueManager: %s", shutdown_res)
        except Exception as e:
            log_exception("Error cerrando STTQueueManager", e)

        try:
            await music_player.cleanup()
        except Exception as e:
            log_exception("Error limpiando MusicPlayer", e)

        try:
            await voice_session_manager.aclose(timeout=5.0)
        except Exception as e:
            log_exception("Error drenando VoiceSessionManager", e)

        if not bot.is_closed():
            await bot.close()

        try:
            await Tortoise.close_connections()
            logger.info("Conexión a la base de datos cerrada correctamente.")
        except Exception as e:
            log_exception("Error al cerrar la conexión ORM", e)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot detenido manualmente")
    except Exception as e:
        log_exception("Error fatal", e)
        exit(1)
