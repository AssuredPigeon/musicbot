#reproductor de musica
import discord #lo usamos para el manejo de voz y audio en discord
import asyncio #para las tareas asincornas (lista de reproduccion)
import logging #para los logs
import yt_dlp #para buscar musica de Youtube
import config 
import os
from utils.error_handler import log_exception
from utils.youtube_utils import search_youtube
from utils.spotify_utils import get_spotify_token, search_spotify_track
from modules.voice_processor import capture_and_process_voice #capturamos y procesamos el audio del usuario
from data.database import add_song_to_history, get_user_history #guardamos las canciones reproducidas en el historial del usuario
from modules.recommendation import RecommendationSystem #para el motor de recomendaciones personalizadas.
import time
import shutil
import datetime
import asyncio
from pathlib import Path
from modules.voice_receiver import VoiceReceiver
from utils.cookie_manager import is_cookie_error

# Configurar logger
logger = logging.getLogger('musicbot.player')

class MusicPlayer:
    def __init__(self, bot):
        self.bot = bot
        self.voice_clients = {}  # {guild_id: voice_client}
        self.queues = {}         # {guild_id: [songs]} lista de canciones
        self.current_songs = {}  # {guild_id: current_song} cancion actual
        self.is_playing = {}     # {guild_id: bool}
        self.is_paused = {}      # {guild_id: bool} nuevo atributo para controlar pausa
        self.voice_command_mode = {}  # {guild_id: {user_id: is_listening}}
        self.recommendation_system = RecommendationSystem()
        self.voice_command_handler = None
        self.volume = {}         # {guild_id: volume_level} para controlar el volumen
        self.default_volume = 0.5

        self.file_cache = {}  # {song_id: {'path': path, 'last_accessed': timestamp, 'size': bytes}}
        self.cache_expiry_days = 7 #Son ls dias para expirar archivos de cache
        self.cache_size_limit_mb = 500 # Tamaño maximo de cache em MB

        self.voice_receivers = {} # diccionario para los receptores de voz
        self._voice_text_channels = {}  # {guild_id: channel_id} canal donde responder comandos de voz

        #self.cleanup_task = asyncio.create_task(self._periodic_cache_cleanup())

    async def _add_to_cache(self, song_id, file_path):
        """Añade un archivo al sistema de caché con su timestamp"""
        try:
            file_size = Path(file_path).stat().st_size
            self.file_cache[song_id] = {
                'path': file_path,
                'last_accessed': time.time(),
                'size': file_size
            }
            logger.info(f"Añadido a caché: {song_id}, tamaño: {file_size / (1024*1024):.2f} MB")
            
            # Verificar si el tamaño de caché supera el límite
            await self._check_cache_size()
        except Exception as e:
            log_exception(f"Error añadiendo archivo a caché: {song_id}", e)
    
    def _update_cache_access(self, song_id):
        """Actualiza el timestamp de último acceso para un archivo"""
        if song_id in self.file_cache:
            #Verificamos que exista el archivo
            file_path = self.file_cache[song_id]['path']
            if Path(file_path).exists():
                self.file_cache[song_id]['last_accessed'] = time.time()
            else:
                #si no existe lo borramos de la cache
                del self.file_cache[song_id]
                logger.warning(f"Archivo no encontrado en cache: {song_id}, eliminando registro")
    
    async def _check_cache_size(self):
        """Verifica si el tamaño de la caché excede el límite y limpia si es necesario"""
        try:
            # Calcular tamaño total actual
            total_size = sum(item['size'] for item in self.file_cache.values())
            total_size_mb = total_size / (1024 * 1024)
            
            # Si excede el límite, eliminar archivos viejos
            if total_size_mb > self.cache_size_limit_mb:
                logger.info(f"Caché excede el límite ({total_size_mb:.2f}MB > {self.cache_size_limit_mb}MB). Limpiando...")
                
                # Ordenar por fecha de último acceso (más antiguos primero)
                sorted_files = sorted(
                    self.file_cache.items(), 
                    key=lambda x: x[1]['last_accessed']
                )
                
                # Eliminar archivos hasta estar bajo el límite (80% del límite)
                target_size = self.cache_size_limit_mb * 0.8 * 1024 * 1024  # 80% del límite en bytes
                current_size = total_size
                
                for song_id, info in sorted_files:
                    if current_size <= target_size:
                        break
                        
                    file_path = info['path']
                    file_size = info['size']
                    
                    try:
                        # Eliminar archivo físico
                        if Path(file_path).exists():
                            Path(file_path).unlink()
                            
                        # Quitar de la caché
                        del self.file_cache[song_id]
                        current_size -= file_size
                        
                        logger.info(f"Eliminado de caché por límite de tamaño: {song_id}")
                    except Exception as e:
                        log_exception(f"Error eliminando archivo de caché: {file_path}", e)
        except Exception as e:
            log_exception("Error verificando tamaño de caché", e)
    
    async def start_cleanup_task(self):
        """Inicia la tarea de limpieza periódica de caché"""
        self.cleanup_task = asyncio.create_task(self._periodic_cache_cleanup())
        logger.info("Tarea de limpieza de caché iniciada")

    async def _periodic_cache_cleanup(self):
        """Tarea periódica para limpiar la caché"""
        retry_count = 0
        max_retries = 3
        try:
            while True:
                # Ejecutar limpieza cada 6 horas
                await asyncio.sleep(6 * 60 * 60)  # 6 horas en segundos
                
                try:
                    await self._cleanup_expired_files()
                    retry_count = 0
                    # Verificar y crear directorio de caché si no existe
                    Path(config.TEMP_AUDIO_DIR).mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    retry_count += 1
                    log_exception(f"Error durante limpieza (intento {retry_count}/{max_retries})", e)

                    if retry_count >= max_retries:
                        logger.error("Demasiados errores consecutivos, deteniendo tarea de limpieza")
                        break

                    await asyncio.sleep(30 * 60)
        except asyncio.CancelledError:
            logger.info("Tarea de limpieza de caché cancelada")
        except Exception as e:
            log_exception("Error crítico en tarea de limpieza de caché", e)
            # Reintentar en 30 minutos si falla
            await asyncio.sleep(30 * 60)
            self.cleanup_task = asyncio.create_task(self._periodic_cache_cleanup())

    async def _cleanup_expired_files(self):
        """Elimina archivos expirados basados en la fecha de último acceso"""
        try:
            # Calcular timestamp de expiración
            expiry_timestamp = time.time() - (self.cache_expiry_days * 24 * 60 * 60)
            
            # Contar archivos procesados para logging
            total_files = len(self.file_cache)
            removed_files = 0
            
            # Buscar archivos expirados
            expired_song_ids = []
            for song_id, info in self.file_cache.items():
                if info['last_accessed'] < expiry_timestamp:
                    expired_song_ids.append(song_id)
            
            # Eliminar archivos expirados
            for song_id in expired_song_ids:
                file_path = self.file_cache[song_id]['path']
                try:
                    if Path(file_path).exists():
                        Path(file_path).unlink()
                    
                    del self.file_cache[song_id]
                    removed_files += 1
                    
                except Exception as e:
                    log_exception(f"Error eliminando archivo expirado: {file_path}", e)
            
            # Limpiar archivos huérfanos en el directorio de caché
            await self._cleanup_orphaned_files()
            
            # Registrar resultado
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info(f"[{now}] Limpieza de caché: eliminados {removed_files} de {total_files} archivos por expiración")
            
        except Exception as e:
            log_exception("Error en limpieza de archivos expirados", e)
    
    async def _cleanup_orphaned_files(self):
        """Elimina archivos en el directorio de caché que no están en el registro de caché"""
        try:
            # Obtener todos los archivos en el directorio de caché
            cache_dir = Path(config.TEMP_AUDIO_DIR)

            cache_dir.mkdir(parents=True, exist_ok=True)
                
            # Conjunto de archivos conocidos en la cache
            known_files = set(Path(info['path']).name for info in self.file_cache.values())
            
            # Contar huerfanos eliminados
            orphans_removed = 0
            
            # Revisar todos los archivos en el directorio
            for file_path in cache_dir.glob('*'):
                if file_path.is_file() and file_path.name not in known_files:
                    try:
                        file_path.unlink()
                        orphans_removed += 1
                    except Exception as e:
                        log_exception(f"Error eliminando archivo huérfano: {file_path}", e)
            
            if orphans_removed > 0:
                logger.info(f"Limpieza de huérfanos: eliminados {orphans_removed} archivos no registrados")
                
        except Exception as e:
            log_exception("Error en limpieza de archivos huérfanos", e)
    
    # Método para limpiar la caché manualmente (para comandos administrativos)
    async def clear_cache(self, older_than_days=None):
        """Limpia la caché manualmente, opcionalmente especificando antigüedad"""
        try:
            if older_than_days is not None:
                # Limpiar archivos más antiguos que X días
                expiry_timestamp = time.time() - (older_than_days * 24 * 60 * 60)
                expired_song_ids = [
                    song_id for song_id, info in self.file_cache.items()
                    if info['last_accessed'] < expiry_timestamp
                ]
                
                for song_id in expired_song_ids:
                    file_path = self.file_cache[song_id]['path']
                    if Path(file_path).exists():
                        Path(file_path).unlink()
                    del self.file_cache[song_id]
                
                return f"Caché limpiada: eliminados {len(expired_song_ids)} archivos más antiguos que {older_than_days} días."
            else:
                # Limpiar toda la caché
                file_count = len(self.file_cache)
                
                for song_id, info in list(self.file_cache.items()):
                    file_path = info['path']
                    if Path(file_path).exists():
                        Path(file_path).unlink()
                
                self.file_cache = {}
                
                # Intentar eliminar y recrear el directorio para asegurar que esté limpio
                cache_dir = Path(config.TEMP_AUDIO_DIR)
                if cache_dir.exists():
                    shutil.rmtree(cache_dir, ignore_errors=True)
                cache_dir.mkdir(parents=True, exist_ok=True)
                
                return f"Caché limpiada completamente: eliminados {file_count} archivos."
        except Exception as e:
            log_exception("Error en limpieza manual de caché", e)
            return "Error al intentar limpiar la caché."
        
    #Asigna la funcion que se ejecutara cuando se detecte una intencion de voz
    def register_voice_command_handler(self, handler):
        """Registra un handler para procesar comandos de voz"""
        self.voice_command_handler = handler
        
    async def join_voice_channel(self, voice_channel):
        """Une el bot a un canal de voz"""
        if not voice_channel:
            return False
            
        guild_id = voice_channel.guild.id
        
        try:
            # Si ya está conectado, moverse al canal
            if guild_id in self.voice_clients and self.voice_clients[guild_id]:
                if self.voice_clients[guild_id].channel.id == voice_channel.id:
                    logger.info(f"Ya conectado al canal {voice_channel.name}")
                    return True
                else:
                    await self.voice_clients[guild_id].move_to(voice_channel)
                    logger.info(f"Se movio al canal {voice_channel.name}")
            else:
                # Conectarse al canal
                self.voice_clients[guild_id] = await voice_channel.connect(timeout=10, reconnect=True)
                logger.info(f"Conectado al canal {voice_channel.name}")
                
            # Inicializar estructuras para este guild si no existen
            if guild_id not in self.queues:
                self.queues[guild_id] = []
            if guild_id not in self.is_playing:
                self.is_playing[guild_id] = False
            if guild_id not in self.is_paused:
                self.is_paused[guild_id] = False
            if guild_id not in self.current_songs:
                self.current_songs[guild_id] = None
            if guild_id not in self.volume:
                self.volume[guild_id] = 0.5
                
            return True
        except Exception as e:
            log_exception(f"Error al unirse al canal de voz {voice_channel.name}", e)
            return False
            
    def is_connected(self, guild_id=None):
        """Verifica si el bot está conectado a un canal de voz"""
        if guild_id is None:
            return bool(self.voice_clients)
            
        return guild_id in self.voice_clients and self.voice_clients[guild_id] and self.voice_clients[guild_id].is_connected()
            
    async def play_song(self, query, user_id):
        """Busca y reproduce una canción"""
        try:
            # Encontrar a qué guild (servidor) pertenece el usuario
            member = None
            guild = None
            
            for g in self.bot.guilds:
                member = g.get_member(user_id)
                if member:
                    guild = g
                    break
                    
            if not guild:
                return "No se pudo determinar a qué servidor perteneces."
                
            guild_id = guild.id
            
            # Verificar si estamos conectados
            if not self.is_connected(guild_id):
                if not member.voice:
                    return "Necesitas estar en un canal de voz primero."
                    
                join_success = await self.join_voice_channel(member.voice.channel)
                if not join_success:
                    return "No pude unirme a tu canal de voz."

            # Buscar primero en YouTube directamente
            search_result = await search_youtube(query)
            
            # Si no se encuentra en YouTube, intentar con Spotify
            if not search_result:
                spotify_token = get_spotify_token()
                if spotify_token:
                    spotify_result = search_spotify_track(query, spotify_token)
                    if spotify_result:
                        # Usar el resultado de Spotify para buscar en YouTube
                        search_query = f"{spotify_result['name']} {spotify_result['artist']}"
                        search_result = await search_youtube(search_query)
            
            if not search_result:
                return f"No pude encontrar resultados para: {query}"
                
            url = search_result['url']
            title = search_result['title']
            song_id = search_result['id']
            artist = search_result.get('artist', 'Desconocido')
            
            # Guardar en el historial del usuario
            await add_song_to_history(user_id, song_id, title, artist)
            
            # Añadir a la cola o reproducir directamente
            if self.is_playing.get(guild_id, False) and not self.is_paused.get(guild_id, False):
                self.queues[guild_id].append({
                    'url': url,
                    'title': title,
                    'requester': user_id,
                    'song_id': song_id,
                    'artist': artist
                })
                return f"🎵 Añadido a la cola: **{title}** - {artist}"
            else:
                # Si está pausado, detener y comenzar nuevo
                if self.is_paused.get(guild_id, False):
                    self.voice_clients[guild_id].stop()
                    self.is_paused[guild_id] = False
                
                # Añadir a la cola y reproducir
                self.queues[guild_id].append({
                    'url': url,
                    'title': title,
                    'requester': user_id,
                    'song_id': song_id,
                    'artist': artist
                })
                await self._play_next(guild_id)
                return f"▶️ Reproduciendo: **{title}** - {artist}"
                
        except Exception as e:
            log_exception(f"Error al reproducir canción: {query}", e)
            return f"Ocurrió un error al intentar reproducir la canción: {str(e)}"

    async def _try_extract_info(self, url, retries=3):
        """Intenta extraer información con diferentes configuraciones"""
        exceptions = []
        
        # Opciones a probar en orden
        options_to_try = [
            # Opción 1: Configuración normal con cookies
            config.YDL_OPTIONS,
            
            # Opción 2: Sin certificados y con bypass geográfico
            {**config.YDL_OPTIONS, 'nocheckcertificate': True, 'geo_bypass': True},
            
            # Opción 3: Con formato diferente y más retries
            {**config.YDL_OPTIONS, 'format': 'bestaudio', 'retries': 10},

            # Opción 4: Con configuración mínima y formato alternativo
            {'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True, 'retries': 5}

        ]
        
        for i, options in enumerate(options_to_try[:retries]):
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    logger.info(f"Intento {i+1}/{retries} para {url}")
                    info = ydl.extract_info(url, download=False)
                    if info:
                        return info
            except Exception as e:
                exceptions.append(str(e))
                logger.warning(f"Error en intento {i+1}: {str(e)}")
                await asyncio.sleep(1)
        
        # Si llegamos aquí, todos los intentos fallaron
        all_errors = " | ".join(exceptions)

        # Detectar si es un error de cookies para dar un mensaje más útil
        if is_cookie_error(all_errors):
            raise Exception(
                f"🍪 Las cookies de YouTube han expirado o son inválidas. "
                f"Ejecuta `scripts/refresh_cookies.sh` para renovarlas. "
                f"(Error: {exceptions[-1]})"
            )

        raise Exception(f"No se pudo extraer información después de {retries} intentos: {all_errors}")

    async def _play_next(self, guild_id, retry_count=0, max_retries=3, problem_song=None):
        """Reproduce la siguiente canción en la cola usando sistema de caché"""

        #Si se intenta reproducir una cancion problematica
        if problem_song and retry_count >= max_retries:
            logger.warning(f"Cancion problematica saltada despues de {max_retries} intentos: {problem_song['title']}")
            await self._notify_error(guild_id, f"⚠️ No se pudo reproducir **{problem_song['title']}** después de varios intentos. Saltando...")
        
            #intentar con la siguiente cancion
            problem_song = None
            retry_count = 0

        # Si tenemos una canción problemática, intentamos reproducirla de nuevo
        if problem_song:
            song = problem_song
        # Si no, verificamos si hay canciones en la cola
        elif guild_id in self.queues and self.queues[guild_id]:
            # Marcar como reproduciendo
            self.is_playing[guild_id] = True
            self.is_paused[guild_id] = False
            
            # Obtener la siguiente canción
            song = self.queues[guild_id].pop(0)
            self.current_songs[guild_id] = song
        else:
            # No hay canción problemática ni canciones en la cola
            self.is_playing[guild_id] = False
            self.is_paused[guild_id] = False
            self.current_songs[guild_id] = None
            return
            
        logger.info(f"Intentando reproducir audio: {song['title']}")
        
        # Preparar y reproducir el audio
        try:
            # Verificar si existe en caché
            song_id = song['song_id']
            cached_file = None
            use_cache = True  # Cambiar a False para deshabilitar caché
            
            if use_cache and song_id in self.file_cache:
                cached_path = self.file_cache[song_id]['path']
                if Path(cached_path).exists():
                    # Actualizar timestamp de último acceso
                    self._update_cache_access(song_id)
                    cached_file = cached_path
                    logger.info(f"Usando archivo en caché para {song['title']}")
            
            if cached_file:
                # Usar archivo de caché
                audio_source = discord.FFmpegPCMAudio(cached_file)
                logger.info("Audio source creado correctamente")
            else:
                # Preparar opciones de yt-dlp
                cookies_path = config.COOKIES_PATH
                custom_options = config.YDL_OPTIONS.copy()
                
                if os.path.exists(cookies_path) and os.path.getsize(cookies_path) > 0:
                    custom_options['cookiefile'] = cookies_path
                    
                # Descargar o streaming según configuración
                use_local_file = True  # Cambiar a False para usar siempre streaming
                
                if use_local_file and use_cache:
                    # Verificar/crear directorio de caché
                    cache_dir = Path(config.TEMP_AUDIO_DIR)
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    
                    # Establecer ruta para el archivo descargado
                    download_path = os.path.join(config.TEMP_AUDIO_DIR, f"{song_id}.mp3")
                    
                    # Descargar el archivo
                    logger.info(f"Descargando {song['title']} a caché...")

                    try:
                        # Asegurar que el directorio de caché existe
                        os.makedirs(config.TEMP_AUDIO_DIR, exist_ok=True)
                        
                        # Configurar la ruta de salida directamente
                        custom_options['outtmpl'] = os.path.join(config.TEMP_AUDIO_DIR, f"{song_id}.%(ext)s")
                        
                        with yt_dlp.YoutubeDL(custom_options) as ydl:
                            info = ydl.extract_info(song['url'], download=True)
                            if not info:
                                raise Exception("No se pudo obtener información del video")
                            
                            # Obtener la ruta real del archivo descargado
                            real_file = None
                            
                            # Método 1: Verificar en requested_downloads
                            if 'requested_downloads' in info and info['requested_downloads']:
                                real_file = info['requested_downloads'][0].get('filepath')
                            
                            # Método 2: Usar extensión e ID para encontrar el archivo
                            if not real_file or not os.path.exists(real_file):
                                real_ext = info.get('ext', 'mp3')
                                real_file = os.path.join(config.TEMP_AUDIO_DIR, f"{song_id}.{real_ext}")
                            
                            # Método 3: Usar el prepare_filename como último recurso
                            if not os.path.exists(real_file):
                                real_file = ydl.prepare_filename(info)
                            
                            # Verificar si el archivo existe y tiene un tamaño válido
                            if os.path.exists(real_file):
                                file_size = os.path.getsize(real_file)
                                if file_size < 1024:  # Menos de 1KB probablemente está corrupto
                                    raise Exception(f"El archivo descargado parece estar corrupto (tamaño: {file_size} bytes)")
                                
                                # Actualizar la ruta de descarga con la ruta real
                                download_path = real_file
                                logger.info(f"Archivo descargado correctamente: {download_path} ({file_size} bytes)")
                            else:
                                raise Exception(f"No se pudo encontrar el archivo descargado. Intentó: {real_file}")
                                
                    except Exception as e:
                        logger.error(f"Error durante la descarga: {str(e)}")
                        raise

                    if os.path.exists(download_path):
                        # Añadir a caché
                        await self._add_to_cache(song_id, download_path)
                    else:
                        logger.warning(f"No se puede añadir a caché, el archivo no existe: {download_path}")
                        # Lanzar excepción para que se maneje como un error de reproducción
                        raise Exception("No se pudo encontrar el archivo descargado para añadir a la caché")
                    # Usar archivo descargado
                    audio_source = discord.FFmpegPCMAudio(download_path)
                else:
                    # Usar streaming directo
                    with yt_dlp.YoutubeDL(custom_options) as ydl:
                        info = ydl.extract_info(song['url'], download=False)
                        audio_url = info['url']
                    
                    FFMPEG_OPTIONS = {
                    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                    'options': '-vn -af "volume=0.5"'
                    }

                    # Crear fuente de audio para streaming
                    audio_source = discord.FFmpegPCMAudio(audio_url, **FFMPEG_OPTIONS)
                    
            
            # Aplicar volumen
            current_volume = self.volume.get(guild_id, 0.5)
            audio_source = discord.PCMVolumeTransformer(audio_source, volume=current_volume)
            
            # Reproducir el audio
            loop = asyncio._get_running_loop()
            self.voice_clients[guild_id].play(
                audio_source,
                after=lambda e: asyncio.run_coroutine_threadsafe(
                    self._song_finished(e, guild_id), 
                    loop
                )
            )
            logger.info("Reproductor de audio iniciado")
            logger.info(f"Reproduciendo en {guild_id}: {song['title']}")
            
        except Exception as e:
            log_exception(f"Error reproduciendo {song['title']} en {guild_id} (intento {retry_count+1}/{max_retries})", e)

            # Volver a intentar la misma cancion pero con diferentes opciones
            if retry_count < max_retries:
                logger.info(f"Reintentando reproducir {song['title']} (intento {retry_count+1}/{max_retries})")
                logger.debug(f"Error: {str(e)}")  # Si quieres registrar la excepción en un nivel de debug
                await asyncio.sleep(2) #una pequeña pausa antes de reintentar
                await self._play_next(guild_id, retry_count + 1, max_retries, song)
            else:
                # Notificar al canal de texto sobre el error si es posible
                await self._notify_error(guild_id, f"Error al reproducir {song['title']}: {str(e)}")
                # Intentar con la siguiente canción
                await self._play_next(guild_id)

    async def cleanup(self):
        """Limpia recursos cuando se cierra el bot"""
        # Cancelar tarea de limpieza periódica
        if hasattr(self, 'cleanup_task') and self.cleanup_task:
            self.cleanup_task.cancel()
            
        # Desconectar de todos los canales de voz
        for guild_id, voice_client in list(self.voice_clients.items()):
            if voice_client and voice_client.is_connected():
                await voice_client.disconnect()
                
        # Notificar limpieza
        logger.info("Recursos del reproductor de música liberados")
    
    async def _notify_error(self, guild_id, message):
        """Intenta notificar un error en un canal de texto apropiado"""
        try:
            guild = self.bot.get_guild(guild_id)
            if guild:
                # Tratar de encontrar un canal general o el primer canal de texto
                text_channels = guild.text_channels
                target_channel = None
                
                for channel in text_channels:
                    if channel.name.lower() in ["general", "música", "music", "bot"]:
                        target_channel = channel
                        break
                
                if not target_channel and text_channels:
                    target_channel = text_channels[0]
                    
                if target_channel:
                    await target_channel.send(f"❌ {message}")
        except Exception as e:
            log_exception("Error notificando error", e)
    
    async def _song_finished(self, error, guild_id):
        """Callback que se ejecuta cuando una canción termina"""
        if error:
            log_exception(f"Error durante la reproducción en {guild_id}", error)
            await self._notify_error(guild_id, f"Error durante la reproducción: {str(error)}")
            
        # Reproducir siguiente canción
        await self._play_next(guild_id)
    
    async def skip(self, guild_id=None):
        """Salta a la siguiente canción"""
        # Si no se especifica guild_id, intentar obtenerlo del primer voice_client
        if guild_id is None:
            if not self.voice_clients:
                return "No estoy conectado a ningún canal de voz."
            guild_id = list(self.voice_clients.keys())[0]
            
        if guild_id not in self.voice_clients or not self.voice_clients[guild_id]:
            return "No estoy conectado a un canal de voz."
            
        if self.voice_clients[guild_id].is_playing() or self.is_paused.get(guild_id, False):
            self.voice_clients[guild_id].stop()
            self.is_paused[guild_id] = False
            return "⏭️ Saltando a la siguiente canción..."
        else:
            return "No hay nada reproduciéndose actualmente."
    
    async def pause(self, guild_id=None):
        """Pausa la reproducción actual"""
        if guild_id is None:
            if not self.voice_clients:
                return "No estoy conectado a ningún canal de voz."
            guild_id = list(self.voice_clients.keys())[0]
            
        if guild_id not in self.voice_clients or not self.voice_clients[guild_id]:
            return "No estoy conectado a un canal de voz."
            
        if self.is_playing.get(guild_id, False) and not self.is_paused.get(guild_id, False):
            if self.voice_clients[guild_id].is_playing():
                self.voice_clients[guild_id].pause()
                self.is_paused[guild_id] = True
                return "⏸️ Reproducción pausada."
        elif self.is_paused.get(guild_id, False):
            return "La reproducción ya está pausada."
        else:
            return "No hay nada reproduciéndose actualmente."
    
    async def resume(self, guild_id=None):
        """Reanuda la reproducción"""
        if guild_id is None:
            if not self.voice_clients:
                return "No estoy conectado a ningún canal de voz."
            guild_id = list(self.voice_clients.keys())[0]
            
        if guild_id not in self.voice_clients or not self.voice_clients[guild_id]:
            return "No estoy conectado a un canal de voz."
            
        if self.is_paused.get(guild_id, False):
            self.voice_clients[guild_id].resume()
            self.is_paused[guild_id] = False
            return "▶️ Reproducción reanudada."
        else:
            return "La reproducción no está pausada."
    
    async def set_volume(self, volume, guild_id=None):
        """Ajusta el volumen de reproducción (0.0 a 1.0)"""
        if guild_id is None:
            if not self.voice_clients:
                return "No estoy conectado a ningún canal de voz."
            guild_id = list(self.voice_clients.keys())[0]
            
        if guild_id not in self.voice_clients or not self.voice_clients[guild_id]:
            return "No estoy conectado a un canal de voz."
            
        # Validar volumen
        try:
            volume = float(volume)
            if volume < 0.0:
                volume = 0.0
            elif volume > 1.0:
                volume = 1.0
        except ValueError:
            return "El volumen debe ser un número entre 0.0 y 1.0."
            
        # Guardar volumen nuevo
        self.volume[guild_id] = volume
        
        # Aplicar a la canción actual si está reproduciéndose
        if self.is_playing.get(guild_id, False) and self.voice_clients[guild_id].source:
            self.voice_clients[guild_id].source.volume = volume
            
        return f"🔊 Volumen ajustado a {int(volume * 100)}%."
    
    async def stop(self, guild_id=None):
        """Detiene la reproducción y desconecta del canal de voz"""
        # Si no se especifica guild_id, intentar obtenerlo del primer voice_client
        if guild_id is None:
            if not self.voice_clients:
                return "No estoy conectado a ningún canal de voz."
            guild_id = list(self.voice_clients.keys())[0]
            
        if guild_id not in self.voice_clients or not self.voice_clients[guild_id]:
            return "No estoy conectado a un canal de voz."
            
        # Limpiar cola y detener reproducción
        self.queues[guild_id] = []
        if self.voice_clients[guild_id].is_playing() or self.is_paused.get(guild_id, False):
            self.voice_clients[guild_id].stop()
            
        # Desconectar
        await self.voice_clients[guild_id].disconnect()
        del self.voice_clients[guild_id]
        self.is_playing[guild_id] = False
        self.is_paused[guild_id] = False
        self.current_songs[guild_id] = None
        
        # Desactivar modo de escucha de voz si está activo
        if guild_id in self.voice_command_mode:
            self.voice_command_mode[guild_id] = {}
            
        return "⏹️ Reproducción detenida y desconectado del canal de voz."
    
    async def get_queue(self, guild_id=None):
        """Muestra la cola de reproducción actual"""
        if guild_id is None:
            if not self.voice_clients:
                return "No estoy conectado a ningún canal de voz."
            guild_id = list(self.voice_clients.keys())[0]
            
        if guild_id not in self.queues or not self.queues[guild_id]:
            return "No hay canciones en la cola."
            
        # Construir mensaje con la cola
        queue_msg = "📋 **Cola de reproducción:**\n"
        
        # Mostrar canción actual
        if self.current_songs.get(guild_id):
            song = self.current_songs[guild_id]
            status = "⏸️ Pausado:" if self.is_paused.get(guild_id, False) else "▶️ Reproduciendo:"
            queue_msg += f"{status} **{song['title']}** - {song.get('artist', 'Desconocido')}\n\n"
        
        # Mostrar cola
        for i, song in enumerate(self.queues[guild_id], 1):
            queue_msg += f"{i}. **{song['title']}** - {song.get('artist', 'Desconocido')}\n"
            
        return queue_msg
    
    async def get_recommendations(self, user_id, num_recommendations=3):
        """Obtiene recomendaciones para un usuario"""
        try:
            # Obtener historial del usuario
            history = await get_user_history(user_id)
            
            if not history or len(history) < 3:
                return "Necesito más canciones en tu historial para generar recomendaciones personalizadas."
                
            # Obtener recomendaciones
            recommendations = await self.recommendation_system.get_user_recommendations(
                user_id, 
                history, 
                num_recommendations
            )
            
            if not recommendations:
                return "No pude generar recomendaciones en este momento. Inténtalo más tarde."
                
            # Formatear resultado
            result = "🎵 **Recomendaciones basadas en tu historial:**\n"
            for i, song in enumerate(recommendations, 1):
                result += f"{i}. **{song['title']}** - {song['artist']}\n"
                
            return result
            
        except Exception as e:
            log_exception(f"Error obteniendo recomendaciones para {user_id}", e)
            return "Ocurrió un error al intentar obtener recomendaciones."
    
    async def start_voice_command_mode(self, user_id):
        """Activa el modo de escucha para comandos de voz"""
        # Encontrar a qué guild pertenece el usuario
        guild = None
        member = None
        
        for g in self.bot.guilds:
            m = g.get_member(user_id)
            if m:
                guild = g
                member = m
                break

                
        if not guild or not member:
            return "No se pudo determinar a qué servidor perteneces."
            
        guild_id = guild.id

        # Verificar si el usuario está en un canal de voz
        if not member.voice or not member.voice.channel:
            return "Necesitas estar en un canal de voz para usar comandos de voz."
        
        # Verificar si estamos conectados a un canal de voz
        if not self.is_connected(guild_id):
            join_success = await self.join_voice_channel(member.voice.channel)
            if not join_success:
                return "No pude unirme a tu canal de voz."

        #Crear un receptor de voz si no existe (o recrearlo con el voice_client actual)
        voice_client = self.voice_clients.get(guild_id)
        if guild_id not in self.voice_receivers or self.voice_receivers[guild_id].voice_client is None:
            self.voice_receivers[guild_id] = VoiceReceiver(voice_client=voice_client, sample_rate=48000)
            await self.voice_receivers[guild_id].setup_receiver()
        elif voice_client and self.voice_receivers[guild_id].voice_client != voice_client:
            # Voice client cambió (reconexión), actualizar y reinstalar el sink
            await self.voice_receivers[guild_id].cleanup()
            self.voice_receivers[guild_id].voice_client = voice_client
            await self.voice_receivers[guild_id].setup_receiver()
            
        # Inicializar el diccionario de modo de voz para este guild si no existe
        if guild_id not in self.voice_command_mode:
            self.voice_command_mode[guild_id] = {}
            
        # Activar escucha para este usuario
        self.voice_command_mode[guild_id][user_id] = True
        
        # Iniciar un task para capturar y procesar voz
        asyncio.create_task(self._voice_command_loop(guild_id, user_id))
        
        return "🎤 Modo de comandos por voz activado. Puedes hablarme directamente."
    


    async def stop_voice_command_mode(self, guild_id=None, user_id=None):
        """Desactiva el modo de escucha para comandos de voz"""

        # Primero detener la grabación si está activa
        try:
            if guild_id is not None and user_id is not None:
                if guild_id in self.voice_receivers:
                    await self.voice_receivers[guild_id].stop_recording(user_id)
            elif guild_id is not None:
                if guild_id in self.voice_receivers:
                    for user_id in self.voice_command_mode.get(guild_id, {}).keys():
                        await self.voice_receivers[guild_id].stop_recording(user_id)
            elif user_id is not None:
                for gid in self.voice_command_mode:
                    if user_id in self.voice_command_mode[gid] and gid in self.voice_receivers:
                        await self.voice_receivers[gid].stop_recording(user_id)
        except Exception as e:
            log_exception("Error al detener grabaciones", e)

        # Si no se especifican parámetros, desactivar para todos
        if guild_id is None and user_id is None:
            for gid in self.voice_command_mode:
                self.voice_command_mode[gid] = {}
            return "Modo de comandos por voz desactivado para todos los servidores."
            
        # Si solo se especifica user_id, buscarlo en todos los guilds
        if guild_id is None and user_id is not None:
            found = False
            for gid in self.voice_command_mode:
                if user_id in self.voice_command_mode[gid]:
                    self.voice_command_mode[gid][user_id] = False
                    found = True
            
            if found:
                return "🎤 Modo de comandos por voz desactivado."
            else:
                return "No estabas en modo de comandos por voz."
            
        # Desactivar para un guild específico
        if user_id is None:
            if guild_id in self.voice_command_mode:
                self.voice_command_mode[guild_id] = {}
                return "Modo de comandos por voz desactivado para todo el servidor."
            return "El servidor no tenía activado el modo de comandos por voz."
            
        # Desactivar para un usuario específico
        if guild_id in self.voice_command_mode and user_id in self.voice_command_mode[guild_id]:
            self.voice_command_mode[guild_id][user_id] = False
            return "🎤 Modo de comandos por voz desactivado."
            
        return "No estabas en modo de comandos por voz."
    
    async def _voice_command_loop(self, guild_id, user_id):
        """Loop para capturar y procesar comandos de voz"""
        logger.info(f"Iniciando loop de captura de voz para usuario {user_id} en guild {guild_id}")
        
        try:
            # Asegurarse de que el VoiceReceiver tenga el voice_client real
            voice_client = self.voice_clients.get(guild_id)
            if guild_id not in self.voice_receivers or self.voice_receivers[guild_id].voice_client is None:
                self.voice_receivers[guild_id] = VoiceReceiver(voice_client=voice_client, sample_rate=48000)
                await self.voice_receivers[guild_id].setup_receiver()

            voice_receiver = self.voice_receivers[guild_id]

            # Iniciar grabacion
            await voice_receiver.start_recording(user_id)

            # Continuar mientras el modo de escucha esté activo para este usuario
            while (guild_id in self.voice_command_mode and 
                   user_id in self.voice_command_mode.get(guild_id, {}) and 
                   self.voice_command_mode[guild_id].get(user_id, False)):
                
                # Verificar si el usuario sigue en el canal de voz
                guild = self.bot.get_guild(guild_id)
                if not guild:
                    break
                    
                member = guild.get_member(user_id)
                if not member or not member.voice or not member.voice.channel:
                    logger.info(f"Usuario {user_id} ya no está en un canal de voz. Desactivando modo de voz.")
                    self.voice_command_mode[guild_id][user_id] = False
                    break
                
                # Capturar y procesar voz
                result = await capture_and_process_voice(user_id, guild_id, voice_receiver)
                
                if result:
                    transcription, intent, entities = result
                    logger.info(f"Comando de voz detectado: {transcription} (intent: {intent})")
                    
                    # Detectar comando para detener la escucha (intent stop con "escucha" o "stop" con "escucha")
                    if intent in ("stop", "detener") and (
                        "escucha" in transcription.lower() or 
                        "listening" in transcription.lower()
                    ):
                        self.voice_command_mode[guild_id][user_id] = False
                        await self._notify_user(guild_id, user_id, "🎤 Modo de comandos por voz desactivado.")
                        break
                        
                    # Procesar el comando de voz via VoiceCommandHandler
                    if self.voice_command_handler:
                        response = await self.voice_command_handler.handle_voice_command(
                            user_id, guild_id, intent, entities, transcription
                        )
                        if response:
                            await self._notify_user(guild_id, user_id, response)
                    else:
                        # Fallback si no hay handler registrado
                        await self._process_basic_voice_command(guild_id, user_id, intent, entities)
                
                # Pequeña pausa para no sobrecargar la CPU
                await asyncio.sleep(0.1)

            #detener la grabacion cuando termine el loop
            await voice_receiver.stop_recording(user_id)
                
        except Exception as e:
            log_exception(f"Error en loop de captura de voz para {user_id}", e)
            # Desactivar modo de escucha en caso de error
            if guild_id in self.voice_command_mode and user_id in self.voice_command_mode[guild_id]:
                self.voice_command_mode[guild_id][user_id] = False
                
            #nos aseguramos de detener la grabacion si hay error
            if guild_id in self.voice_receivers:
                await self.voice_receivers[guild_id].stop_recording(user_id)

            # Notificar al usuario sobre el error
            await self._notify_user(
                guild_id, 
                user_id, 
                "❌ Se produjo un error en el modo de comandos por voz. Intenta activarlo nuevamente."
            )
    
    # Nuevo método para procesar comandos básicos de voz
    async def _process_basic_voice_command(self, guild_id, user_id, intent, entities):
        """
        Fallback para procesar comandos de voz directamente sin VoiceCommandHandler.
        Usa los mismos nombres de intent que IntentProcessor (play_song, pause, etc.)
        """
        try:
            response = None
            
            if intent == "play_song":
                song_entity = next((e for e in entities if e["type"] == "song"), None)
                query = song_entity["value"] if song_entity else ""
                if query:
                    response = await self.play_song(query, user_id)
                    
            elif intent == "pause":
                response = await self.pause(guild_id)
                
            elif intent == "resume":
                response = await self.resume(guild_id)
                
            elif intent == "skip":
                response = await self.skip(guild_id)
                
            elif intent == "stop":
                response = await self.stop(guild_id)

            elif intent == "volume":
                vol_entity = next((e for e in entities if e["type"] == "volume_level"), None)
                if vol_entity:
                    volume_level = max(0, min(100, int(vol_entity["value"]))) / 100.0
                    response = await self.set_volume(volume_level, guild_id)

            elif intent == "queue":
                response = await self.get_queue(guild_id)

            elif intent == "help":
                response = (
                    "🎙️ **Comandos de voz:** reproduce, pausa, continúa, salta, "
                    "detén la música, volumen [0-100], muestra la cola."
                )
            
            if response:
                await self._notify_user(guild_id, user_id, response)
                
        except Exception as e:
            log_exception(f"Error procesando comando de voz básico: {intent}", e)

    def set_voice_text_channel(self, guild_id: int, channel_id: int):
        """Almacena el canal de texto donde se activó el modo voz para responder ahí."""
        self._voice_text_channels[guild_id] = channel_id

    async def _notify_user(self, guild_id: int, user_id: int, message: str):
        """Notifica al usuario en el canal de texto donde se activó el modo voz."""
        try:
            guild = self.bot.get_guild(guild_id)
            if not guild:
                return

            # 1ª opción: canal guardado cuando el usuario ejecutó !listen / /listen
            channel_id = self._voice_text_channels.get(guild_id)
            if channel_id:
                channel = guild.get_channel(channel_id)
                if channel:
                    await channel.send(f"<@{user_id}> {message}")
                    return

            # Fallback: primer canal donde el usuario tenga permiso de escritura
            member = guild.get_member(user_id)
            if member:
                for channel in guild.text_channels:
                    perms = channel.permissions_for(member)
                    if perms.read_messages and perms.send_messages:
                        await channel.send(f"<@{user_id}> {message}")
                        return

        except Exception as e:
            log_exception(f"Error notificando al usuario {user_id}", e)