import requests
import base64
import time
import logging
import config
from utils.error_handler import log_exception

# Cache para el token
_spotify_token = None
_token_expiry = 0

def get_spotify_token():
    """
    Obtiene un token de acceso para la API de Spotify.
    Utiliza el cache para evitar solicitudes innecesarias.
    """
    global _spotify_token, _token_expiry
    
    # Si tenemos un token y no ha expirado, usarlo
    current_time = time.time()
    if _spotify_token and current_time < _token_expiry:
        return _spotify_token
        
    try:
        # Preparar la autenticación
        auth_str = f"{config.SPOTIFY_CLIENT_ID}:{config.SPOTIFY_CLIENT_SECRET}"
        b64_auth = base64.b64encode(auth_str.encode()).decode()
        
        headers = {
            "Authorization": f"Basic {b64_auth}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        data = {"grant_type": "client_credentials"}
        
        # Hacer la solicitud
        response = requests.post(
            "https://accounts.spotify.com/api/token", 
            headers=headers, 
            data=data,
            timeout=10
        )
        
        # Verificar si la solicitud fue exitosa
        response.raise_for_status()
        
        # Extraer token y tiempo de expiración
        token_data = response.json()
        _spotify_token = token_data.get("access_token")
        
        # Guardar la expiración (típicamente 3600 segundos = 1 hora)
        # Restamos 300 segundos (5 minutos) para renovar antes de que expire
        expires_in = token_data.get("expires_in", 3600) - 300
        _token_expiry = current_time + expires_in
        
        return _spotify_token
        
    except requests.RequestException as e:
        log_exception("Error al obtener token de Spotify", e)
        return None
    except Exception as e:
        log_exception("Error inesperado obteniendo token de Spotify", e)
        return None
    
def search_spotify_track(query, token=None):
    """
    Busca una canción en Spotify.
    Devuelve un diccionario con información sobre la canción encontrada.
    """
    if not token:
        token = get_spotify_token()
        if not token:
            return None
            
    try:
        headers = {"Authorization": f"Bearer {token}"}
        params = {
            "q": query, 
            "type": "track", 
            "limit": 1,
            "market": "ES"  # Puedes ajustar esto según tu mercado objetivo
        }
        
        response = requests.get(
            "https://api.spotify.com/v1/search", 
            headers=headers, 
            params=params,
            timeout=10
        )
        
        # Verificar si la solicitud fue exitosa
        response.raise_for_status()
        
        data = response.json()
        items = data.get("tracks", {}).get("items", [])
        
        if not items:
            return None
            
        track = items[0]
        
        # Extraer información relevante
        result = {
            "name": track["name"],
            "artist": track["artists"][0]["name"],
            "album": track["album"]["name"],
            "release_date": track["album"].get("release_date"),
            "duration_ms": track["duration_ms"],
            "popularity": track["popularity"],
            "spotify_id": track["id"],
            "preview_url": track.get("preview_url"),
            "image_url": track["album"]["images"][0]["url"] if track["album"]["images"] else None
        }
        
        # Si hay más artistas, agregarlos
        if len(track["artists"]) > 1:
            result["all_artists"] = [artist["name"] for artist in track["artists"]]
            
        return result
        
    except requests.RequestException as e:
        log_exception(f"Error en búsqueda de Spotify para '{query}'", e)
        return None
    except Exception as e:
        log_exception(f"Error inesperado buscando '{query}' en Spotify", e)
        return None
    