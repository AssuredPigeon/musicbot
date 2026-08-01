from tortoise import fields, models

class TicketSettings(models.Model):
    id = fields.IntField(pk=True)
    guild_id = fields.BigIntField(unique=True)
    log_channel_id = fields.BigIntField(null=True)
    ticket_limit = fields.IntField(default=1)  # Número máximo de tickets abiertos por usuario

    class Meta:
        table = "ticket_settings"

    @classmethod
    async def get_or_create_settings(cls, guild_id: int):
        settings, created = await cls.get_or_create(guild_id=guild_id)
        return settings

    @classmethod
    async def set_log_channel(cls, guild_id: int, channel_id: int):
        settings = await cls.get_or_create_settings(guild_id)
        settings.log_channel_id = channel_id
        await settings.save()

    @classmethod
    async def get_log_channel_id(cls, guild_id: int):
        settings = await cls.get_or_create_settings(guild_id)
        return settings.log_channel_id
