# Gestión de base de datos
import sqlite3
import asyncio
import logging
import os
import json
from datetime import datetime
import config
from utils.error_handler import log_exception
from tortoise import Tortoise

async def init_orm():
    await Tortoise.init(
        db_url="sqlite://db.sqlite3",  # o usa config.DATABASE_URL si defines ahí la ruta
        modules={"models": ["data.models", "modules.welcome_system", "modules.auto_messages", "modules.ticket_models", "modules.ticket_settings"]}

    )
    await Tortoise.generate_schemas()

logger = logging.getLogger('musicbot.database')

# Inicializar base de datos
def _init_db():
    """Inicializa la base de datos con las tablas necesarias"""
    # Extraer nombre de archivo desde la URL
    db_file = config.DATABASE_URL.replace('sqlite:///', '')
    
    conn = None
    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        
        # Crear tabla de usuarios
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            last_seen TIMESTAMP,
            preferences TEXT  -- JSON con preferencias
        )
        ''')
        
        # Crear tabla de historial de reproducción
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS play_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            song_id TEXT,
            title TEXT,
            artist TEXT,
            played_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
        ''')
        
        # Crear tabla de canciones en cola
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS queue_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            song_id TEXT,
            title TEXT,
            url TEXT,
            added_by INTEGER,
            position INTEGER,
            added_at TIMESTAMP
        )
        ''')
        
        # Crear índices para mejor rendimiento
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_history_user ON play_history (user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_queue_guild ON queue_items (guild_id)')
        
        conn.commit()
        logger.info("Base de datos inicializada correctamente")
        
    except Exception as e:
        log_exception("Error inicializando base de datos", e)
    finally:
        if conn:
            conn.close()

# Inicializar DB al importar el módulo
_init_db()

# Función helper para ejecutar queries en un executor
async def _execute_db(query_func):
    """
    Ejecuta una función de base de datos en un executor
    para evitar bloquear el bucle de eventos
    """
    return await asyncio.get_event_loop().run_in_executor(None, query_func)

# Funciones de acceso a datos

async def add_song_to_history(user_id, song_id, title, artist=None):
    """
    Añade una canción al historial de reproducción del usuario
    
    Args:
        user_id: ID del usuario que escuchó la canción
        song_id: ID único de la canción (generalmente ID de YouTube)
        title: Título de la canción
        artist: Artista (opcional)
        
    Returns:
        True si se añadió correctamente, False en caso contrario
    """
    def _add_to_history():
        try:
            db_file = config.DATABASE_URL.replace('sqlite:///', '')
            conn = sqlite3.connect(db_file)
            cursor = conn.cursor()
            
            # Asegurarse de que el usuario existe
            cursor.execute(
                'INSERT OR IGNORE INTO users (user_id, last_seen) VALUES (?, ?)',
                (user_id, datetime.now().isoformat())
            )
            
            # Actualizar last_seen si el usuario ya existe
            cursor.execute(
                'UPDATE users SET last_seen = ? WHERE user_id = ?',
                (datetime.now().isoformat(), user_id)
            )
            
            # Añadir entrada al historial
            cursor.execute(
                'INSERT INTO play_history (user_id, song_id, title, artist, played_at) VALUES (?, ?, ?, ?, ?)',
                (user_id, song_id, title, artist, datetime.now().isoformat())
            )
            
            conn.commit()
            return True
        except Exception as e:
            log_exception(f"Error añadiendo canción al historial para usuario {user_id}", e)
            return False
        finally:
            if conn:
                conn.close()
                
    return await _execute_db(_add_to_history)

async def get_user_history(user_id, limit=20):
    """
    Obtiene el historial de reproducción de un usuario
    
    Args:
        user_id: ID del usuario
        limit: Número máximo de entradas a obtener
        
    Returns:
        Lista de canciones reproducidas por el usuario
    """
    def _get_history():
        try:
            db_file = config.DATABASE_URL.replace('sqlite:///', '')
            conn = sqlite3.connect(db_file)
            conn.row_factory = sqlite3.Row  # Para obtener resultados como diccionarios
            cursor = conn.cursor()
            
            cursor.execute(
                '''SELECT song_id, title, artist, played_at 
                   FROM play_history 
                   WHERE user_id = ? 
                   ORDER BY played_at DESC 
                   LIMIT ?''',
                (user_id, limit)
            )
            
            history = []
            for row in cursor.fetchall():
                history.append({
                    'song_id': row['song_id'],
                    'title': row['title'],
                    'artist': row['artist'],
                    'played_at': row['played_at']
                })
                
            return history
        except Exception as e:
            log_exception(f"Error obteniendo historial para usuario {user_id}", e)
            return []
        finally:
            if conn:
                conn.close()
                
    return await _execute_db(_get_history)

async def save_queue(guild_id, queue_items):
    """
    Guarda la cola de reproducción actual en la base de datos
    
    Args:
        guild_id: ID del servidor
        queue_items: Lista de elementos en la cola
        
    Returns:
        True si se guardó correctamente, False en caso contrario
    """
    def _save_queue():
        try:
            db_file = config.DATABASE_URL.replace('sqlite:///', '')
            conn = sqlite3.connect(db_file)
            cursor = conn.cursor()
            
            # Eliminar cola anterior para este guild
            cursor.execute('DELETE FROM queue_items WHERE guild_id = ?', (guild_id,))
            
            # Insertar nuevos elementos en la cola
            for position, item in enumerate(queue_items):
                cursor.execute(
                    '''INSERT INTO queue_items 
                       (guild_id, song_id, title, url, added_by, position, added_at) 
                       VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    (
                        guild_id,
                        item.get('song_id'),
                        item.get('title'),
                        item.get('url'),
                        item.get('requester'),
                        position,
                        datetime.now().isoformat()
                    )
                )
                
            conn.commit()
            return True
        except Exception as e:
            log_exception(f"Error guardando cola para guild {guild_id}", e)
            return False
        finally:
            if conn:
                conn.close()
                
    return await _execute_db(_save_queue)

async def load_queue(guild_id):
    """
    Carga la cola guardada para un servidor
    
    Args:
        guild_id: ID del servidor
        
    Returns:
        Lista de elementos en la cola
    """
    def _load_queue():
        try:
            db_file = config.DATABASE_URL.replace('sqlite:///', '')
            conn = sqlite3.connect(db_file)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute(
                '''SELECT song_id, title, url, added_by 
                   FROM queue_items 
                   WHERE guild_id = ? 
                   ORDER BY position ASC''',
                (guild_id,)
            )
            
            queue = []
            for row in cursor.fetchall():
                queue.append({
                    'song_id': row['song_id'],
                    'title': row['title'],
                    'url': row['url'],
                    'requester': row['added_by']
                })
                
            return queue
        except Exception as e:
            log_exception(f"Error cargando cola para guild {guild_id}", e)
            return []
        finally:
            if conn:
                conn.close()
                
    return await _execute_db(_load_queue)

async def update_user_preferences(user_id, preferences):
    """
    Actualiza las preferencias de un usuario
    
    Args:
        user_id: ID del usuario
        preferences: Diccionario con preferencias
        
    Returns:
        True si se actualizó correctamente, False en caso contrario
    """
    def _update_prefs():
        try:
            db_file = config.DATABASE_URL.replace('sqlite:///', '')
            conn = sqlite3.connect(db_file)
            cursor = conn.cursor()
            
            # Convertir preferencias a JSON
            prefs_json = json.dumps(preferences)
            
            # Insertar o actualizar preferencias
            cursor.execute(
                '''INSERT OR REPLACE INTO users 
                   (user_id, preferences, last_seen) 
                   VALUES (?, ?, ?)''',
                (user_id, prefs_json, datetime.now().isoformat())
            )
            
            conn.commit()
            return True
        except Exception as e:
            log_exception(f"Error actualizando preferencias para usuario {user_id}", e)
            return False
        finally:
            if conn:
                conn.close()
                
    return await _execute_db(_update_prefs)

async def get_user_preferences(user_id):
    """
    Obtiene las preferencias de un usuario
    
    Args:
        user_id: ID del usuario
        
    Returns:
        Diccionario con las preferencias del usuario o None si no se encontró
    """
    def _get_prefs():
        try:
            db_file = config.DATABASE_URL.replace('sqlite:///', '')
            conn = sqlite3.connect(db_file)
            cursor = conn.cursor()
            
            cursor.execute(
                'SELECT preferences FROM users WHERE user_id = ?',
                (user_id,)
            )
            
            result = cursor.fetchone()
            if result and result[0]:
                return json.loads(result[0])
            return {}
        except Exception as e:
            log_exception(f"Error obteniendo preferencias para usuario {user_id}", e)
            return {}
        finally:
            if conn:
                conn.close()
                
    return await _execute_db(_get_prefs)

async def get_popular_songs(limit=10):
    """
    Obtiene las canciones más populares basadas en el historial
    
    Args:
        limit: Número máximo de canciones a obtener
        
    Returns:
        Lista de canciones populares
    """
    def _get_popular():
        try:
            db_file = config.DATABASE_URL.replace('sqlite:///', '')
            conn = sqlite3.connect(db_file)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute(
                '''SELECT song_id, title, artist, COUNT(*) as play_count 
                   FROM play_history 
                   GROUP BY song_id 
                   ORDER BY play_count DESC 
                   LIMIT ?''',
                (limit,)
            )
            
            songs = []
            for row in cursor.fetchall():
                songs.append({
                    'song_id': row['song_id'],
                    'title': row['title'],
                    'artist': row['artist'],
                    'play_count': row['play_count']
                })
                
            return songs
        except Exception as e:
            log_exception(f"Error obteniendo canciones populares", e)
            return []
        finally:
            if conn:
                conn.close()
                
    return await _execute_db(_get_popular)