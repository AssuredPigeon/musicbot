#configuracion y variables globales
import os #para leer variables de entorono, crear carpetas, etc
from dotenv import load_dotenv

#Ubicacion absoluta para cookies.txt
COOKIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')
# Cargar variables de entorno desde .env si existe (ayuda a ocultar informacion sensible)
load_dotenv()

# Configuración del Bot
BOT_TOKEN = os.getenv('BOT_TOKEN')
BOT_PREFIX = os.getenv('BOT_PREFIX', '!')
BOT_DESCRIPTION = "Bot de música con reconocimiento de voz"

#Configuracion de spotify
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

# Validar que las variables críticas estén definidas
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN no está definido. Crea un archivo .env con tus credenciales (mira .env.example).")
if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
    raise ValueError("❌ SPOTIFY_CLIENT_ID o SPOTIFY_CLIENT_SECRET no están definidos en el archivo .env.")

OWNER_ID = [517134072980439042, 374758259904610304]

# ─── OpenAI Whisper API (opcional, desactivada por defecto) ───────────────────
# Pon WHISPER_USE_API=true en .env para usar la API de pago en lugar de Whisper local.
# Si está en false, se usa el modelo local (gratis, sin internet).
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')   # Vacío = API desactivada
WHISPER_USE_API = os.getenv('WHISPER_USE_API', 'false').lower() == 'true'

# Configuraciones para Vosk
VOSK_MODEL_PATH = "models/vosk-model-es-0.42"
VOSK_SAMPLE_RATE = 16000  # Vosk funciona mejor con 16kHz
VOSK_ENABLED = False  # Habilitar/deshabilitar el uso de Vosk

# Palabras clave para activar el procesamiento completo con Whisper
TRIGGER_KEYWORDS = ["reproduce", "play", "pon", "canción", "música", "song", "escuchar", "tocar"]

# Umbral de confianza para Vosk (0.0 - 1.0)
VOSK_CONFIDENCE_THRESHOLD = 0.5

# Configuración de audio
AUDIO_SAMPLE_RATE = 16000 #frecuencia de muestreo del audio (16 kHz)
AUDIO_CHANNELS = 1
WHISPER_MODEL_SIZE = "base"  # tiny, base, small, medium, large (es el tamaño del modelo para laa transcripcion)
TEMP_AUDIO_DIR = "temp_audio" #la carpeta donde se almacenaran los audios temporales para el procesamiento

# Configuración de base de datos
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///musicbot.db') #direccion de la base de datos

# Límites y timeouts
SEARCH_TIMEOUT = 10  # Segundos para timeout en búsquedas de YT (tiempo maximo para buscar una cancion antes de fallar)
MAX_QUEUE_SIZE = 100  # Máximo número de canciones en cola
YDL_OPTIONS = { 
    'format': 'bestaudio/best',  #buscamos el mejor formato de audio disponible
    'quiet': True,
    'no_warnings': True,
    'extract_flat': 'in_playlist', #evitamos la descarga de videos de listas completas
    'cookiefile': COOKIES_PATH,  # Ruta absoluta
    'postprocessors': [{   #utilizamos FFmeg para convertir el audio a .mp3 de 192 kbps
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }],
}

# Crear directorio temporal si no existe
os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)