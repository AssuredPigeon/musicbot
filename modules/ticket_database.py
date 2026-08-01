import asyncio
import logging
import discord
from datetime import datetime
from tortoise import Tortoise
from typing import List, Optional, Union
from modules.ticket_settings import TicketSettings
set_log_channel = TicketSettings.set_log_channel 

# Import your models module
from modules.ticket_models import Ticket, TicketTranscript

logger = logging.getLogger('musicbot.tickets')

async def create_ticket_record(user_id: int, guild_id: int, channel_id: int, issue_type: str = None) -> Optional[Ticket]:
    """
    Crea un nuevo registro de ticket en la base de datos
    
    Args:
        user_id: ID del usuario que creó el ticket
        guild_id: ID del servidor
        channel_id: ID del canal del ticket
        issue_type: Tipo de problema (opcional)
        
    Returns:
        Instancia del ticket creado o None si falló
    """
    try:
        # Obtener el siguiente número de ticket
        ticket_number = await Ticket.get_next_ticket_number(guild_id)
        
        # Crear el ticket
        ticket = await Ticket.create(
            user_id=user_id,
            guild_id=guild_id,
            channel_id=channel_id,
            ticket_number=ticket_number,
            issue_type=issue_type,
            status="open"
        )
        
        logger.info(f"Ticket #{ticket_number} creado por usuario {user_id} en servidor {guild_id}")
        return ticket
    except Exception as e:
        logger.error(f"Error al crear ticket: {str(e)}")
        return None

async def get_user_open_tickets_count(user_id: int, guild_id: int) -> int:
    """
    Obtiene la cantidad de tickets abiertos por un usuario en un servidor
    
    Args:
        user_id: ID del usuario
        guild_id: ID del servidor
        
    Returns:
        Número de tickets abiertos del usuario
    """
    try:
        return await Ticket.get_open_tickets_count(user_id, guild_id)
    except Exception as e:
        logger.error(f"Error al obtener tickets abiertos: {str(e)}")
        return 0

async def close_ticket(channel_id: int, closed_by: int) -> Optional[Ticket]:
    """
    Cierra un ticket existente
    
    Args:
        channel_id: ID del canal del ticket
        closed_by: ID del usuario que cerró el ticket
        
    Returns:
        Instancia del ticket cerrado o None si no se encontró
    """
    try:
        ticket = await Ticket.close_ticket(channel_id, closed_by)
        if ticket:
            logger.info(f"Ticket #{ticket.ticket_number} cerrado por usuario {closed_by}")
        return ticket
    except Exception as e:
        logger.error(f"Error al cerrar ticket: {str(e)}")
        return None

async def archive_ticket(channel_id: int, closed_by: int) -> Optional[Ticket]:
    """
    Archiva un ticket existente (alternativa a eliminar el canal)
    
    Args:
        channel_id: ID del canal del ticket
        closed_by: ID del usuario que archivó el ticket
        
    Returns:
        Instancia del ticket archivado o None si no se encontró
    """
    try:
        ticket = await Ticket.archive_ticket(channel_id, closed_by)
        if ticket:
            logger.info(f"Ticket #{ticket.ticket_number} archivado por usuario {closed_by}")
        return ticket
    except Exception as e:
        logger.error(f"Error al archivar ticket: {str(e)}")
        return None

async def save_ticket_transcript(ticket_id: int, content: str) -> Optional[TicketTranscript]:
    """
    Guarda la transcripción de un ticket
    
    Args:
        ticket_id: ID del ticket
        content: Contenido de la transcripción (HTML o texto)
        
    Returns:
        Instancia de la transcripción creada o None si falló
    """
    try:
        ticket = await Ticket.get_or_none(id=ticket_id)
        if not ticket:
            logger.error(f"No se encontró el ticket con ID {ticket_id}")
            return None
            
        transcript = await TicketTranscript.create(
            ticket=ticket,
            content=content
        )
        
        logger.info(f"Transcripción guardada para ticket #{ticket.ticket_number}")
        return transcript
    except Exception as e:
        logger.error(f"Error al guardar transcripción: {str(e)}")
        return None

async def get_ticket_by_channel(channel_id: int) -> Optional[Ticket]:
    """
    Obtiene un ticket por su canal
    
    Args:
        channel_id: ID del canal del ticket
        
    Returns:
        Instancia del ticket o None si no se encontró
    """
    try:
        return await Ticket.get_or_none(channel_id=channel_id)
    except Exception as e:
        logger.error(f"Error al obtener ticket por canal: {str(e)}")
        return None

async def get_open_tickets_for_guild(guild_id: int) -> List[Ticket]:
    """
    Obtiene todos los tickets abiertos para un servidor
    
    Args:
        guild_id: ID del servidor
        
    Returns:
        Lista de tickets abiertos
    """
    try:
        return await Ticket.get_open_tickets_for_guild(guild_id)
    except Exception as e:
        logger.error(f"Error al obtener tickets abiertos para servidor: {str(e)}")
        return []

async def get_user_tickets(user_id: int, guild_id: int, status: str = None) -> List[Ticket]:
    """
    Obtiene los tickets de un usuario
    
    Args:
        user_id: ID del usuario
        guild_id: ID del servidor
        status: Estado de los tickets a filtrar (opcional)
        
    Returns:
        Lista de tickets del usuario
    """
    try:
        query = Ticket.filter(user_id=user_id, guild_id=guild_id)
        if status:
            query = query.filter(status=status)
        return await query.order_by('-created_at')
    except Exception as e:
        logger.error(f"Error al obtener tickets del usuario: {str(e)}")
        return []

async def generate_ticket_stats(guild_id: int) -> dict:
    """
    Genera estadísticas de tickets para un servidor
    
    Args:
        guild_id: ID del servidor
        
    Returns:
        Diccionario con estadísticas
    """
    try:
        total_tickets = await Ticket.filter(guild_id=guild_id).count()
        open_tickets = await Ticket.filter(guild_id=guild_id, status="open").count()
        closed_tickets = await Ticket.filter(guild_id=guild_id, status="closed").count()
        archived_tickets = await Ticket.filter(guild_id=guild_id, status="archived").count()
        
        # Obtener los usuarios con más tickets
        all_tickets = await Ticket.filter(guild_id=guild_id)
        
        user_counts = {}
        for ticket in all_tickets:
            if ticket.user_id not in user_counts:
                user_counts[ticket.user_id] = 0
            user_counts[ticket.user_id] += 1
        
        top_users = sorted(user_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        
        return {
            "total_tickets": total_tickets,
            "open_tickets": open_tickets,
            "closed_tickets": closed_tickets,
            "archived_tickets": archived_tickets,
            "top_users": top_users
        }
    except Exception as e:
        logger.error(f"Error al generar estadísticas: {str(e)}")
        return {
            "total_tickets": 0,
            "open_tickets": 0,
            "closed_tickets": 0,
            "archived_tickets": 0,
            "top_users": []
        }

async def get_log_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    log_channel_id = await TicketSettings.get_log_channel_id(guild.id)
    if log_channel_id:
        return guild.get_channel(log_channel_id)
    return None
