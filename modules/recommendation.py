# Sistema de recomendación
import logging
import random
from collections import Counter
from utils.error_handler import log_exception
from utils.youtube_utils import search_youtube

logger = logging.getLogger('musicbot.recommendation')

class RecommendationSystem:
    def __init__(self):
        # Podría expandirse para usar un modelo de recomendación más sofisticado
        pass
        
    async def get_user_recommendations(self, user_id, history, num_recommendations=3):
        """
        Genera recomendaciones basadas en el historial del usuario
        
        Args:
            user_id: ID del usuario
            history: Lista de canciones escuchadas por el usuario
            num_recommendations: Número de recomendaciones a generar
            
        Returns:
            Lista de canciones recomendadas
        """
        try:
            if not history:
                return []
                
            recommendations = []
            
            # Estrategia 1: Basada en artistas más escuchados
            artist_counter = Counter(song['artist'] for song in history if song.get('artist'))
            
            # Obtener los artistas más escuchados
            top_artists = [artist for artist, _ in artist_counter.most_common(3)]
            
            # Estrategia 2: Basada en características de canciones (simulado)
            # En una implementación real, podrías usar características como género, tempo, etc.
            
            # Para cada artista popular, buscar una canción relacionada
            for artist in top_artists:
                if len(recommendations) >= num_recommendations:
                    break
                    
                # Evitar recomendar canciones ya escuchadas
                listened_song_ids = {song['song_id'] for song in history if 'song_id' in song}
                
                # Buscar una canción del artista
                try:
                    search_result = await search_youtube(f"best songs {artist}")
                    
                    if search_result and search_result['id'] not in listened_song_ids:
                        recommendations.append({
                            'title': search_result['title'],
                            'artist': search_result['artist'] or artist,
                            'url': search_result['url'],
                            'id': search_result['id']
                        })
                except Exception as e:
                    logger.warning(f"Error buscando recomendación para {artist}: {str(e)}")
            
            # Si no tenemos suficientes recomendaciones, añadir algunas generales
            if len(recommendations) < num_recommendations:
                # Lista de géneros populares para recomendaciones generales
                popular_genres = ["pop hits", "rock classics", "trending songs", "viral hits"]
                
                while len(recommendations) < num_recommendations:
                    try:
                        # Elegir un género aleatorio
                        query = random.choice(popular_genres)
                        search_result = await search_youtube(query)
                        
                        if search_result and search_result['id'] not in listened_song_ids:
                            recommendations.append({
                                'title': search_result['title'],
                                'artist': search_result['artist'] or "Artista Desconocido",
                                'url': search_result['url'],
                                'id': search_result['id']
                            })
                    except Exception as e:
                        logger.warning(f"Error en recomendación general: {str(e)}")
                        # Evitar bucle infinito si hay problemas
                        if len(recommendations) == 0:
                            break
            
            return recommendations
            
        except Exception as e:
            log_exception(f"Error generando recomendaciones para usuario {user_id}", e)
            return []