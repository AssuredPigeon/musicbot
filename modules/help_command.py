import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional, List, Dict, Any, Union
import logging
import asyncio

logger = logging.getLogger('musicbot.help_command')

class CategorySelect(discord.ui.Select):
    """Menú desplegable para seleccionar categorías de comandos"""
    
    def __init__(self, categories, help_view):
        self.help_view = help_view
        
        # Crear opciones para el menú desplegable
        options = [
            discord.SelectOption(
                label="Inicio",
                description="Página principal de ayuda",
                emoji="🏠",
                value="home"
            )
        ]
        
        # Agregar cada categoría como opción
        for cat_name, cat_data in categories.items():
            # Omitir categorías sin comandos
            if not cat_data.get('commands'):
                continue
                
            # Obtener emoji personalizado o usar uno por defecto
            emoji = cat_data.get('emoji', '📁')
            
            options.append(
                discord.SelectOption(
                    label=cat_name,
                    description=cat_data.get('description', f"Comandos de {cat_name}"),
                    emoji=emoji,
                    value=cat_name
                )
            )
            
        super().__init__(
            placeholder="Selecciona una categoría",
            min_values=1,
            max_values=1,
            options=options
        )
    
    async def callback(self, interaction: discord.Interaction):
        # Obtener categoría seleccionada
        selected = self.values[0]
        
        # Actualizar vista con la categoría seleccionada
        await self.help_view.show_category(selected, interaction)


class HelpView(discord.ui.View):
    """Vista interactiva para navegar por las categorías de ayuda"""
    
    def __init__(self, help_command, timeout=180):
        super().__init__(timeout=timeout)
        self.help_command = help_command
        self.context = help_command.context
        self.bot = help_command.context.bot
        self.current_page = 0
        self.current_category = "home"
        self.message = None
        
        # Organizar comandos por categorías
        self.categories = self.organize_commands()
        
        # Agregar selector de categorías
        self.add_item(CategorySelect(self.categories, self))
        
        # Botones para navegar entre páginas
        self.prev_button = discord.ui.Button(emoji="⬅️", style=discord.ButtonStyle.secondary, disabled=True)
        self.next_button = discord.ui.Button(emoji="➡️", style=discord.ButtonStyle.secondary, disabled=True)
        self.prev_button.callback = self.previous_page
        self.next_button.callback = self.next_page
        
        self.add_item(self.prev_button)
        self.add_item(self.next_button)
    
    def organize_commands(self) -> Dict[str, Dict[str, Any]]:
        """Organiza los comandos por categorías"""
        categories = {}
        
        # Emoji por defecto para cada categoría
        default_emojis = {
            "Music": "🎵",
            "Configuration": "⚙️",
            "Welcome": "👋",
            "Auto Role": "🔄",
            "General": "🔍",
            "Admin": "🛡️",
            "Fun": "🎮",
            "Utility": "🔧",
            "Acción": "👊",
            "Anime": "🎌",
            "Club": "🏢",
            "Configuración": "⚙️",
            "Economía": "💰",
            "Diversión": "🎲",
            "No Category": "📁"
        }
        
        # Ordenar comandos por categoría
        for command in self.bot.commands:
            # Ignorar comandos ocultos
            if command.hidden:
                continue
                
            # Determinar categoría
            category = command.cog_name if command.cog_name else "No Category"
            
            # Inicializar categoría si no existe
            if category not in categories:
                categories[category] = {
                    'description': f"Comandos de {category}",
                    'emoji': default_emojis.get(category, "📁"),
                    'commands': []
                }
            
            # Agregar comando a la categoría
            categories[category]['commands'].append(command)
        
        return categories
    
    async def show_category(self, category: str, interaction: discord.Interaction):
        """Muestra una categoría específica de comandos"""
        self.current_category = category
        self.current_page = 0
        
        # Generar embed para la categoría
        embed = await self.get_category_embed(category)
        
        # Actualizar botones de navegación
        self.update_navigation_buttons()
        
        # Responder a la interacción
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def get_category_embed(self, category: str) -> discord.Embed:
        """Genera un embed para la categoría especificada"""
        if category == "home":
            # Embed para la página principal
            embed = discord.Embed(
                title="📚 Comandos Keyo",
                description="Bienvenido al sistema de ayuda. Selecciona una categoría para ver los comandos disponibles.",
                color=discord.Color.purple()
            )
            
            # Mostrar estadísticas generales
            total_commands = sum(len(cat_data['commands']) for cat_name, cat_data in self.categories.items())
            embed.add_field(
                name="» Menú Help",
                value=f"Tenemos {len(self.categories)} categorías y {total_commands} comandos para explorar.\n",
                inline=False
            )
            
            # Mostrar cómo usar el comando help
            embed.add_field(
                name="Lista de comandos:",
                value=f"!help <categoria>",
                inline=True
            )
            
            embed.add_field(
                name="Comando en detalle:",
                value=f"!help <comando>",
                inline=True
            )
            
            # Mostrar categorías disponibles
            embed.add_field(
                name="» Categorías",
                value="\n".join([
                    f"!help {cat_name.lower()} :: {cat_data.get('emoji', '📁')} {cat_name}"
                    for cat_name, cat_data in self.categories.items()
                    if cat_data.get('commands')
                ]),
                inline=False
            )
        else:
            # Embed para una categoría específica
            cat_data = self.categories.get(category, {})
            commands = cat_data.get('commands', [])
            
            embed = discord.Embed(
                title=f"{cat_data.get('emoji', '📁')} {category}",
                description=cat_data.get('description', f"Comandos de {category}"),
                color=discord.Color.purple()
            )
            
            # Mostrar comandos de la categoría
            if not commands:
                embed.add_field(name="Sin comandos", value="Esta categoría no tiene comandos disponibles.")
            else:
                # Paginar si hay muchos comandos
                items_per_page = 10
                pages = (len(commands) + items_per_page - 1) // items_per_page
                
                # Actualizar información de página
                if pages > 1:
                    embed.set_footer(text=f"Página {self.current_page + 1}/{pages}")
                
                # Obtener comandos para la página actual
                start_idx = self.current_page * items_per_page
                end_idx = min(start_idx + items_per_page, len(commands))
                page_commands = commands[start_idx:end_idx]
                
                # Mostrar cada comando
                for cmd in page_commands:
                    # Obtener resumen de ayuda
                    brief = cmd.brief or cmd.help
                    if brief:
                        brief = brief.split('\n')[0]  # Solo la primera línea
                    else:
                        brief = "Sin descripción"
                    
                    embed.add_field(
                        name=f"{self.context.clean_prefix}{cmd.name}",
                        value=f"{brief}",
                        inline=False
                    )
        
        return embed
    
    def update_navigation_buttons(self):
        """Actualiza el estado de los botones de navegación"""
        if self.current_category == "home":
            # No hay navegación en la página principal
            self.prev_button.disabled = True
            self.next_button.disabled = True
            return
            
        # Obtener comandos de la categoría actual
        commands = self.categories.get(self.current_category, {}).get('commands', [])
        
        # Calcular páginas
        items_per_page = 10
        pages = (len(commands) + items_per_page - 1) // items_per_page
        
        # Actualizar estado de botones
        self.prev_button.disabled = (self.current_page <= 0)
        self.next_button.disabled = (self.current_page >= pages - 1)
    
    async def previous_page(self, interaction: discord.Interaction):
        """Navega a la página anterior"""
        self.current_page = max(0, self.current_page - 1)
        embed = await self.get_category_embed(self.current_category)
        self.update_navigation_buttons()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def next_page(self, interaction: discord.Interaction):
        """Navega a la página siguiente"""
        # Obtener comandos y calcular páginas
        commands = self.categories.get(self.current_category, {}).get('commands', [])
        items_per_page = 10
        pages = (len(commands) + items_per_page - 1) // items_per_page
        
        self.current_page = min(pages - 1, self.current_page + 1)
        embed = await self.get_category_embed(self.current_category)
        self.update_navigation_buttons()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def on_timeout(self):
        """Desactiva los botones cuando el tiempo expira"""
        # Desactivar todos los ítems
        for item in self.children:
            item.disabled = True
        
        # Actualizar mensaje si aún existe
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                pass


class EnhancedHelpCommand(commands.HelpCommand):
    """Comando de ayuda personalizado con categorías y navegación"""
    
    def __init__(self, **options):
        super().__init__(**options)
    
    async def send_bot_help(self, mapping):
        """Envía la ayuda general del bot"""
        view = HelpView(self)
        embed = await view.get_category_embed("home")
        
        # Enviar mensaje con la vista
        view.message = await self.get_destination().send(embed=embed, view=view)
    
    async def send_command_help(self, command):
        """Envía ayuda para un comando específico"""
        embed = discord.Embed(
            title=f"Ayuda: {self.context.clean_prefix}{command.qualified_name}",
            description=command.help or "No hay descripción disponible.",
            color=discord.Color.purple()
        )
        
        # Mostrar sintaxis del comando
        embed.add_field(
            name="Sintaxis",
            value=f"`{self.context.clean_prefix}{command.qualified_name} {command.signature}`",
            inline=False
        )
        
        # Mostrar alias si existen
        if command.aliases:
            embed.add_field(
                name="Alias",
                value=", ".join(f"`{alias}`" for alias in command.aliases),
                inline=False
            )
        
        # Mostrar permisos requeridos
        checks = []
        for check in command.checks:
            if hasattr(check, "__qualname__"):
                check_name = check.__qualname__
                if "has_permissions" in check_name or "guild_only" in check_name:
                    checks.append(check_name)
        
        if checks:
            embed.add_field(
                name="Permisos Requeridos",
                value="\n".join(checks),
                inline=False
            )
        
        # Mostrar categoría
        if command.cog_name:
            embed.add_field(
                name="Categoría",
                value=command.cog_name,
                inline=False
            )
        
        await self.get_destination().send(embed=embed)
    
    async def send_group_help(self, group):
        """Envía ayuda para un grupo de comandos"""
        embed = discord.Embed(
            title=f"Ayuda: {self.context.clean_prefix}{group.qualified_name}",
            description=group.help or "Grupo de comandos sin descripción.",
            color=discord.Color.purple()
        )
        
        # Mostrar sintaxis del grupo
        embed.add_field(
            name="Sintaxis",
            value=f"`{self.context.clean_prefix}{group.qualified_name} {group.signature}`",
            inline=False
        )
        
        # Mostrar subcomandos
        if isinstance(group, commands.Group):
            subcommands = sorted(group.commands, key=lambda x: x.name)
            if subcommands:
                subcommands_text = ""
                for subcmd in subcommands:
                    brief = subcmd.brief or subcmd.help
                    if brief:
                        brief = brief.split('\n')[0]  # Solo la primera línea
                    else:
                        brief = "Sin descripción"
                    
                    subcommands_text += f"`{self.context.clean_prefix}{subcmd.qualified_name}` - {brief}\n"
                
                embed.add_field(
                    name="Subcomandos",
                    value=subcommands_text,
                    inline=False
                )
        
        # Mostrar alias si existen
        if group.aliases:
            embed.add_field(
                name="Alias",
                value=", ".join(f"`{alias}`" for alias in group.aliases),
                inline=False
            )
        
        # Mostrar categoría
        if group.cog_name:
            embed.add_field(
                name="Categoría",
                value=group.cog_name,
                inline=False
            )
        
        await self.get_destination().send(embed=embed)
    
    async def send_cog_help(self, cog):
        """Envía ayuda para un cog específico"""
        # Crear una vista con la categoría seleccionada
        view = HelpView(self)
        
        # Mostrar la categoría correspondiente al cog
        category = cog.qualified_name
        embed = await view.get_category_embed(category)
        
        # Enviar mensaje con la vista
        view.current_category = category
        view.update_navigation_buttons()
        view.message = await self.get_destination().send(embed=embed, view=view)


# Clase para manejar el comando slash de ayuda
class SlashHelpHandler:
    def __init__(self, bot):
        self.bot = bot


def setup(bot):
    """Configura el comando de ayuda personalizado y registra los comandos slash"""
    # Configurar el comando de ayuda personalizado
    bot.help_command = EnhancedHelpCommand(command_attrs={
        'help': 'Muestra este mensaje de ayuda',
        'brief': 'Muestra información de ayuda'
    })
    
    # Crear manejador para comandos slash
    slash_help_handler = SlashHelpHandler(bot)
    
    # Registrar comando slash global
    @bot.tree.command(name="help", description="Muestra la ayuda del bot")
    async def slash_help(interaction: discord.Interaction):
        """Comando slash básico para mostrar ayuda"""
        try:
            # Crear un contexto simulado para el comando de ayuda
            ctx = await bot.get_context(interaction)
            bot.help_command.context = ctx
            
            # Crear una vista para la ayuda
            view = HelpView(bot.help_command)
            embed = await view.get_category_embed("home")
            
            # Responder directamente a la interacción con el embed y la vista
            await interaction.response.send_message(embed=embed, view=view)
            
            # Guardar referencia al mensaje para la vista
            response = await interaction.original_response()
            view.message = response
        except Exception as e:
            logger.error(f"Error al mostrar ayuda: {e}")
            # Si aún no se ha respondido a la interacción
            if not interaction.response.is_done():
                await interaction.response.send_message("Ocurrió un error al mostrar la ayuda", ephemeral=True)
            else:
                await interaction.followup.send("Ocurrió un error al mostrar la ayuda", ephemeral=True)
    
    logger.info("Sistema de ayuda configurado correctamente")
    return slash_help_handler