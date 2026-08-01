# Manejo centralizado de errores
import os
import sys
import logging
import traceback
from datetime import datetime

# Configuración global del logger
def setup_logging():
    """Configura el sistema de logging con formato y nivel adecuados"""
    # Asegurar que existe el directorio de logs
    os.makedirs("logs", exist_ok=True)
    
    # Configurar formato del log
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    date_format = '%Y-%m-%d %H:%M:%S'
    
    # Configurar el logger raíz
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        datefmt=date_format,
        handlers=[
            # Handler para consola
            logging.StreamHandler(),
            # Handler para archivo con rotación diaria
            logging.FileHandler(
                f"logs/musicbot_{datetime.now().strftime('%Y%m%d')}.log"
            )
        ]
    )
    
    # Reducir verbosidad de ciertos loggers
    logging.getLogger('discord').setLevel(logging.WARNING)
    logging.getLogger('discord.http').setLevel(logging.WARNING)
    logging.getLogger('discord.gateway').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)
    logging.getLogger('whisper').setLevel(logging.WARNING)
    logging.getLogger('yt_dlp').setLevel(logging.WARNING)

def log_exception(message, exception):
    """
    Registra una excepción de forma detallada
    
    Args:
        message: Mensaje descriptivo sobre el contexto del error
        exception: La excepción capturada
    """
    logger = logging.getLogger('musicbot.error')
    
    # Obtener detalles del error
    exc_type = type(exception).__name__
    exc_msg = str(exception)
    exc_traceback = ''.join(traceback.format_exception(
        type(exception), 
        exception, 
        exception.__traceback__
    ))
    
    # Registrar error detallado
    logger.error(f"{message}: {exc_type} - {exc_msg}\n{exc_traceback}")
    
    # Si es un error crítico, también imprimir en stderr
    if message.startswith("Error crítico") or message.startswith("Error fatal"):
        print(f"ERROR CRÍTICO: {message}: {exc_type} - {exc_msg}", file=sys.stderr)