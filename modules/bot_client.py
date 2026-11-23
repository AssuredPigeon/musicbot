#Configuracion del cliente de Discord
import discord  
from discord.ext import commands
from discord import app_commands
from discord.ui import View, Button
import logging
import subprocess
import config #cargamos las configuraciones que definimos en confing.py
from modules.music_player import MusicPlayer #clase con la que gestionamos la reproduccion de musica
from modules.voice_processor import capture_and_process_voice
from modules.intent_processor import IntentProcessor # para identificar la intencion de una frase (no agregado aun)
from utils.error_handler import log_exception
from modules.voice_commands import VoiceCommandHandler
from modules.auto_messages import AutoMessageSystem
from modules import welcome_system
from modules.help_command import setup as setup_help
from modules import dev_commands
from modules import ticket_system
from tortoise import Tortoise
from utils.cookie_manager import CookieMonitor, check_cookies_health
from utils.stt_queue import stt_queue_manager

# Configurar logger
logger = logging.getLogger('musicbot.client') #creamos el logger especifico para separar logs por modulos

# Configuración de intents para el bot
intents = discord.Intents.default() #definimos los eventos que puede ver el bot
intents.message_content = True #le permitimos leer el contenido de mensajes (para los comandos)
intents.voice_states = True # le permitimos que detecte conexiones o desconexiones de voz
intents.members = True # Necesario para eventos de miembros (como on_member_join)

# Inicializar bot con prefijos, descripcion y los permisos disponibels (intenciones)
bot = commands.Bot(command_prefix=config.BOT_PREFIX, description=config.BOT_DESCRIPTION, intents=intents)

async def load_extensions():

    setup_help(bot)
    await welcome_system.setup(bot)
    await ticket_system.setup(bot)
    dev_commands.setup(bot)  #Para actualizas los slahs

# Crear instancias compartidas
music_player = MusicPlayer(bot) #Para el manejo de la logica de reproduccion y cola de musica
intent_processor = IntentProcessor() #detectamos la intencion del usuario cuando habla (reproducir, detener, etc)
auto_message_system = AutoMessageSystem(bot)  # Inicializamos el sistema de mensajes automáticos

@bot.event
async def on_ready():
    """Evento que se dispara cuando el bot se ha conectado"""
    logger.info(f'Bot conectado como {bot.user}')

    # Cargar extensiones después de la conexión
    try:
        await load_extensions()
        logger.info("Extensiones cargadas correctamente")
    except Exception as e:
        log_exception("Error al cargar extensiones", e)

    # Cargar modulo de traduccion (separado del music bot)
    try:
        await bot.load_extension("translator.commands")
        logger.info("Modulo de traduccion cargado correctamente")
    except Exception as e:
        log_exception("Error al cargar el modulo de traduccion", e)

    # Establecer estado/actividad
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening, 
        name=f"{config.BOT_PREFIX}help | /help"
    ))

    # Sincronizar los comandos slash con Discord
        # Descomenta y ajusta la siguiente línea si quieres sincronizar para un servidor específico
        # guild_synced = await bot.tree.sync(guild=discord.Object(id=TU_ID_DE_SERVIDOR))
        # logger.info(f"Sincronizados {len(guild_synced)} comandos slash para servidor específico")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Sincronizados {len(synced)} comandos slash")
    except Exception as e:
        log_exception("Error al sincronizar comandos slash", e)

    await music_player.start_cleanup_task()
    stt_queue_manager.start(bot.loop)

    # Registrar la instancia única de VoiceCommandHandler en el MusicPlayer
    # Esto conecta el pipeline de voz: AudioSink -> Whisper (cola única) -> IntentProcessor -> MusicPlayer
    music_player.register_voice_command_handler(voice_command_handler)
    logger.info("VoiceCommandHandler único registrado en MusicPlayer")

    # Verificar estado de cookies de YouTube al arrancar
    cookie_health = check_cookies_health(config.COOKIES_PATH)
    logger.info(f"[Cookies] {cookie_health['message']}")
    if cookie_health["status"] in ("expired", "expiring_soon", "missing"):
        logger.warning("Las cookies de YouTube requieren atención. Ejecuta scripts/refresh_cookies.sh")

    # Iniciar monitor periódico de cookies (revisa cada 24h y avisa al owner por DM)
    cookie_monitor = CookieMonitor(bot, check_interval_hours=24)
    cookie_monitor.start()
    logger.info("CookieMonitor iniciado (verificación cada 24 horas)")

    # Cargar los mensajes automáticos cuando el bot esté listo
    try:
        await auto_message_system.load_auto_messages()
        logger.info("Sistema de mensajes automáticos cargado")
    except Exception as e:
        log_exception("Error al cargar mensajes automáticos", e)

# 4. Comando de prueba adicional para verificar que los slash commands funcionan
@bot.tree.command(name="ping", description="Comprueba si el bot está respondiendo")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! Latencia: {round(bot.latency * 1000)}ms", ephemeral=True)

@bot.event
async def on_message(message):
    # Procesar los comandos primero
    await bot.process_commands(message)
    
    # Luego procesar los triggers de palabras clave
    try:
        await auto_message_system.process_message(message)
    except Exception as e:
        log_exception("Error al procesar triggers de mensajes", e)


@bot.event
async def on_command_error(ctx, error):
    """Manejador global de errores para comandos"""
    if isinstance(error, commands.CommandNotFound):
        return  # Ignorar comandos no encontrados
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Faltan argumentos: {error}")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"Argumento inválido: {error}")
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Comando en cooldown. Intenta nuevamente en {error.retry_after:.2f} segundos.")
    else:
        # Loggear errores no manejados
        log_exception(f"Error en comando {ctx.command}", error)
        await ctx.send("Ocurrió un error inesperado. El administrador ha sido notificado.")

@bot.command()
async def join(ctx):
    """Comando para unirse al canal de voz del usuario"""
    try:
        if not ctx.author.voice:
            await ctx.send("Necesitas estar en un canal de voz primero.")
            return
            
        voice_channel = ctx.author.voice.channel
        await music_player.join_voice_channel(voice_channel)
        await ctx.send(f"Me he unido a {voice_channel.name}")
    except Exception as e:
        log_exception("Error en comando join", e)
        await ctx.send("No pude unirme al canal de voz.")

@bot.command()
async def play(ctx, *, query):
    """Reproducir una canción por texto"""
    try:
        if not ctx.author.voice:
            await ctx.send("Necesitas estar en un canal de voz primero.")
            return
            
        voice_channel = ctx.author.voice.channel
        
        # Unirse al canal si no lo está ya
        if not music_player.is_connected(ctx.guild.id):
            await music_player.join_voice_channel(voice_channel)
            
        # Procesar y reproducir
        response = await music_player.play_song(query, ctx.author.id)
        await ctx.send(response)
    except Exception as e:
        log_exception("Error en comando play", e)
        await ctx.send("No pude reproducir la canción.")

@bot.command()
async def listen(ctx):
    """Inicia modo de escucha para comandos de voz"""
    try:
        if not ctx.author.voice:
            await ctx.send("Necesitas estar en un canal de voz primero.")
            return
            
        guild_id = ctx.guild.id
        voice_channel = ctx.author.voice.channel

        # Unirse al canal si no lo está ya
        if not music_player.is_connected(guild_id):
            await music_player.join_voice_channel(voice_channel, require_receive=True)

        # Guardar el canal de texto donde se activó la escucha (para responder aquí)
        music_player.set_voice_text_channel(guild_id, ctx.channel.id)

        # Activar modo escucha
        response = await music_player.start_voice_command_mode(ctx.author.id)
        if response and not response.startswith("No") and not response.startswith("❌"):
            await ctx.send(response)
        else:
            await ctx.send(response or "No pude iniciar el modo de escucha de voz.")
    except Exception as e:
        log_exception("Error en comando listen", e)
        await ctx.send("No pude iniciar el modo de escucha.")

@bot.command()
async def stop_listen(ctx):
    """Detiene el modo de escucha para comandos de voz"""
    try:
        response = await music_player.stop_voice_command_mode(
            guild_id=ctx.guild.id if ctx.guild else None,
            user_id=ctx.author.id,
        )
        await ctx.send(response)
    except Exception as e:
        log_exception("Error en comando stop_listen", e)
        await ctx.send("Ocurrió un error al detener el modo de escucha.")

@bot.command()
async def skip(ctx):
    """Saltar a la siguiente canción"""
    try:
        response = await music_player.skip(guild_id=ctx.guild.id if ctx.guild else None)
        await ctx.send(response)
    except Exception as e:
        log_exception("Error en comando skip", e)
        await ctx.send("No pude saltar la canción.")

@bot.command()
async def stop(ctx):
    """Detener reproducción y desconectar"""
    try:
        response = await music_player.stop(guild_id=ctx.guild.id if ctx.guild else None)
        await ctx.send(response)
    except Exception as e:
        log_exception("Error en comando stop", e)
        await ctx.send("No pude detener la reproducción.")

@bot.command()
async def recommend(ctx):
    """Obtener recomendaciones basadas en el historial del usuario"""
    try:
        recommendations = await music_player.get_recommendations(ctx.author.id)
        if recommendations:
            await ctx.send(recommendations)
        else:
            await ctx.send("No tengo suficiente historial para recomendarte canciones. ¡Escucha algunas primero!")
    except Exception as e:
        log_exception("Error en comando recommend", e)
        await ctx.send("No pude generar recomendaciones.")

@bot.command(name="apagar")
async def shutdown(ctx):
    if ctx.author.id in config.OWNER_ID:  # Verificamos si el autor del comando tiene permisos
        await ctx.send("Apagando el bot...")

        # Desconectar el bot de Discord
        await Tortoise.close_connections()
        await bot.close()

        # Detener el proceso del bot en PM2
        subprocess.run(["pm2", "stop", "discord-musicbot"])
    else:
        await ctx.send("No tienes permiso para apagar el bot.")

@bot.command(name="encender")
async def start(ctx):
    if ctx.author.id in config.OWNER_ID:  # Verificamos si el autor tiene permisos
        await ctx.send("Encendiendo el bot...")

        # Iniciar el bot con PM2
        subprocess.run(["pm2", "start", "discord-musicbot"])

        await ctx.send("El bot se ha encendido correctamente.")
    else:
        await ctx.send("No tienes permiso para encender el bot.")

@bot.command(name="reiniciar")
async def restart(ctx):
    if ctx.author.id in config.OWNER_ID:  # Verificamos si el autor tiene permisos
        await ctx.send("Reiniciando el bot...")

        # Reiniciar el bot con PM2
        subprocess.run(["pm2", "restart", "discord-musicbot"])

        await ctx.send("El bot se ha reiniciado correctamente.")
    else:
        await ctx.send("No tienes permiso para reiniciar el bot.")


@bot.tree.command(name="play", description="Reproduce una canción por su nombre o URL")
@app_commands.describe(query="Nombre de la canción o URL")
async def slash_play(interaction: discord.Interaction, query: str):
    """Comando slash para reproducir música"""
    try:
        if not interaction.user.voice:
            await interaction.response.send_message("Necesitas estar en un canal de voz primero.", ephemeral=True)
            return
            
        voice_channel = interaction.user.voice.channel
        
        # Responder inmediatamente para evitar timeout
        await interaction.response.defer(thinking=True)
        
        # Unirse al canal si no lo está ya
        if not music_player.is_connected(interaction.guild_id):
            await music_player.join_voice_channel(voice_channel)
            
        # Procesar y reproducir
        response = await music_player.play_song(query, interaction.user.id)
        await interaction.followup.send(response)
    except Exception as e:
        log_exception("Error en comando slash play", e)
        if interaction.response.is_done():
            await interaction.followup.send("No pude reproducir la canción.", ephemeral=True)
        else:
            await interaction.response.send_message("No pude reproducir la canción.", ephemeral=True)

@bot.tree.command(name="skip", description="Salta a la siguiente canción en la cola")
async def slash_skip(interaction: discord.Interaction):
    """Comando slash para saltar la canción actual"""
    try:
        await interaction.response.defer(thinking=True)
        response = await music_player.skip(guild_id=interaction.guild_id)
        await interaction.followup.send(response)
    except Exception as e:
        log_exception("Error en comando slash skip", e)
        await interaction.followup.send("No pude saltar la canción.", ephemeral=True)

@bot.tree.command(name="stop", description="Detiene la reproducción y desconecta el bot")
async def slash_stop(interaction: discord.Interaction):
    """Comando slash para detener la reproducción"""
    try:
        await interaction.response.defer(thinking=True)
        response = await music_player.stop(guild_id=interaction.guild_id)
        await interaction.followup.send(response)
    except Exception as e:
        log_exception("Error en comando slash stop", e)
        await interaction.followup.send("No pude detener la reproducción.", ephemeral=True)

@bot.tree.command(name="join", description="Hace que el bot se una a tu canal de voz")
async def slash_join(interaction: discord.Interaction):
    """Comando slash para unirse al canal de voz"""
    try:
        if not interaction.user.voice:
            await interaction.response.send_message("Necesitas estar en un canal de voz primero.", ephemeral=True)
            return
            
        voice_channel = interaction.user.voice.channel
        await interaction.response.defer(thinking=True)
        await music_player.join_voice_channel(voice_channel)
        await interaction.followup.send(f"Me he unido a {voice_channel.name}")
    except Exception as e:
        log_exception("Error en comando slash join", e)
        if interaction.response.is_done():
            await interaction.followup.send("No pude unirme al canal de voz.", ephemeral=True)
        else:
            await interaction.response.send_message("No pude unirme al canal de voz.", ephemeral=True)

@bot.tree.command(name="listen", description="Inicia el modo de escucha para comandos de voz")
async def slash_listen(interaction: discord.Interaction):
    """Comando slash para iniciar escucha de voz"""
    try:
        if not interaction.user.voice:
            await interaction.response.send_message("Necesitas estar en un canal de voz primero.", ephemeral=True)
            return
            
        voice_channel = interaction.user.voice.channel
        guild_id = interaction.guild_id
        await interaction.response.defer(thinking=True)

        # Unirse al canal si no lo está ya
        if not music_player.is_connected(guild_id):
            await music_player.join_voice_channel(voice_channel, require_receive=True)

        # Guardar el canal de texto donde se activó la escucha (para responder aquí)
        music_player.set_voice_text_channel(guild_id, interaction.channel_id)

        # Activar modo escucha
        response = await music_player.start_voice_command_mode(interaction.user.id)
        if response and not response.startswith("No") and not response.startswith("❌"):
            await interaction.followup.send(response)
        else:
            await interaction.followup.send(response or "No pude iniciar el modo de escucha de voz.")
    except Exception as e:
        log_exception("Error en comando slash listen", e)
        await interaction.followup.send("No pude iniciar el modo de escucha.", ephemeral=True)

@bot.tree.command(name="stop_listen", description="Detiene el modo de escucha para comandos de voz")
async def slash_stop_listen(interaction: discord.Interaction):
    """Comando slash para detener escucha de voz"""
    try:
        await interaction.response.defer(thinking=True)
        response = await music_player.stop_voice_command_mode(
            guild_id=interaction.guild_id,
            user_id=interaction.user.id,
        )
        await interaction.followup.send(response)
    except Exception as e:
        log_exception("Error en comando slash stop_listen", e)
        await interaction.followup.send("Ocurrió un error al detener el modo de escucha.", ephemeral=True)

@bot.tree.command(name="recommend", description="Obtiene recomendaciones basadas en tu historial de escucha")
async def slash_recommend(interaction: discord.Interaction):
    """Comando slash para obtener recomendaciones"""
    try:
        await interaction.response.defer(thinking=True)
        recommendations = await music_player.get_recommendations(interaction.user.id)
        if recommendations:
            await interaction.followup.send(recommendations)
        else:
            await interaction.followup.send("No tengo suficiente historial para recomendarte canciones. ¡Escucha algunas primero!")
    except Exception as e:
        log_exception("Error en comando slash recommend", e)
        await interaction.followup.send("No pude generar recomendaciones.", ephemeral=True)

@bot.tree.command(name="apagar", description="Apaga el bot (solo para administradores)")
async def slash_shutdown(interaction: discord.Interaction):
    """Comando slash para apagar el bot"""
    if interaction.user.id in config.OWNER_ID:
        await interaction.response.send_message("Apagando el bot...")

        # Desconectar el bot de Discord
        await Tortoise.close_connections()
        await bot.close()

        # Detener el proceso del bot en PM2
        subprocess.run(["pm2", "stop", "discord-musicbot"])
    else:
        await interaction.response.send_message("No tienes permiso para apagar el bot.", ephemeral=True)

@bot.tree.command(name="encender", description="Enciende el bot (solo para administradores)")
async def slash_start(interaction: discord.Interaction):
    """Comando slash para encender el bot"""
    if interaction.user.id in config.OWNER_ID:
        await interaction.response.send_message("Encendiendo el bot...")

        # Iniciar el proceso del bot en PM2
        subprocess.run(["pm2", "start", "discord-musicbot"])

        await interaction.response.send_message("El bot se ha encendido correctamente.")
    else:
        await interaction.response.send_message("No tienes permiso para encender el bot.", ephemeral=True)

@bot.tree.command(name="reiniciar", description="Reinicia el bot (solo para administradores)")
async def slash_restart(interaction: discord.Interaction):
    """Comando slash para reiniciar el bot"""
    if interaction.user.id in config.OWNER_ID:
        await interaction.response.send_message("Reiniciando el bot...")

        # Reiniciar el proceso del bot en PM2
        subprocess.run(["pm2", "restart", "discord-musicbot"])

        await interaction.response.send_message("El bot se ha reiniciado correctamente.")
    else:
        await interaction.response.send_message("No tienes permiso para reiniciar el bot.", ephemeral=True)


@bot.tree.command(name="cookies", description="Verifica el estado de las cookies de YouTube (solo admins)")
async def slash_cookies(interaction: discord.Interaction):
    """Comando slash para verificar estado de cookies de YouTube"""
    if interaction.user.id not in config.OWNER_ID:
        await interaction.response.send_message(
            "No tienes permiso para usar este comando.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    health = check_cookies_health(config.COOKIES_PATH)

    status_emoji = {
        "ok": "✅",
        "expiring_soon": "🟡",
        "expired": "🔴",
        "missing": "❌",
    }.get(health["status"], "❓")

    msg = (
        f"**🍪 Estado de Cookies de YouTube**\n\n"
        f"{status_emoji} **Estado:** `{health['status']}`\n"
        f"📅 **Días hasta expiración:** `{health['days_until_expiry']}`\n\n"
        f"{health['message']}"
    )

    if health["status"] in ("expired", "expiring_soon", "missing"):
        msg += (
            "\n\n**Para renovar desde tu PC:**\n"
            "```bash\nbash scripts/refresh_cookies.sh\n```"
        )

    await interaction.followup.send(msg, ephemeral=True)


@bot.command(name="say")
@commands.has_permissions(manage_messages=True)  # Restringe el comando a personas con permiso de gestionar mensajes
async def say(ctx, *, mensaje=None):
    """
    Hace que el bot envíe un mensaje
    Uso: !say [mensaje]
    """
    try:
        # Si no hay mensaje, enviamos instrucciones
        if mensaje is None:
            await ctx.send("❌ Debes proporcionarme un mensaje para enviar. Uso: `!say [mensaje]`")
            return
            
        # Eliminar el comando original del usuario para mayor limpieza
        try:
            await ctx.message.delete()
        except:
            pass  # Ignoramos si no se puede borrar (falta de permisos, etc.)
            
        # Enviar el mensaje como el bot
        await ctx.send(mensaje)
        logger.info(f"Mensaje 'say' enviado por {ctx.author} en {ctx.channel.name}")
        
    except Exception as e:
        log_exception("Error en comando say", e)
        await ctx.send("❌ No pude enviar el mensaje.")

# Versión avanzada con selección de canal
@bot.command(name="sayto")
@commands.has_permissions(manage_messages=True)
async def sayto(ctx, canal: discord.TextChannel = None, *, mensaje=None):
    """
    Hace que el bot envíe un mensaje a un canal específico
    Uso: !sayto #canal [mensaje]
    """
    try:
        # Verificar parámetros
        if canal is None or mensaje is None:
            await ctx.send("❌ Debes especificar un canal y un mensaje. Uso: `!sayto #canal [mensaje]`")
            return
            
        # Verificar permisos en el canal objetivo
        if not canal.permissions_for(ctx.guild.me).send_messages:
            await ctx.send(f"❌ No tengo permiso para enviar mensajes en {canal.mention}")
            return
            
        # Enviar mensaje al canal especificado
        await canal.send(mensaje)
        
        # Confirmar al autor que se envió el mensaje
        await ctx.send(f"✅ Mensaje enviado a {canal.mention}", delete_after=5)
        
        # Eliminar el comando original
        try:
            await ctx.message.delete()
        except:
            pass
            
        logger.info(f"Mensaje 'sayto' enviado por {ctx.author} en {canal.name}")
        
    except Exception as e:
        log_exception("Error en comando sayto", e)
        await ctx.send("❌ No pude enviar el mensaje al canal especificado.")

# Comandos slash correspondientes
@bot.tree.command(name="say", description="Hace que el bot envíe un mensaje en este canal")
@app_commands.describe(mensaje="Mensaje que quieres que envíe el bot")
@app_commands.checks.has_permissions(manage_messages=True)
async def slash_say(interaction: discord.Interaction, mensaje: str):
    """Comando slash para que el bot envíe un mensaje"""
    try:
        # Responder de forma efímera (solo visible para el autor)
        await interaction.response.send_message("✅ Mensaje enviado", ephemeral=True)
        
        # Enviar el mensaje como el bot
        await interaction.channel.send(mensaje)
        logger.info(f"Mensaje 'say' slash enviado por {interaction.user} en {interaction.channel.name}")
        
    except Exception as e:
        log_exception("Error en comando slash say", e)
        if interaction.response.is_done():
            await interaction.followup.send("❌ No pude enviar el mensaje.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ No pude enviar el mensaje.", ephemeral=True)

@bot.tree.command(name="sayto", description="Hace que el bot envíe un mensaje a un canal específico")
@app_commands.describe(
    canal="Canal donde quieres enviar el mensaje",
    mensaje="Mensaje que quieres que envíe el bot"
)
@app_commands.checks.has_permissions(manage_messages=True)
async def slash_sayto(interaction: discord.Interaction, canal: discord.TextChannel, mensaje: str):
    """Comando slash para que el bot envíe un mensaje a un canal específico"""
    try:
        # Verificar permisos en el canal objetivo
        if not canal.permissions_for(interaction.guild.me).send_messages:
            await interaction.response.send_message(f"❌ No tengo permiso para enviar mensajes en {canal.mention}", ephemeral=True)
            return
            
        # Enviar mensaje al canal especificado
        await canal.send(mensaje)
        
        # Confirmar al autor que se envió el mensaje
        await interaction.response.send_message(f"✅ Mensaje enviado a {canal.mention}", ephemeral=True)
        logger.info(f"Mensaje 'sayto' slash enviado por {interaction.user} en {canal.name}")
        
    except Exception as e:
        log_exception("Error en comando slash sayto", e)
        if interaction.response.is_done():
            await interaction.followup.send("❌ No pude enviar el mensaje al canal especificado.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ No pude enviar el mensaje al canal especificado.", ephemeral=True)

# Manejo de errores para los comandos slash (permisos)
@slash_say.error
@slash_sayto.error
async def slash_permission_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message("❌ No tienes permisos para usar este comando. Se requiere permiso para gestionar mensajes.", ephemeral=True)
    else:
        log_exception("Error no manejado en comando say/sayto", error)
        await interaction.response.send_message("❌ Ocurrió un error al procesar tu comando.", ephemeral=True)


@bot.command(name="announce")
@commands.has_permissions(manage_messages=True)
async def announce(ctx, *, mensaje=None):
    """
    Crea un anuncio con formato embed
    Uso: !announce [mensaje]
    """
    try:
        # Si no hay mensaje, enviamos instrucciones
        if mensaje is None:
            await ctx.send("❌ Debes proporcionarme un mensaje para el anuncio. Uso: `!announce [mensaje]`")
            return
            
        # Crear un mensaje embed bonito
        embed = discord.Embed(
            title="📢 Anuncio",
            description=mensaje,
            color=discord.Color.blue(),
            timestamp=ctx.message.created_at
        )
        
        # Añadir autor del anuncio
        embed.set_author(
            name=ctx.author.display_name,
            icon_url=ctx.author.display_avatar.url
        )
        
        # Añadir pie de página
        embed.set_footer(text=f"Anuncio en {ctx.guild.name}")
        
        # Eliminar el comando original
        try:
            await ctx.message.delete()
        except:
            pass
            
        # Enviar el anuncio
        await ctx.send(embed=embed)
        logger.info(f"Anuncio enviado por {ctx.author} en {ctx.channel.name}")
        
    except Exception as e:
        log_exception("Error en comando announce", e)
        await ctx.send("❌ No pude enviar el anuncio.")

# Versión slash del comando de anuncio
@bot.tree.command(name="announce", description="Crea un anuncio con formato embed")
@app_commands.describe(
    titulo="Título del anuncio (opcional)",
    mensaje="Contenido del anuncio",
    color="Color del anuncio en formato hexadecimal (opcional, ej: #FF0000)"
)
@app_commands.checks.has_permissions(manage_messages=True)
async def slash_announce(
    interaction: discord.Interaction, 
    mensaje: str,
    titulo: str = "📢 Anuncio",
    color: str = None
):
    """Comando slash para crear anuncios con formato embed"""
    try:
        # Procesar el color si se proporciona
        embed_color = discord.Color.blue()  # Color predeterminado
        if color:
            try:
                # Convertir hex a un entero que Discord pueda usar
                if color.startswith('#'):
                    color = color[1:]
                embed_color = discord.Color(int(color, 16))
            except:
                # Si hay error en el formato, usamos el color predeterminado
                pass
                
        # Crear un mensaje embed bonito
        embed = discord.Embed(
            title=titulo,
            description=mensaje,
            color=embed_color,
            timestamp=interaction.created_at
        )
        
        # Añadir autor del anuncio
        embed.set_author(
            name=interaction.user.display_name,
            icon_url=interaction.user.display_avatar.url
        )
        
        # Añadir pie de página
        embed.set_footer(text=f"Anuncio en {interaction.guild.name}")
            
        # Responder primero de forma efímera
        await interaction.response.send_message("✅ Anuncio enviado", ephemeral=True)
        
        # Enviar el anuncio
        await interaction.channel.send(embed=embed)
        logger.info(f"Anuncio slash enviado por {interaction.user} en {interaction.channel.name}")
        
    except Exception as e:
        log_exception("Error en comando slash announce", e)
        if interaction.response.is_done():
            await interaction.followup.send("❌ No pude enviar el anuncio.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ No pude enviar el anuncio.", ephemeral=True)

# Comando para enviar un anuncio a un canal específico con embed
@bot.tree.command(name="announceto", description="Crea un anuncio con formato embed en un canal específico")
@app_commands.describe(
    canal="Canal donde quieres enviar el anuncio",
    titulo="Título del anuncio (opcional)",
    mensaje="Contenido del anuncio",
    color="Color del anuncio en formato hexadecimal (opcional, ej: #FF0000)"
)
@app_commands.checks.has_permissions(manage_messages=True)
async def slash_announceto(
    interaction: discord.Interaction,
    canal: discord.TextChannel,
    mensaje: str,
    titulo: str = "📢 Anuncio",
    color: str = None
):
    """Comando slash para crear anuncios con formato embed en un canal específico"""
    try:
        # Verificar permisos en el canal objetivo
        if not canal.permissions_for(interaction.guild.me).send_messages:
            await interaction.response.send_message(f"❌ No tengo permiso para enviar mensajes en {canal.mention}", ephemeral=True)
            return
            
        # Procesar el color si se proporciona
        embed_color = discord.Color.blue()  # Color predeterminado
        if color:
            try:
                # Convertir hex a un entero que Discord pueda usar
                if color.startswith('#'):
                    color = color[1:]
                embed_color = discord.Color(int(color, 16))
            except:
                # Si hay error en el formato, usamos el color predeterminado
                pass
                
        # Crear un mensaje embed bonito
        embed = discord.Embed(
            title=titulo,
            description=mensaje,
            color=embed_color,
            timestamp=interaction.created_at
        )
        
        # Añadir autor del anuncio
        embed.set_author(
            name=interaction.user.display_name,
            icon_url=interaction.user.display_avatar.url
        )
        
        # Añadir pie de página
        embed.set_footer(text=f"Anuncio en {interaction.guild.name}")
            
        # Enviar el anuncio al canal especificado
        await canal.send(embed=embed)
        
        # Confirmar al autor que se envió el anuncio
        await interaction.response.send_message(f"✅ Anuncio enviado a {canal.mention}", ephemeral=True)
        logger.info(f"Anuncio 'announceto' slash enviado por {interaction.user} en {canal.name}")
        
    except Exception as e:
        log_exception("Error en comando slash announceto", e)
        if interaction.response.is_done():
            await interaction.followup.send("❌ No pude enviar el anuncio al canal especificado.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ No pude enviar el anuncio al canal especificado.", ephemeral=True)

@bot.group(name="automsg", invoke_without_command=True)
@commands.has_permissions(manage_guild=True)
async def auto_msg(ctx):
    """Comandos para gestionar mensajes automáticos"""
    await ctx.send("🤖 **Sistema de Mensajes Automáticos**\n\n"
                  f"Usa `{config.BOT_PREFIX}automsg list` para ver los mensajes configurados\n"
                  f"Usa `{config.BOT_PREFIX}automsg add <#canal> <intervalo_minutos> <mensaje>` para añadir un mensaje\n"
                  f"Usa `{config.BOT_PREFIX}automsg remove <id>` para eliminar un mensaje\n"
                  f"Usa `{config.BOT_PREFIX}automsg help` para más información")

@auto_msg.command(name="help")
@commands.has_permissions(manage_guild=True)
async def auto_msg_help(ctx):
    """Muestra ayuda detallada sobre los mensajes automáticos"""
    embed = discord.Embed(
        title="📝 Ayuda de Mensajes Automáticos",
        description="Sistema para enviar mensajes periódicamente en canales específicos.",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name=f"{config.BOT_PREFIX}automsg list",
        value="Lista todos los mensajes automáticos configurados en el servidor.",
        inline=False
    )
    
    embed.add_field(
        name=f"{config.BOT_PREFIX}automsg add <#canal> <minutos> <mensaje>",
        value="Añade un nuevo mensaje automático que se enviará cada X minutos.",
        inline=False
    )
    
    embed.add_field(
        name=f"{config.BOT_PREFIX}automsg addembed <#canal> <minutos> <título> | <mensaje> | <pie_de_página>",
        value="Añade un nuevo mensaje automático con formato embed.",
        inline=False
    )
    
    embed.add_field(
        name=f"{config.BOT_PREFIX}automsg random <#canal> <minutos> <mensaje1> ||| <mensaje2> ||| ...",
        value="Añade mensajes automáticos aleatorios separados por |||",
        inline=False
    )
    
    embed.add_field(
        name=f"{config.BOT_PREFIX}automsg remove <id>",
        value="Elimina un mensaje automático por su ID.",
        inline=False
    )
    
    embed.set_footer(text="Todos los comandos requieren permisos para gestionar el servidor.")
    
    await ctx.send(embed=embed)

@auto_msg.command(name="list")
@commands.has_permissions(manage_guild=True)
async def auto_msg_list(ctx):
    """Lista los mensajes automáticos configurados"""
    try:
        messages = await auto_message_system.list_auto_messages(ctx.guild.id)
        
        if not messages:
            await ctx.send("⚠️ No hay mensajes automáticos configurados en este servidor.")
            return
        
        embed = discord.Embed(
            title="📋 Mensajes Automáticos Configurados",
            description=f"Total: {len(messages)} mensajes",
            color=discord.Color.blue()
        )
        
        for msg in messages:
            channel = ctx.guild.get_channel(msg.channel_id)
            channel_name = f"#{channel.name}" if channel else f"Canal desconocido ({msg.channel_id})"
            interval_mins = msg.interval_seconds // 60
            
            content = msg.message
            if len(content) > 100:
                content = content[:97] + "..."
                
            embed.add_field(
                name=f"ID: {msg.id} - {channel_name} - Cada {interval_mins} minutos",
                value=f"{'📋 (Embed) ' if msg.use_embed else ''}"
                      f"{'🎲 (Aleatorio) ' if msg.is_random else ''}"
                      f"{'🔄 (Solo si activo) ' if msg.only_if_active else ''}"
                      f"```{content}```",
                inline=False
            )
        
        await ctx.send(embed=embed)
            
    except Exception as e:
        log_exception("Error al listar mensajes automáticos", e)
        await ctx.send("❌ Ocurrió un error al listar los mensajes automáticos.")

@auto_msg.command(name="add")
@commands.has_permissions(manage_guild=True)
async def auto_msg_add(ctx, channel: discord.TextChannel, interval: int, *, message: str):
    """Añade un mensaje automático"""
    if interval < 1:
        await ctx.send("❌ El intervalo debe ser al menos 1 minuto.")
        return
        
    if len(message) > 2000:
        await ctx.send("❌ El mensaje no puede exceder los 2000 caracteres.")
        return
        
    try:
        auto_message = await auto_message_system.add_auto_message(
            guild_id=ctx.guild.id,
            channel_id=channel.id,
            message=message,
            interval_minutes=interval
        )
        
        if auto_message:
            await ctx.send(f"✅ Mensaje automático añadido con ID {auto_message.id}. Se enviará cada {interval} minutos en {channel.mention}.")
        else:
            await ctx.send("❌ No se pudo añadir el mensaje automático.")
            
    except Exception as e:
        log_exception("Error al añadir mensaje automático", e)
        await ctx.send("❌ Ocurrió un error al añadir el mensaje automático.")

@auto_msg.command(name="addembed")
@commands.has_permissions(manage_guild=True)
async def auto_msg_add_embed(ctx, channel: discord.TextChannel, interval: int, *, content: str):
    """Añade un mensaje automático con formato embed (título | mensaje | pie)"""
    if interval < 1:
        await ctx.send("❌ El intervalo debe ser al menos 1 minuto.")
        return
        
    parts = content.split("|", 2)
    
    if len(parts) < 2:
        await ctx.send("❌ Formato incorrecto. Uso: `!automsg addembed #canal intervalo título | mensaje | pie_de_página`")
        return
        
    title = parts[0].strip()
    message = parts[1].strip()
    footer = parts[2].strip() if len(parts) > 2 else ""
    
    if len(message) > 4000:
        await ctx.send("❌ El mensaje no puede exceder los 4000 caracteres.")
        return
        
    try:
        auto_message = await auto_message_system.add_auto_message(
            guild_id=ctx.guild.id,
            channel_id=channel.id,
            message=message,
            interval_minutes=interval,
            use_embed=True,
            embed_title=title,
            embed_footer=footer
        )
        
        if auto_message:
            await ctx.send(f"✅ Mensaje automático con embed añadido con ID {auto_message.id}. Se enviará cada {interval} minutos en {channel.mention}.")
        else:
            await ctx.send("❌ No se pudo añadir el mensaje automático.")
            
    except Exception as e:
        log_exception("Error al añadir mensaje automático con embed", e)
        await ctx.send("❌ Ocurrió un error al añadir el mensaje automático.")

@auto_msg.command(name="random")
@commands.has_permissions(manage_guild=True)
async def auto_msg_random(ctx, channel: discord.TextChannel, interval: int, *, messages: str):
    """Añade mensajes automáticos aleatorios separados por |||"""
    if interval < 1:
        await ctx.send("❌ El intervalo debe ser al menos 1 minuto.")
        return
        
    message_list = messages.split("|||")
    
    if len(message_list) < 2:
        await ctx.send("❌ Debes proporcionar al menos 2 mensajes separados por |||")
        return
        
    try:
        auto_message = await auto_message_system.add_auto_message(
            guild_id=ctx.guild.id,
            channel_id=channel.id,
            message="",  # Placeholder, no se usará
            interval_minutes=interval,
            is_random=True,
            messages=messages
        )
        
        if auto_message:
            await ctx.send(f"✅ Conjunto de {len(message_list)} mensajes aleatorios añadido con ID {auto_message.id}. Uno de ellos se enviará cada {interval} minutos en {channel.mention}.")
        else:
            await ctx.send("❌ No se pudieron añadir los mensajes automáticos aleatorios.")
            
    except Exception as e:
        log_exception("Error al añadir mensajes automáticos aleatorios", e)
        await ctx.send("❌ Ocurrió un error al añadir los mensajes automáticos aleatorios.")

@auto_msg.command(name="remove")
@commands.has_permissions(manage_guild=True)
async def auto_msg_remove(ctx, message_id: int):
    """Elimina un mensaje automático por su ID"""
    try:
        success = await auto_message_system.remove_auto_message(message_id)
        
        if success:
            await ctx.send(f"✅ Mensaje automático con ID {message_id} eliminado correctamente.")
        else:
            await ctx.send(f"❌ No se encontró un mensaje automático con ID {message_id}.")
            
    except Exception as e:
        log_exception("Error al eliminar mensaje automático", e)
        await ctx.send("❌ Ocurrió un error al eliminar el mensaje automático.")

# ------------ GRUPO DE COMANDOS PARA TRIGGERS DE PALABRAS CLAVE ------------

@bot.group(name="trigger", invoke_without_command=True)
@commands.has_permissions(manage_guild=True)
async def trigger(ctx):
    """Comandos para gestionar triggers de palabras clave"""
    await ctx.send("🔤 **Sistema de Triggers de Palabras Clave**\n\n"
                  f"Usa `{config.BOT_PREFIX}trigger list` para ver los triggers configurados\n"
                  f"Usa `{config.BOT_PREFIX}trigger add <palabras> <respuesta>` para añadir un trigger\n"
                  f"Usa `{config.BOT_PREFIX}trigger remove <id>` para eliminar un trigger\n"
                  f"Usa `{config.BOT_PREFIX}trigger help` para más información")

@trigger.command(name="help")
@commands.has_permissions(manage_guild=True)
async def trigger_help(ctx):
    """Muestra ayuda detallada sobre los triggers de palabras clave"""
    embed = discord.Embed(
        title="🔤 Ayuda de Triggers de Palabras",
        description="Sistema para responder automáticamente cuando se detectan palabras o frases específicas.",
        color=discord.Color.green()
    )
    
    embed.add_field(
        name=f"{config.BOT_PREFIX}trigger list",
        value="Lista todos los triggers de palabras configurados en el servidor.",
        inline=False
    )
    
    embed.add_field(
        name=f"{config.BOT_PREFIX}trigger add <palabras_clave> <respuesta>",
        value="Añade un nuevo trigger que responderá cuando se detecten las palabras clave.",
        inline=False
    )
    
    embed.add_field(
        name=f"{config.BOT_PREFIX}trigger addembed <palabras_clave> <título> | <respuesta> | <pie_de_página>",
        value="Añade un nuevo trigger con respuesta en formato embed.",
        inline=False
    )
    
    embed.add_field(
        name=f"{config.BOT_PREFIX}trigger addmode <tipo_coincidencia> <palabras_clave> <respuesta>",
        value="Añade un trigger con un modo de coincidencia específico:\n"
              "- exact: Coincidencia exacta de palabra\n"
              "- contains: Contiene la palabra en cualquier parte\n"
              "- starts: El mensaje comienza con la palabra\n"
              "- ends: El mensaje termina con la palabra\n"
              "- regex: Usa expresión regular",
        inline=False
    )
    
    embed.add_field(
        name=f"{config.BOT_PREFIX}trigger random <palabras_clave> <respuesta1> ||| <respuesta2> ||| ...",
        value="Configura respuestas aleatorias para un trigger.",
        inline=False
    )
    
    embed.add_field(
        name=f"{config.BOT_PREFIX}trigger remove <id>",
        value="Elimina un trigger por su ID.",
        inline=False
    )
    
    embed.add_field(
        name="Variables disponibles en respuestas",
        value="{user} - Mención del usuario\n"
              "{username} - Nombre del usuario\n"
              "{channel} - Mención del canal\n"
              "{server} - Nombre del servidor",
        inline=False
    )
    
    embed.set_footer(text="Todos los comandos requieren permisos para gestionar el servidor.")
    
    await ctx.send(embed=embed)

@trigger.command(name="list")
@commands.has_permissions(manage_guild=True)
async def trigger_list(ctx):
    """Lista los triggers de palabras clave configurados"""
    try:
        triggers = await auto_message_system.list_keyword_triggers(ctx.guild.id)
        
        if not triggers:
            await ctx.send("⚠️ No hay triggers de palabras clave configurados en este servidor.")
            return
        
        embed = discord.Embed(
            title="📋 Triggers de Palabras Configurados",
            description=f"Total: {len(triggers)} triggers",
            color=discord.Color.green()
        )
        
        for t in triggers:
            keywords = t.keywords
            if len(keywords) > 50:
                keywords = keywords[:47] + "..."
                
            response = t.response
            if len(response) > 100:
                response = response[:97] + "..."
                
            embed.add_field(
                name=f"ID: {t.id} - Modo: {t.match_mode} - Cooldown: {t.cooldown_seconds}s",
                value=f"📝 Palabras: `{keywords}`\n"
                      f"{'📋 (Embed) ' if t.use_embed else ''}"
                      f"{'🎲 (Aleatorio) ' if t.is_random else ''}"
                      f"✏️ Respuesta: ```{response}```",
                inline=False
            )
        
        await ctx.send(embed=embed)
            
    except Exception as e:
        log_exception("Error al listar triggers", e)
        await ctx.send("❌ Ocurrió un error al listar los triggers.")

@trigger.command(name="add")
@commands.has_permissions(manage_guild=True)
async def trigger_add(ctx, keywords: str, cooldown: int = 60, *, response: str):
    """Añade un trigger de palabras clave"""

    if cooldown < 0:
        await ctx.send("❌ El cooldown debe ser un número positivo.")
        return
    
    try:
        trigger = await auto_message_system.add_keyword_trigger(
            guild_id=ctx.guild.id,
            keywords=keywords,
            response=response,
            match_mode="contains",
            cooldown_seconds=cooldown
        )
        
        if trigger:
            await ctx.send(
                f"✅ Trigger añadido con ID {trigger.id}. "
                f"Responderá cuando alguien escriba '{keywords}' "
                f"con un cooldown de {cooldown}s."
            )
        else:
            await ctx.send("❌ No se pudo añadir el trigger.")
            
    except Exception as e:
        log_exception("Error al añadir trigger", e)
        await ctx.send("❌ Ocurrió un error al añadir el trigger.")

@trigger.command(name="addembed")
@commands.has_permissions(manage_guild=True)
async def trigger_add_embed(ctx, keywords: str, *, content: str):
    """Añade un trigger con respuesta en formato embed (título | mensaje | pie)"""
    parts = content.split("|", 2)
    
    if len(parts) < 2:
        await ctx.send("❌ Formato incorrecto. Uso: `!trigger addembed palabras_clave título | mensaje | pie_de_página`")
        return
        
    title = parts[0].strip()
    message = parts[1].strip()
    footer = parts[2].strip() if len(parts) > 2 else ""
    
    try:
        trigger = await auto_message_system.add_keyword_trigger(
            guild_id=ctx.guild.id,
            keywords=keywords,
            response=message,
            match_mode="contains",
            use_embed=True,
            embed_title=title,
            embed_footer=footer
        )
        
        if trigger:
            await ctx.send(f"✅ Trigger con embed añadido con ID {trigger.id}. Responderá cuando alguien escriba '{keywords}'.")
        else:
            await ctx.send("❌ No se pudo añadir el trigger.")
            
    except Exception as e:
        log_exception("Error al añadir trigger con embed", e)
        await ctx.send("❌ Ocurrió un error al añadir el trigger.")

@trigger.command(name="addmode")
@commands.has_permissions(manage_guild=True)
async def trigger_add_mode(ctx, match_mode: str, keywords: str, *, response: str):
    """Añade un trigger con modo de coincidencia específico"""
    valid_modes = ["exact", "contains", "starts", "ends", "regex"]
    
    if match_mode not in valid_modes:
        await ctx.send(f"❌ Modo de coincidencia inválido. Valores permitidos: {', '.join(valid_modes)}")
        return
    
    try:
        trigger = await auto_message_system.add_keyword_trigger(
            guild_id=ctx.guild.id,
            keywords=keywords,
            response=response,
            match_mode=match_mode
        )
        
        if trigger:
            await ctx.send(f"✅ Trigger añadido con ID {trigger.id}. Modo: {match_mode}. Responderá con '{response}'.")
        else:
            await ctx.send("❌ No se pudo añadir el trigger.")
            
    except Exception as e:
        log_exception("Error al añadir trigger con modo específico", e)
        await ctx.send("❌ Ocurrió un error al añadir el trigger.")

@trigger.command(name="random")
@commands.has_permissions(manage_guild=True)
async def trigger_random(ctx, keywords: str, *, responses: str):
    """Añade un trigger con respuestas aleatorias separadas por |||"""
    response_list = responses.split("|||")
    
    if len(response_list) < 2:
        await ctx.send("❌ Debes proporcionar al menos 2 respuestas separadas por |||")
        return
        
    try:
        trigger = await auto_message_system.add_keyword_trigger(
            guild_id=ctx.guild.id,
            keywords=keywords,
            response=response_list[0].strip(),  # Primera respuesta como principal
            match_mode="contains",
            is_random=True,
            responses=responses
        )
        
        if trigger:
            await ctx.send(f"✅ Trigger con {len(response_list)} respuestas aleatorias añadido con ID {trigger.id}. Responderá cuando alguien escriba '{keywords}'.")
        else:
            await ctx.send("❌ No se pudo añadir el trigger.")
            
    except Exception as e:
        log_exception("Error al añadir trigger con respuestas aleatorias", e)
        await ctx.send("❌ Ocurrió un error al añadir el trigger.")

@trigger.command(name="remove")
@commands.has_permissions(manage_guild=True)
async def trigger_remove(ctx, trigger_id: int):
    """Elimina un trigger por su ID"""
    try:
        success = await auto_message_system.remove_keyword_trigger(trigger_id)
        
        if success:
            await ctx.send(f"✅ Trigger con ID {trigger_id} eliminado correctamente.")
        else:
            await ctx.send(f"❌ No se encontró un trigger con ID {trigger_id}.")
            
    except Exception as e:
        log_exception("Error al eliminar trigger", e)
        await ctx.send("❌ Ocurrió un error al eliminar el trigger.")

# ------------ COMANDOS SLASH PARA AUTO MENSAJES ------------

@bot.tree.command(name="automsg_list", description="Lista los mensajes automáticos configurados")
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_automsg_list(interaction: discord.Interaction):
    try:
        messages = await auto_message_system.list_auto_messages(interaction.guild.id)
        
        if not messages:
            await interaction.response.send_message("⚠️ No hay mensajes automáticos configurados en este servidor.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="📋 Mensajes Automáticos Configurados",
            description=f"Total: {len(messages)} mensajes",
            color=discord.Color.blue()
        )
        
        for msg in messages:
            channel = interaction.guild.get_channel(msg.channel_id)
            channel_name = f"#{channel.name}" if channel else f"Canal desconocido ({msg.channel_id})"
            interval_mins = msg.interval_seconds // 60
            
            content = msg.message
            if len(content) > 100:
                content = content[:97] + "..."
                
            embed.add_field(
                name=f"ID: {msg.id} - {channel_name} - Cada {interval_mins} minutos",
                value=f"{'📋 (Embed) ' if msg.use_embed else ''}"
                      f"{'🎲 (Aleatorio) ' if msg.is_random else ''}"
                      f"{'🔄 (Solo si activo) ' if msg.only_if_active else ''}"
                      f"```{content}```",
                inline=False
            )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
            
    except Exception as e:
        log_exception("Error al listar mensajes automáticos", e)
        await interaction.response.send_message("❌ Ocurrió un error al listar los mensajes automáticos.", ephemeral=True)

@bot.tree.command(name="automsg_add", description="Añade un mensaje automático")
@app_commands.describe(
    channel="Canal donde se enviará el mensaje",
    interval="Intervalo en minutos entre mensajes",
    message="Mensaje a enviar"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_automsg_add(interaction: discord.Interaction, channel: discord.TextChannel, interval: int, message: str):
    if interval < 1:
        await interaction.response.send_message("❌ El intervalo debe ser al menos 1 minuto.", ephemeral=True)
        return
        
    if len(message) > 2000:
        await interaction.response.send_message("❌ El mensaje no puede exceder los 2000 caracteres.", ephemeral=True)
        return
        
    try:
        auto_message = await auto_message_system.add_auto_message(
            guild_id=interaction.guild.id,
            channel_id=channel.id,
            message=message,
            interval_minutes=interval
        )
        
        if auto_message:
            await interaction.response.send_message(f"✅ Mensaje automático añadido con ID {auto_message.id}. Se enviará cada {interval} minutos en {channel.mention}.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ No se pudo añadir el mensaje automático.", ephemeral=True)
            
    except Exception as e:
        log_exception("Error al añadir mensaje automático", e)
        await interaction.response.send_message("❌ Ocurrió un error al añadir el mensaje automático.", ephemeral=True)

@bot.tree.command(name="automsg_remove", description="Elimina un mensaje automático por su ID")
@app_commands.describe(message_id="ID del mensaje automático a eliminar")
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_automsg_remove(interaction: discord.Interaction, message_id: int):
    try:
        success = await auto_message_system.remove_auto_message(message_id)
        
        if success:
            await interaction.response.send_message(f"✅ Mensaje automático con ID {message_id} eliminado correctamente.", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ No se encontró un mensaje automático con ID {message_id}.", ephemeral=True)
            
    except Exception as e:
        log_exception("Error al eliminar mensaje automático", e)
        await interaction.response.send_message("❌ Ocurrió un error al eliminar el mensaje automático.", ephemeral=True)

@bot.tree.command(name="automsg_addembed", description="Añade un mensaje automático con formato embed")
@app_commands.describe(
    channel="Canal donde se enviará el mensaje",
    interval="Intervalo en minutos entre mensajes",
    title="Título del embed",
    message="Contenido del mensaje",
    footer="Pie de página (opcional)"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_automsg_addembed(
    interaction: discord.Interaction, 
    channel: discord.TextChannel, 
    interval: int, 
    title: str, 
    message: str, 
    footer: str = ""
):
    if interval < 1:
        await interaction.response.send_message("❌ El intervalo debe ser al menos 1 minuto.", ephemeral=True)
        return
        
    if len(message) > 4000:
        await interaction.response.send_message("❌ El mensaje no puede exceder los 4000 caracteres.", ephemeral=True)
        return
        
    try:
        auto_message = await auto_message_system.add_auto_message(
            guild_id=interaction.guild.id,
            channel_id=channel.id,
            message=message,
            interval_minutes=interval,
            use_embed=True,
            embed_title=title,
            embed_footer=footer
        )
        
        if auto_message:
            await interaction.response.send_message(f"✅ Mensaje automático con embed añadido con ID {auto_message.id}. Se enviará cada {interval} minutos en {channel.mention}.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ No se pudo añadir el mensaje automático.", ephemeral=True)
            
    except Exception as e:
        log_exception("Error al añadir mensaje automático con embed", e)
        await interaction.response.send_message("❌ Ocurrió un error al añadir el mensaje automático.", ephemeral=True)

@bot.tree.command(name="automsg_random", description="Añade mensajes automáticos aleatorios")
@app_commands.describe(
    channel="Canal donde se enviarán los mensajes",
    interval="Intervalo en minutos entre mensajes",
    messages="Mensajes separados por |||"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_automsg_random(interaction: discord.Interaction, channel: discord.TextChannel, interval: int, messages: str):
    if interval < 1:
        await interaction.response.send_message("❌ El intervalo debe ser al menos 1 minuto.", ephemeral=True)
        return
        
    message_list = messages.split("|||")
    
    if len(message_list) < 2:
        await interaction.response.send_message("❌ Debes proporcionar al menos 2 mensajes separados por |||", ephemeral=True)
        return
        
    try:
        auto_message = await auto_message_system.add_auto_message(
            guild_id=interaction.guild.id,
            channel_id=channel.id,
            message="",  # Placeholder, no se usará
            interval_minutes=interval,
            is_random=True,
            messages=messages
        )
        
        if auto_message:
            await interaction.response.send_message(f"✅ Conjunto de {len(message_list)} mensajes aleatorios añadido con ID {auto_message.id}. Uno de ellos se enviará cada {interval} minutos en {channel.mention}.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ No se pudieron añadir los mensajes automáticos aleatorios.", ephemeral=True)
            
    except Exception as e:
        log_exception("Error al añadir mensajes automáticos aleatorios", e)
        await interaction.response.send_message("❌ Ocurrió un error al añadir los mensajes automáticos aleatorios.", ephemeral=True)

# ------------ COMANDOS SLASH PARA TRIGGERS ------------

@bot.tree.command(name="trigger_list", description="Lista los triggers de palabras clave configurados")
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_trigger_list(interaction: discord.Interaction):
    try:
        triggers = await auto_message_system.list_keyword_triggers(interaction.guild.id)
        
        if not triggers:
            await interaction.response.send_message("⚠️ No hay triggers de palabras clave configurados en este servidor.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="📋 Triggers de Palabras Configurados",
            description=f"Total: {len(triggers)} triggers",
            color=discord.Color.green()
        )
        
        for t in triggers:
            keywords = t.keywords
            if len(keywords) > 50:
                keywords = keywords[:47] + "..."
                
            response = t.response
            if len(response) > 100:
                response = response[:97] + "..."
                
            embed.add_field(
                name=f"ID: {t.id} - Modo: {t.match_mode} - Cooldown: {t.cooldown_seconds}s",
                value=f"📝 Palabras: `{keywords}`\n"
                      f"{'📋 (Embed) ' if t.use_embed else ''}"
                      f"{'🎲 (Aleatorio) ' if t.is_random else ''}"
                      f"✏️ Respuesta: ```{response}```",
                inline=False
            )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
            
    except Exception as e:
        log_exception("Error al listar triggers", e)
        await interaction.response.send_message("❌ Ocurrió un error al listar los triggers.", ephemeral=True)

@bot.tree.command(name="trigger_add", description="Añade un trigger de palabras clave")
@app_commands.describe(
    keywords="Palabras clave que activarán la respuesta",
    response="Respuesta que enviará el bot",
    cooldown="Tiempo de espera entre respuestas (en segundos, opcional, por defecto 60)"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_trigger_add(interaction: discord.Interaction, keywords: str, response: str, cooldown: int = 60):
    if cooldown < 0:
        await interaction.response.send_message("❌ El cooldown debe ser un número positivo.", ephemeral=True)
        return
    try:
        trigger = await auto_message_system.add_keyword_trigger(
            guild_id=interaction.guild.id,
            keywords=keywords,
            response=response,
            match_mode="contains",
            cooldown_seconds=cooldown
        )
        
        if trigger:
             await interaction.response.send_message(
                f"✅ Trigger añadido con ID {trigger.id}. "
                f"Responderá cuando alguien escriba '{keywords}' "
                f"con un cooldown de {cooldown}s.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message("❌ No se pudo añadir el trigger.", ephemeral=True)
            
    except Exception as e:
        log_exception("Error al añadir trigger", e)
        await interaction.response.send_message("❌ Ocurrió un error al añadir el trigger.", ephemeral=True)

@bot.tree.command(name="trigger_addembed", description="Añade un trigger con respuesta en formato embed")
@app_commands.describe(
    keywords="Palabras clave que activarán la respuesta",
    title="Título del embed",
    message="Contenido del mensaje",
    footer="Pie de página (opcional)"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_trigger_addembed(
    interaction: discord.Interaction,
    keywords: str,
    title: str,
    message: str,
    footer: str = ""
):
    try:
        trigger = await auto_message_system.add_keyword_trigger(
            guild_id=interaction.guild.id,
            keywords=keywords,
            response=message,
            match_mode="contains",
            use_embed=True,
            embed_title=title,
            embed_footer=footer
        )
        
        if trigger:
            await interaction.response.send_message(f"✅ Trigger con embed añadido con ID {trigger.id}. Responderá cuando alguien escriba '{keywords}'.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ No se pudo añadir el trigger.", ephemeral=True)
            
    except Exception as e:
        log_exception("Error al añadir trigger con embed", e)
        await interaction.response.send_message("❌ Ocurrió un error al añadir el trigger.", ephemeral=True)

@bot.tree.command(name="trigger_addmode", description="Añade un trigger con modo de coincidencia específico")
@app_commands.describe(
    match_mode="Modo de coincidencia (exact, contains, starts, ends, regex)",
    keywords="Palabras clave que activarán la respuesta",
    response="Respuesta que enviará el bot"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_trigger_addmode(interaction: discord.Interaction, match_mode: str, keywords: str, response: str):
    valid_modes = ["exact", "contains", "starts", "ends", "regex"]
    
    if match_mode not in valid_modes:
        await interaction.response.send_message(f"❌ Modo de coincidencia inválido. Valores permitidos: {', '.join(valid_modes)}", ephemeral=True)
        return
    
    try:
        trigger = await auto_message_system.add_keyword_trigger(
            guild_id=interaction.guild.id,
            keywords=keywords,
            response=response,
            match_mode=match_mode
        )
        
        if trigger:
            await interaction.response.send_message(f"✅ Trigger añadido con ID {trigger.id}. Modo: {match_mode}. Responderá con '{response}'.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ No se pudo añadir el trigger.", ephemeral=True)
            
    except Exception as e:
        log_exception("Error al añadir trigger con modo específico", e)
        await interaction.response.send_message("❌ Ocurrió un error al añadir el trigger.", ephemeral=True)

@bot.tree.command(name="trigger_random", description="Añade un trigger con respuestas aleatorias")
@app_commands.describe(
    keywords="Palabras clave que activarán la respuesta",
    responses="Respuestas separadas por |||"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_trigger_random(interaction: discord.Interaction, keywords: str, responses: str):
    response_list = responses.split("|||")
    
    if len(response_list) < 2:
        await interaction.response.send_message("❌ Debes proporcionar al menos 2 respuestas separadas por |||", ephemeral=True)
        return
        
    try:
        trigger = await auto_message_system.add_keyword_trigger(
            guild_id=interaction.guild.id,
            keywords=keywords,
            response=response_list[0].strip(),  # Primera respuesta como principal
            match_mode="contains",
            is_random=True,
            responses=responses
        )
        
        if trigger:
            await interaction.response.send_message(f"✅ Trigger con {len(response_list)} respuestas aleatorias añadido con ID {trigger.id}. Responderá cuando alguien escriba '{keywords}'.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ No se pudo añadir el trigger.", ephemeral=True)
            
    except Exception as e:
        log_exception("Error al añadir trigger con respuestas aleatorias", e)
        await interaction.response.send_message("❌ Ocurrió un error al añadir el trigger.", ephemeral=True)

@bot.tree.command(name="trigger_remove", description="Elimina un trigger por su ID")
@app_commands.describe(trigger_id="ID del trigger a eliminar")
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_trigger_remove(interaction: discord.Interaction, trigger_id: int):
    try:
        success = await auto_message_system.remove_keyword_trigger(trigger_id)
        
        if success:
            await interaction.response.send_message(f"✅ Trigger con ID {trigger_id} eliminado correctamente.", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ No se encontró un trigger con ID {trigger_id}.", ephemeral=True)
            
    except Exception as e:
        log_exception("Error al eliminar trigger", e)
        await interaction.response.send_message("❌ Ocurrió un error al eliminar el trigger.", ephemeral=True)

# Manejador de errores para comandos slash de mensajes automáticos y triggers
@slash_automsg_list.error
@slash_automsg_add.error
@slash_automsg_remove.error
@slash_automsg_addembed.error
@slash_automsg_random.error
@slash_trigger_list.error
@slash_trigger_add.error
@slash_trigger_addembed.error
@slash_trigger_addmode.error
@slash_trigger_random.error
@slash_trigger_remove.error
async def automsg_trigger_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message("❌ No tienes permisos suficientes para usar este comando. Se requiere permiso para gestionar el servidor.", ephemeral=True)
    else:
        log_exception("Error no manejado en comando de automsg/trigger", error)
        await interaction.response.send_message("❌ Ocurrió un error al procesar tu comando.", ephemeral=True)
# Más comandos aquí...

# Función para manejar comandos de voz recibidos
async def handle_voice_command(user_id, transcription, intent, entities):
    """Procesa comandos recibidos por voz"""
    try:
        # Obtener el guild del usuario
        guild = None
        for g in bot.guilds:
            member = g.get_member(user_id)
            if member:
                guild = g
                break
                
        if not guild:
            logger.warning(f"No se pudo encontrar guild para el usuario {user_id}")
            return
            
        # Crear un contexto similar para procesar el comando
        if intent == "reproducir_cancion":
            song_query = entities.get("song", "")
            artist = entities.get("artist", "")
            
            if artist:
                query = f"{song_query} {artist}"
            else:
                query = song_query
                
            if query:
                response = await music_player.play_song(query, user_id)
                # Enviar respuesta al canal de texto asociado con el canal de voz
                if music_player.voice_client and music_player.voice_client.channel:
                    text_channels = guild.text_channels
                    if text_channels:
                        await text_channels[0].send(f"Comando de voz: {response}")
            
        # Implementar más intenciones aquí
            
    except Exception as e:
        log_exception("Error al procesar comando de voz", e)

# Registrar instancia única de handler en el reproductor de música
voice_command_handler = VoiceCommandHandler(bot, music_player)
music_player.register_voice_command_handler(voice_command_handler)
music_player.register_voice_command_handler(voice_command_handler)