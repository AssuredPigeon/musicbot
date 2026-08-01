# Funciones para interactuar con YouTube
import asyncio
import re
import logging
import yt_dlp
import config
from utils.error_handler import log_exception

logger = logging.getLogger('musicbot.youtube')

async def search_youtube(query, max_results=1):
    """
    Busca una canción en YouTube
    
    Args:
        query: Consulta de búsqueda
        max_results: Número máximo de resultados
        
    Returns:
        Diccionario con información de la canción o None si hay error
    """
    try:
        # Opciones para YoutubeDL
        ydl_opts = {
            'format': 'bestaudio',
            'quiet': True,
            'no_warnings': True,
            'default_search': 'ytsearch', 
            'extract_flat': True,
            'max_downloads': max_results,
            'skip_download': True,
            'cookiefile': config.COOKIES_PATH
        }
        
        # Buscar con límite de tiempo para evitar bloqueos
        async def search():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                result = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
                if not result or 'entries' not in result:
                    return None
                return result['entries'][:max_results]
                
        # Ejecutar búsqueda con timeout
        try:
            search_results = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, lambda: asyncio.run(search())),
                timeout=config.SEARCH_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning(f"Timeout en búsqueda de YouTube: {query}")
            return None
        
        if not search_results:
            return None
            
        # Procesar el primer resultado
        first_result = search_results[0]
        
        # Si no tenemos suficiente información, obtener detalles completos
        if 'id' not in first_result or 'title' not in first_result:
            return None
            
        # Extraer información básica
        video_id = first_result['id']
        title = first_result['title']
        
        # Intentar extraer el artista del título (patrón común: "Título - Artista")
        artist = None
        title_parts = title.split(' - ')
        if len(title_parts) > 1:
            artist = title_parts[1].strip()
            title = title_parts[0].strip()
        
        # Construir URL
        url = f"https://www.youtube.com/watch?v={video_id}"
        
        return {
            'url': url,
            'id': video_id,
            'title': title,
            'artist': artist
        }
        
    except Exception as e:
        log_exception(f"Error buscando en YouTube: {query}", e)
        return None

async def extract_youtube_info(url):
    """
    Extrae información detallada de un video de YouTube
    
    Args:
        url: URL del video
        
    Returns:
        Diccionario con información o None si hay error
    """
    try:
        ydl_opts = {
            'format': 'bestaudio',
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'cookiefile': config.COOKIES_PATH
        }
        
        # Extraer información con límite de tiempo
        async def extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
                
        info = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, lambda: asyncio.run(extract())),
            timeout=config.SEARCH_TIMEOUT
        )
        
        if not info:
            return None
            
        # Extraer datos relevantes
        result = {
            'id': info.get('id'),
            'title': info.get('title'),
            'url': info.get('webpage_url', url),
            'duration': info.get('duration'),
            'thumbnail': info.get('thumbnail'),
        }
        
        # Intentar extraer el artista
        artist = None
        if 'artist' in info:
            artist = info['artist']
        elif 'uploader' in info:
            artist = info['uploader']
        elif 'title' in info:
            # Intentar extraer del título
            title_parts = info['title'].split(' - ')
            if len(title_parts) > 1:
                artist = title_parts[1].strip()
                result['title'] = title_parts[0].strip()
        
        result['artist'] = artist
        return result
        
    except Exception as e:
        log_exception(f"Error extrayendo información de YouTube: {url}", e)
        return None