from tortoise import fields, models
from datetime import datetime

class Ticket(models.Model):
    """Modelo para el sistema de tickets"""
    id = fields.IntField(pk=True)
    user_id = fields.BigIntField()
    guild_id = fields.BigIntField()
    channel_id = fields.BigIntField()
    ticket_number = fields.IntField()
    status = fields.CharField(max_length=20, default="open")  # open, closed, archived
    issue_type = fields.CharField(max_length=50, null=True)  # tipo de problema (opcional)
    created_at = fields.DatetimeField(auto_now_add=True)
    closed_at = fields.DatetimeField(null=True)
    closed_by = fields.BigIntField(null=True)
    
    class Meta:
        table = "tickets"
    
    @classmethod
    async def get_open_tickets_count(cls, user_id, guild_id):
        """Obtiene la cantidad de tickets abiertos por un usuario en un servidor"""
        return await cls.filter(user_id=user_id, guild_id=guild_id, status="open").count()
    
    @classmethod
    async def get_next_ticket_number(cls, guild_id):
        """Obtiene el siguiente número de ticket para un servidor"""
        # Obtenemos el ticket con el mayor número para este servidor
        last_ticket = await cls.filter(guild_id=guild_id).order_by('-ticket_number').first()
        if last_ticket:
            return last_ticket.ticket_number + 1
        return 1
    
    @classmethod
    async def get_open_tickets_for_guild(cls, guild_id):
        """Obtiene todos los tickets abiertos para un servidor"""
        return await cls.filter(guild_id=guild_id, status="open")

    @classmethod
    async def archive_ticket(cls, channel_id, closed_by):
        """Marca un ticket como archivado"""
        ticket = await cls.get_or_none(channel_id=channel_id, status="open")
        if ticket:
            ticket.status = "archived"
            ticket.closed_at = datetime.now()
            ticket.closed_by = closed_by
            await ticket.save()
            return ticket
        return None
        
    @classmethod
    async def close_ticket(cls, channel_id, closed_by):
        """Marca un ticket como cerrado"""
        ticket = await cls.get_or_none(channel_id=channel_id, status="open")
        if ticket:
            ticket.status = "closed"
            ticket.closed_at = datetime.now()
            ticket.closed_by = closed_by
            await ticket.save()
            return ticket
        return None

class TicketTranscript(models.Model):
    """Modelo para guardar transcripciones de tickets"""
    id = fields.IntField(pk=True)
    ticket = fields.ForeignKeyField('models.Ticket', related_name='transcripts')
    content = fields.TextField()  # Contenido HTML o texto plano
    created_at = fields.DatetimeField(auto_now_add=True)
    
    class Meta:
        table = "ticket_transcripts"