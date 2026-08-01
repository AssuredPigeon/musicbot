# modules/auto_messages.py

import discord
import asyncio
import logging
import random
from datetime import datetime, timedelta
from data.models import AutoMessage, KeywordTrigger

logger = logging.getLogger('musicbot.auto_messages')

class AutoMessageSystem:
    def __init__(self, bot):
        """
        Inicializa el sistema de mensajes automáticos
        
        Args:
            bot: La instancia del cliente de Discord
        """
        self.bot = bot
        self.timed_tasks = {}  # Para almacenar tareas programadas por canal {canal_id: task}
        self.keyword_triggers = {}  # Para almacenar triggers por servidor {guild_id: [triggers]}
        self.last_triggered = {}  # Para controlar cooldowns {guild_id+keyword: datetime}
        self.cooldown = 60  # Cooldown en segundos para evitar spam de palabras clave
    
    async def load_auto_messages(self):
        """Carga todos los mensajes automáticos desde la base de datos"""
        try:
            # Este método se llamará al iniciar el bot
            # Carga los mensajes automáticos programados
            auto_messages = await AutoMessage.get_all_active()
            
            for msg in auto_messages:
                self._schedule_auto_message(msg)
                
            # Carga los triggers de palabras clave
            triggers = await KeywordTrigger.get_all_active()
            
            for trigger in triggers:
                if trigger.guild_id not in self.keyword_triggers:
                    self.keyword_triggers[trigger.guild_id] = []
                    
                self.keyword_triggers[trigger.guild_id].append(trigger)
                
            logger.info(f"Sistema de mensajes automáticos cargado: {len(auto_messages)} mensajes temporales, {len(triggers)} triggers de palabras")
            
        except Exception as e:
            logger.error(f"Error al cargar mensajes automáticos: {str(e)}")
    
    def _schedule_auto_message(self, auto_message):
        """
        Programa un mensaje automático para ser enviado periódicamente
        
        Args:
            auto_message: Objeto AutoMessage con la configuración
        """
        # Cancelar tarea existente si hay una para este canal
        if auto_message.channel_id in self.timed_tasks:
            self.timed_tasks[auto_message.channel_id].cancel()
            
        # Crear y guardar la nueva tarea
        task = asyncio.create_task(self._auto_message_loop(auto_message))
        self.timed_tasks[auto_message.channel_id] = task
        
    async def _auto_message_loop(self, auto_message):
        """
        Bucle de envío periódico de mensajes automáticos
        
        Args:
            auto_message: Objeto AutoMessage con la configuración
        """
        try:
            while True:
                # Obtener el canal
                channel = self.bot.get_channel(auto_message.channel_id)
                if not channel:
                    logger.warning(f"Canal {auto_message.channel_id} no encontrado, cancelando mensajes automáticos")
                    break
                
                # Esperar el intervalo configurado
                await asyncio.sleep(auto_message.interval_seconds)
                
                # Verificar actividad en el canal antes de enviar
                if auto_message.only_if_active:
                    # Verificamos mensajes en la última hora
                    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
                    messages = await channel.history(after=one_hour_ago, limit=5).flatten()
                    
                    if len(messages) < 5:  # Si hay menos de 5 mensajes en la última hora
                        continue  # Saltamos este ciclo, el canal no está activo
                
                # Seleccionar mensaje aleatorio si hay varios
                if auto_message.is_random and auto_message.messages:
                    messages = auto_message.messages.split('|||')
                    message = random.choice(messages).strip()
                else:
                    message = auto_message.message
                
                # Crear embed si está configurado así
                if auto_message.use_embed:
                    embed = discord.Embed(
                        description=message,
                        color=discord.Color.from_rgb(
                            auto_message.embed_color_r,
                            auto_message.embed_color_g,
                            auto_message.embed_color_b
                        )
                    )
                    
                    if auto_message.embed_title:
                        embed.title = auto_message.embed_title
                        
                    if auto_message.embed_footer:
                        embed.set_footer(text=auto_message.embed_footer)
                        
                    await channel.send(embed=embed)
                else:
                    # Enviar como mensaje normal
                    await channel.send(message)
                    
                logger.info(f"Mensaje automático enviado en canal {channel.name} ({channel.id})")
                
        except asyncio.CancelledError:
            logger.info(f"Tarea de mensajes automáticos cancelada para canal {auto_message.channel_id}")
        except Exception as e:
            logger.error(f"Error en bucle de mensajes automáticos: {str(e)}")
    
    async def process_message(self, message):
        """
        Procesa un mensaje para verificar si activa algún trigger de palabras clave
        
        Args:
            message: El mensaje de Discord a procesar
        """
        # Ignorar mensajes de bots
        if message.author.bot:
            return
            
        # Verificar si hay triggers para este servidor
        guild_id = message.guild.id if message.guild else None
        if not guild_id or guild_id not in self.keyword_triggers:
            return
            
        content = message.content.lower()
        
        # Revisar cada trigger configurado para este servidor
        for trigger in self.keyword_triggers[guild_id]:
            # Verificar si el canal está en la lista de permitidos (si hay lista)
            if trigger.channel_whitelist and message.channel.id not in trigger.channel_whitelist:
                continue
                
            # Verificar si el canal está en la lista de excluidos
            if trigger.channel_blacklist and message.channel.id in trigger.channel_blacklist:
                continue
            
            # Verificar la palabra clave según el modo
            matched = False
            
            if trigger.match_mode == "exact":
                # Coincidencia exacta (palabra completa)
                words = content.split()
                for keyword in trigger.keywords.lower().split(','):
                    keyword = keyword.strip()
                    if keyword in words:
                        matched = True
                        break
                        
            elif trigger.match_mode == "contains":
                # Contiene la palabra/frase en cualquier parte
                for keyword in trigger.keywords.lower().split(','):
                    keyword = keyword.strip()
                    if keyword in content:
                        matched = True
                        break
                        
            elif trigger.match_mode == "starts":
                # Empieza con la palabra/frase
                for keyword in trigger.keywords.lower().split(','):
                    keyword = keyword.strip()
                    if content.startswith(keyword):
                        matched = True
                        break
                        
            elif trigger.match_mode == "ends":
                # Termina con la palabra/frase
                for keyword in trigger.keywords.lower().split(','):
                    keyword = keyword.strip()
                    if content.endswith(keyword):
                        matched = True
                        break
                        
            elif trigger.match_mode == "regex":
                # Expresión regular (más avanzado)
                import re
                try:
                    pattern = re.compile(trigger.keywords, re.IGNORECASE)
                    if pattern.search(content):
                        matched = True
                except:
                    logger.error(f"Regex inválido en trigger {trigger.id}: {trigger.keywords}")
            
            # Si encontramos coincidencia y no está en cooldown, enviar respuesta
            if matched:
                # Verificar cooldown
                cooldown_key = f"{guild_id}_{trigger.id}"
                now = datetime.utcnow()
                
                if cooldown_key in self.last_triggered:
                    time_diff = (now - self.last_triggered[cooldown_key]).total_seconds()
                    if time_diff < trigger.cooldown_seconds:
                        continue  # Aún en cooldown, ignorar
                
                # Actualizar timestamp del último trigger
                self.last_triggered[cooldown_key] = now
                
                # Seleccionar respuesta (aleatoria si hay múltiples)
                if trigger.is_random and trigger.responses:
                    responses = trigger.responses.split('|||')
                    response = random.choice(responses).strip()
                else:
                    response = trigger.response
                
                # Sustituir variables especiales en la respuesta
                response = response.replace("{user}", message.author.mention)
                response = response.replace("{username}", message.author.display_name)
                response = response.replace("{channel}", message.channel.mention)
                response = response.replace("{server}", message.guild.name)
                
                # Determinar el canal para responder
                if trigger.response_channel_id:
                    response_channel = self.bot.get_channel(trigger.response_channel_id)
                else:
                    response_channel = message.channel
                    
                if not response_channel:
                    logger.warning(f"Canal para respuesta no encontrado: {trigger.response_channel_id}")
                    continue
                
                # Enviar como embed o mensaje normal
                try:
                    if trigger.use_embed:
                        embed = discord.Embed(
                            description=response,
                            color=discord.Color.from_rgb(
                                trigger.embed_color_r,
                                trigger.embed_color_g,
                                trigger.embed_color_b
                            )
                        )
                        
                        if trigger.embed_title:
                            embed.title = trigger.embed_title
                            
                        if trigger.embed_footer:
                            embed.set_footer(text=trigger.embed_footer)
                            
                        await response_channel.send(embed=embed)
                    else:
                        # Enviar como mensaje normal
                        await response_channel.send(response)
                        
                    logger.info(f"Mensaje de trigger enviado para palabra '{trigger.keywords}' en {response_channel.name}")
                    
                except Exception as e:
                    logger.error(f"Error al enviar respuesta de trigger: {str(e)}")
    
    async def add_auto_message(self, guild_id, channel_id, message, interval_minutes=60, 
                              use_embed=False, embed_title=None, embed_footer=None, 
                              embed_color=(47, 49, 237), only_if_active=True, is_random=False, 
                              messages=None):
        """
        Añade un nuevo mensaje automático programado
        
        Returns:
            AutoMessage: El objeto creado o None si hay error
        """
        try:
            # Crear registro en base de datos
            auto_message = await AutoMessage.create(
                guild_id=guild_id,
                channel_id=channel_id,
                message=message,
                messages=messages or "",
                interval_seconds=interval_minutes * 60,
                use_embed=use_embed,
                embed_title=embed_title or "",
                embed_footer=embed_footer or "",
                embed_color_r=embed_color[0],
                embed_color_g=embed_color[1],
                embed_color_b=embed_color[2],
                only_if_active=only_if_active,
                is_random=is_random,
                is_active=True
            )
            
            # Programar el mensaje
            self._schedule_auto_message(auto_message)
            
            return auto_message
            
        except Exception as e:
            logger.error(f"Error al añadir mensaje automático: {str(e)}")
            return None
    
    async def add_keyword_trigger(self, guild_id, keywords, response, match_mode="contains",
                                 use_embed=False, embed_title=None, embed_footer=None, 
                                 embed_color=(47, 49, 237), cooldown_seconds=60, is_random=False,
                                 responses=None, channel_whitelist=None, channel_blacklist=None,
                                 response_channel_id=None):
        """
        Añade un nuevo trigger de palabras clave
        
        Returns:
            KeywordTrigger: El objeto creado o None si hay error
        """
        try:
            # Crear registro en base de datos
            trigger = await KeywordTrigger.create(
                guild_id=guild_id,
                keywords=keywords,
                response=response,
                responses=responses or "",
                match_mode=match_mode,
                use_embed=use_embed,
                embed_title=embed_title or "",
                embed_footer=embed_footer or "",
                embed_color_r=embed_color[0],
                embed_color_g=embed_color[1],
                embed_color_b=embed_color[2],
                cooldown_seconds=cooldown_seconds,
                is_random=is_random,
                channel_whitelist=channel_whitelist or [],
                channel_blacklist=channel_blacklist or [],
                response_channel_id=response_channel_id,
                is_active=True
            )
            
            # Añadir a la caché en memoria
            if guild_id not in self.keyword_triggers:
                self.keyword_triggers[guild_id] = []
                
            self.keyword_triggers[guild_id].append(trigger)
            
            return trigger
            
        except Exception as e:
            logger.error(f"Error al añadir trigger de palabras clave: {str(e)}")
            return None
            
    async def remove_auto_message(self, message_id):
        """Elimina un mensaje automático programado"""
        try:
            # Obtener el mensaje de la base de datos
            auto_message = await AutoMessage.get(message_id)
            if not auto_message:
                return False
                
            # Cancelar la tarea asociada si existe
            if auto_message.channel_id in self.timed_tasks:
                self.timed_tasks[auto_message.channel_id].cancel()
                del self.timed_tasks[auto_message.channel_id]
            
            # Eliminar de la base de datos
            await AutoMessage.delete(message_id)
            
            return True
            
        except Exception as e:
            logger.error(f"Error al eliminar mensaje automático: {str(e)}")
            return False
            
    async def remove_keyword_trigger(self, trigger_id):
        """Elimina un trigger de palabras clave"""
        try:
            # Obtener el trigger de la base de datos
            trigger = await KeywordTrigger.get(trigger_id)
            if not trigger:
                return False
                
            # Eliminar de la caché en memoria
            if trigger.guild_id in self.keyword_triggers:
                self.keyword_triggers[trigger.guild_id] = [
                    t for t in self.keyword_triggers[trigger.guild_id] 
                    if t.id != trigger_id
                ]
            
            # Eliminar de la base de datos
            await KeywordTrigger.filter(id=trigger_id).delete()
            
            return True
            
        except Exception as e:
            logger.error(f"Error al eliminar trigger de palabras clave: {str(e)}")
            return False
    
    async def list_auto_messages(self, guild_id):
        """Obtiene la lista de mensajes automáticos para un servidor"""
        try:
            return await AutoMessage.get_by_guild(guild_id)
        except Exception as e:
            logger.error(f"Error al listar mensajes automáticos: {str(e)}")
            return []
            
    async def list_keyword_triggers(self, guild_id):
        """Obtiene la lista de triggers de palabras clave para un servidor"""
        try:
            return await KeywordTrigger.get_by_guild(guild_id)
        except Exception as e:
            logger.error(f"Error al listar triggers de palabras clave: {str(e)}")
            return []