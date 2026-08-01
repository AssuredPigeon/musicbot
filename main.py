#main punto de entrada principal
import logging  #Modulo estandar para registrar log (mensajes de informacion, advertencia, etc)
import asyncio  #Para manejar las operaciones asincronas (nos sirve para que la espera de multiples eventos que espera)
from modules.bot_client import bot #Importamos el cliente principal del bot desde el modulo
import config #importamos la configuracion del bot
from utils.error_handler import setup_logging, log_exception #Son funciones personalizadas para configurar el sistema de logs.
from modules.transcription_server import start_transcription_server
from modules.music_player import MusicPlayer
from modules.auto_messages import AutoMessageSystem  # Importamos el sistema de mensajes automáticos
from data.database import init_orm
from tortoise import Tortoise 

music_player = MusicPlayer(bot)  # Crea instancia de tu reproductor de música
# Configurar logging
setup_logging() #LLamamos a la funcion para configurar el formato y nivel del archivo logs.
logger = logging.getLogger('musicbot')  #crea el logger para registrar los eventos relacionados al bot

# Inicializar el sistema de mensajes automáticos
auto_message_system = AutoMessageSystem(bot)

# Iniciar el servidor Flask en segundo plano
start_transcription_server(bot, music_player)

async def main(): #funcion principal para inicializar al bot
    try:
        logger.info("Iniciando MusicBot...")

        await init_orm()

        # Cargar mensajes automáticos antes de iniciar el bot
        logger.info("Cargando sistema de mensajes automáticos...")
        await auto_message_system.load_auto_messages()

        # Iniciar el bot
        await bot.start(config.BOT_TOKEN) #Inicia el bot con su token. Corre hasta que el bot se detiene.
    except Exception as e:
        log_exception("Error crítico en el bot", e) #si hay alguna excepcion se registra con un mensaje
    finally:  #Nos aseguramos que el bot se cierre correctamente si no esta cerrado
        # Cerrar correctamente al terminar
        if not bot.is_closed():
            await bot.close() #lo cierra

        try:
            await Tortoise.close_connections()
            logger.info("Conexión a la base de datos cerrada correctamente.")
        except Exception as e:
            log_exception("Error al cerrar la conexión ORM", e)

if __name__ == "__main__": #Nos aseguramos que el bloque se ejecute cuando el archivo se ejecuta directamente (si se importa no funciona)
    try:
        # Ejecutar el bucle de eventos principal
        asyncio.run(main()) #Lanza a la funcion main() dentro del bucle de eventos
    except KeyboardInterrupt: #Detectamos si el usuario presiona Ctrl+C para detener al bot manualmente
        logger.info("Bot detenido manualmente")
    except Exception as e:
        log_exception("Error fatal", e) #Captura cualquier otra excepcion y se registra y finaliza el programa con exit(1)
        exit(1)