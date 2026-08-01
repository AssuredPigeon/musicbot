import discord
from discord.ext import commands
from discord import app_commands
import asyncio
from modules.ticket_database import create_ticket_record, get_log_channel
from modules.ticket_database import get_user_open_tickets_count
from modules.ticket_settings import TicketSettings
from utils.helpers import LegacyInteraction

class CloseTicketButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="❌ Cerrar Ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ Solo el staff puede cerrar el ticket.", ephemeral=True)
            return

        from modules.ticket_database import close_ticket

        await close_ticket(interaction.channel.id, closed_by=interaction.user.id)

        await interaction.channel.delete(reason="Ticket cerrado por el staff")



class ConfirmTicketView(discord.ui.View):
    def __init__(self, bot, user: discord.User, support_role: discord.Role, category, timeout_seconds: int):
        super().__init__(timeout=timeout_seconds)
        self.bot = bot
        self.user = user
        self.support_role = support_role
        self.category = category
        self.confirmed = False

    @discord.ui.button(label="✅ Confirmar", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            await interaction.response.send_message("❌ Este botón no es para ti.", ephemeral=True)
            return

        if not self.support_role or self.support_role not in interaction.guild.roles:
            await interaction.response.send_message(
                "⚠️ El rol de soporte especificado ya no existe en este servidor.",
                ephemeral=True
            )
            return

        self.confirmed = True
        await interaction.response.send_message("🎫 Ticket confirmado. Creando canal...", ephemeral=True)

        guild = interaction.guild
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            self.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            self.support_role: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        ticket_channel = await guild.create_text_channel(
            name=f"ticket-{self.user.name}".lower().replace(" ", "-"),
            overwrites=overwrites,
            category=self.category,
            reason="Ticket creado por usuario"
        )

        await create_ticket_record(
            user_id=self.user.id,
            guild_id=guild.id,
            channel_id=ticket_channel.id
        )

        # Notificación (opcional si quieres alertar)
        log_channel = await get_log_channel(guild)
        if log_channel:
            await log_channel.send(
                f"📩 Nuevo ticket creado: {ticket_channel.mention} por {self.user.mention}"
            )

        await ticket_channel.send(
            f"🎟️ {self.user.mention}, bienvenido a tu ticket. El equipo de soporte te atenderá pronto.",
            view=CloseTicketButton()
        )
        self.stop()

    @discord.ui.button(label="❌ Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            await interaction.response.send_message("❌ Este botón no es para ti.", ephemeral=True)
            return

        self.confirmed = False
        await interaction.response.send_message("❌ Has cancelado la creación del ticket.", ephemeral=True)
        self.stop()

    async def on_timeout(self):
        if not self.confirmed:
            try:
                await self.message.edit(content="⏰ Tiempo agotado. No se ha creado el ticket.", view=None)
            except:
                pass


class TicketPanelView(discord.ui.View):
    def __init__(self, bot, support_role: discord.Role, category: discord.CategoryChannel = None, timeout_seconds: int = 60):
        super().__init__(timeout=None)
        self.bot = bot
        self.support_role = support_role
        self.category = category
        self.timeout_seconds = timeout_seconds

    @discord.ui.button(label="🎟 Abrir Ticket", style=discord.ButtonStyle.primary, custom_id="open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):

        # Verificar límite de tickets abiertos por usuario
        ticket_limit = (await TicketSettings.get_or_create_settings(interaction.guild.id)).ticket_limit
        count = await get_user_open_tickets_count(interaction.user.id, interaction.guild.id)

        if count >= ticket_limit:
            await interaction.response.send_message(
                f"❌ Solo puedes tener {ticket_limit} ticket(s) abierto(s) al mismo tiempo.",
                ephemeral=True
            )
            return

        view = ConfirmTicketView(
            self.bot,
            user=interaction.user,
            support_role=self.support_role,
            category=self.category,
            timeout_seconds=self.timeout_seconds
        )
        await interaction.response.send_message(
            "¿Estás seguro de que deseas abrir un ticket?",
            view=view,
            ephemeral=True
        )
        view.message = await interaction.original_response()


class TicketSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="setup_ticket", description="Crea un panel para tickets con botón interactivo")
    @app_commands.describe(
        channel="Canal donde se enviará el panel",
        support_role="Rol del equipo de soporte que verá los tickets",
        category="Categoría opcional donde se crearán los tickets",
        timeout_seconds="Tiempo (segundos) que tiene el usuario para cancelar (por defecto 60)"
    )
    async def setup_ticket(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        support_role: discord.Role,
        category: discord.CategoryChannel = None,
        timeout_seconds: int = 60
    ):
        
        if support_role not in interaction.guild.roles:
            await interaction.response.send_message("❌ El rol de soporte especificado no existe.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="🎟 Sistema de Tickets",
            description="Haz clic en el botón para abrir un ticket con el staff. Tendrás unos segundos para cancelarlo.",
            color=discord.Color.blue()
        )
        view = TicketPanelView(
            self.bot,
            support_role=support_role,
            category=category,
            timeout_seconds=timeout_seconds
        )
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message("✅ Panel de tickets enviado correctamente.", ephemeral=True)

from modules import ticket_database
from modules.ticket_database import (
    create_ticket_record, close_ticket,
    get_user_open_tickets_count,
    get_ticket_by_channel,
    get_open_tickets_for_guild,
    get_user_tickets,
    generate_ticket_stats,
    get_log_channel
)
from modules.ticket_settings import TicketSettings

class TicketAdmin(commands.GroupCog, name="ticket"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    @app_commands.command(name="stats", description="📊 Ver estadísticas de tickets")
    async def stats(self, interaction: discord.Interaction):
        data = await generate_ticket_stats(interaction.guild.id)
        embed = discord.Embed(title="📈 Estadísticas de Tickets", color=discord.Color.blurple())
        embed.add_field(name="Total", value=str(data['total_tickets']))
        embed.add_field(name="Abiertos", value=str(data['open_tickets']))
        embed.add_field(name="Cerrados", value=str(data['closed_tickets']))
        embed.add_field(name="Archivados", value=str(data['archived_tickets']))
        top_users = "\n".join([f"<@{uid}>: {count}" for uid, count in data['top_users']]) or "Ninguno"
        embed.add_field(name="👤 Usuarios con más tickets", value=top_users, inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="list", description="📄 Ver tickets abiertos")
    async def list(self, interaction: discord.Interaction):
        tickets = await get_open_tickets_for_guild(interaction.guild.id)
        if not tickets:
            await interaction.response.send_message("✅ No hay tickets abiertos.", ephemeral=True)
            return
        description = "\n".join([f"`#{t.ticket_number}` <#{t.channel_id}> - <@{t.user_id}>" for t in tickets])
        embed = discord.Embed(title="📂 Tickets Abiertos", description=description, color=discord.Color.orange())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="history", description="📜 Ver historial de un usuario")
    @app_commands.describe(user="Usuario del que deseas ver el historial")
    async def history(self, interaction: discord.Interaction, user: discord.User):
        tickets = await get_user_tickets(user.id, interaction.guild.id)
        if not tickets:
            await interaction.response.send_message("Este usuario no tiene historial de tickets.", ephemeral=True)
            return
        description = "\n".join([
            f"`#{t.ticket_number}` Estado: **{t.status}** | Canal: <#{t.channel_id}> | Fecha: `{t.created_at.strftime('%Y-%m-%d')}`"
            for t in tickets
        ])
        embed = discord.Embed(title=f"📜 Historial de {user}", description=description, color=discord.Color.green())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="config", description="⚙️ Configurar el sistema de tickets")
    @app_commands.describe(log_channel="Canal donde se enviarán notificaciones de nuevos tickets")
    async def config(self, interaction: discord.Interaction, log_channel: discord.TextChannel):
        await interaction.response.send_message(f"✅ Canal de logs configurado: {log_channel.mention}", ephemeral=True)

    # Prefijo legacy (si usas command_prefix="!")
    @commands.command(name="ticketstats")
    async def ticket_stats_legacy(self, ctx):
        interaction = LegacyInteraction(ctx)
        await self.stats(interaction)

    @commands.command(name="ticketlist")
    async def ticket_list_legacy(self, ctx):
        interaction = await self.bot.get_or_fetch_interaction(ctx)
        await self.list(interaction)

    @commands.command(name="tickethistory")
    async def ticket_history_legacy(self, ctx, user: discord.User):
        interaction = await self.bot.get_or_fetch_interaction(ctx)
        await self.history(interaction, user)

    @commands.command(name="ticketconfig")
    async def ticket_config_legacy(self, ctx, log_channel: discord.TextChannel):
        await TicketSettings.set_log_channel(ctx.guild.id, log_channel.id)
        await ctx.send(f"✅ Canal de logs configurado: {log_channel.mention}")

async def setup(bot):
    await bot.add_cog(TicketSystem(bot))
    await bot.add_cog(TicketAdmin(bot))
