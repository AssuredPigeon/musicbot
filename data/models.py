# Modelos de datos
import json
from datetime import datetime

class User:
    """Modelo para representar un usuario en la base de datos"""
    
    def __init__(self, user_id, username=None, last_seen=None, preferences=None):
        self.user_id = user_id
        self.username = username
        self.last_seen = last_seen or datetime.now()
        self.preferences = preferences or {}
        
    @classmethod
    def from_db_row(cls, row):
        """Crea una instancia de User desde una fila de la base de datos"""
        if not row:
            return None
            
        preferences = {}
        if row['preferences']:
            try:
                preferences = json.loads(row['preferences'])
            except json.JSONDecodeError:
                pass
                
        return cls(
            user_id=row['user_id'],
            username=row['username'],
            last_seen=datetime.fromisoformat(row['last_seen']) if row['last_seen'] else None,
            preferences=preferences
        )
        
    def to_dict(self):
        """Convierte la instancia a un diccionario"""
        return {
            'user_id': self.user_id,
            'username': self.username,
            'last_seen': self.last_seen.isoformat() if self.last_seen else None,
            'preferences': self.preferences
        }
        
    def update_preference(self, key, value):
        """Actualiza una preferencia específica"""
        if self.preferences is None:
            self.preferences = {}
        self.preferences[key] = value
        
    def get_preference(self, key, default=None):
        """Obtiene una preferencia específica"""
        if not self.preferences:
            return default
        return self.preferences.get(key, default)


class PlayHistoryItem:
    """Modelo para representar un elemento del historial de reproducción"""
    
    def __init__(self, id=None, user_id=None, song_id=None, title=None, artist=None, played_at=None):
        self.id = id
        self.user_id = user_id
        self.song_id = song_id
        self.title = title
        self.artist = artist
        self.played_at = played_at or datetime.now()
        
    @classmethod
    def from_db_row(cls, row):
        """Crea una instancia desde una fila de la base de datos"""
        if not row:
            return None
            
        return cls(
            id=row['id'] if 'id' in row else None,
            user_id=row['user_id'],
            song_id=row['song_id'],
            title=row['title'],
            artist=row['artist'],
            played_at=datetime.fromisoformat(row['played_at']) if row['played_at'] else None
        )
        
    def to_dict(self):
        """Convierte la instancia a un diccionario"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'song_id': self.song_id,
            'title': self.title,
            'artist': self.artist,
            'played_at': self.played_at.isoformat() if self.played_at else None
        }


class QueueItem:
    """Modelo para representar un elemento en la cola de reproducción"""
    
    def __init__(self, id=None, guild_id=None, song_id=None, title=None, 
                 url=None, added_by=None, position=None, added_at=None):
        self.id = id
        self.guild_id = guild_id
        self.song_id = song_id
        self.title = title
        self.url = url
        self.added_by = added_by
        self.position = position
        self.added_at = added_at or datetime.now()
        
    @classmethod
    def from_db_row(cls, row):
        """Crea una instancia desde una fila de la base de datos"""
        if not row:
            return None
            
        return cls(
            id=row['id'] if 'id' in row else None,
            guild_id=row['guild_id'],
            song_id=row['song_id'],
            title=row['title'],
            url=row['url'],
            added_by=row['added_by'],
            position=row['position'] if 'position' in row else None,
            added_at=datetime.fromisoformat(row['added_at']) if row['added_at'] else None
        )
        
    def to_dict(self):
        """Convierte la instancia a un diccionario"""
        return {
            'id': self.id,
            'guild_id': self.guild_id,
            'song_id': self.song_id,
            'title': self.title,
            'url': self.url,
            'added_by': self.added_by,
            'position': self.position,
            'added_at': self.added_at.isoformat() if self.added_at else None
        }
        
    @property
    def requester(self):
        """Alias para added_by para mantener compatibilidad con el resto del código"""
        return self.added_by


class Song:
    """Modelo para representar una canción"""
    
    def __init__(self, song_id, title, artist=None, url=None, duration=None, thumbnail=None):
        self.song_id = song_id
        self.title = title
        self.artist = artist
        self.url = url
        self.duration = duration
        self.thumbnail = thumbnail
        
    @classmethod
    def from_youtube_info(cls, info):
        """Crea una instancia desde la información obtenida de YouTube"""
        if not info:
            return None
            
        return cls(
            song_id=info.get('id'),
            title=info.get('title'),
            artist=info.get('artist'),
            url=info.get('url'),
            duration=info.get('duration'),
            thumbnail=info.get('thumbnail')
        )
        
    def to_dict(self):
        """Convierte la instancia a un diccionario"""
        return {
            'song_id': self.song_id,
            'title': self.title,
            'artist': self.artist,
            'url': self.url,
            'duration': self.duration,
            'thumbnail': self.thumbnail
        }
        
    def format_duration(self):
        """Formatea la duración en formato mm:ss"""
        if not self.duration:
            return "??:??"
        
        minutes = int(self.duration // 60)
        seconds = int(self.duration % 60)
        return f"{minutes:02d}:{seconds:02d}"
    
from tortoise import fields, models

class AutoMessage(models.Model):
    id = fields.IntField(pk=True)
    guild_id = fields.IntField()
    channel_id = fields.IntField()
    message = fields.TextField()
    interval_seconds = fields.IntField()
    is_active = fields.BooleanField(default=True)

    @classmethod
    async def get_all_active(cls):
        return await cls.filter(is_active=True)

    @classmethod
    async def get_by_guild(cls, guild_id):
        return await cls.filter(guild_id=guild_id)

class KeywordTrigger(models.Model):
    id = fields.IntField(pk=True)
    guild_id = fields.IntField()
    keywords = fields.TextField()
    response = fields.TextField()
    responses = fields.TextField(default="")
    match_mode = fields.CharField(max_length=10, default="contains")
    use_embed = fields.BooleanField(default=False)
    embed_title = fields.TextField(default="", null=True)
    embed_footer = fields.TextField(default="", null=True)
    embed_color_r = fields.IntField(default=47)
    embed_color_g = fields.IntField(default=49)
    embed_color_b = fields.IntField(default=237)
    cooldown_seconds = fields.IntField(default=60)
    is_random = fields.BooleanField(default=False)
    channel_whitelist = fields.JSONField(default=list)
    channel_blacklist = fields.JSONField(default=list)
    response_channel_id = fields.IntField(null=True)
    is_active = fields.BooleanField(default=True)

    @classmethod
    async def get_all_active(cls):
        return await cls.filter(is_active=True)

    @classmethod
    async def get_by_guild(cls, guild_id):
        return await cls.filter(guild_id=guild_id)