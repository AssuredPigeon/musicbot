import discord
from discord.ext import commands
import logging

logger = logging.getLogger('musicbot.dev_commands')

class DevCommands(commands.Cog):
    """Comandos para el desarrollo y administración del bot"""
    
    def __init__(self, bot):
        self.bot = bot
    
    @commands.command(name="sync", hidden=True)
    @commands.is_owner()  # Solo el dueño del bot puede usar este comando
    async def sync_command(self, ctx):
        """Sincroniza los comandos slash con Discord"""
        try:
            logger.info("Sincronizando comandos slash...")
            synced = await self.bot.tree.sync()
            await ctx.send(f"¡Sincronizados {len(synced)} comandos slash!")
            logger.info(f"Sincronizados {len(synced)} comandos slash")
        except Exception as e:
            logger.error(f"Error al sincronizar comandos slash: {e}")
            await ctx.send(f"Error al sincronizar: {str(e)}")
    
    @commands.command(name="guild_sync", hidden=True)
    @commands.is_owner()
    async def guild_sync(self, ctx, guild_id: int = None):
        """Sincroniza comandos slash en un servidor específico"""
        try:
            if guild_id:
                guild = discord.Object(id=guild_id)
                synced = await self.bot.tree.sync(guild=guild)
                await ctx.send(f"¡Sincronizados {len(synced)} comandos para el servidor {guild_id}!")
            else:
                # Si no se proporciona ID, usamos el servidor actual
                synced = await self.bot.tree.sync(guild=ctx.guild)
                await ctx.send(f"¡Sincronizados {len(synced)} comandos para este servidor!")
        except Exception as e:
            logger.error(f"Error al sincronizar comandos slash: {e}")
            await ctx.send(f"Error al sincronizar: {str(e)}")

def setup(bot):
    """Configuración del módulo"""
    bot.add_cog(DevCommands(bot))
    logger.info("Módulo de comandos de desarrollo cargado")