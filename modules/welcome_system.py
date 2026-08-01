from tortoise import fields, models
import json
import discord
from discord import app_commands
from discord.ext import commands
import logging

# Configurar logger
logger = logging.getLogger('musicbot.welcome_system')

class WelcomeConfig(models.Model):
    """Modelo para la configuración de mensajes de bienvenida"""
    id = fields.IntField(pk=True)
    guild_id = fields.IntField(unique=True)
    channel_id = fields.IntField(null=True)
    enabled = fields.BooleanField(default=True)
    message = fields.TextField(null=True)
    embed_title = fields.TextField(default="¡Bienvenido/a!")
    embed_color = fields.IntField(default=3066993)  # Verde por defecto
    embed_footer = fields.TextField(null=True)
    embed_enabled = fields.BooleanField(default=True)
    
    @classmethod
    async def get_for_guild(cls, guild_id):
        """Obtiene la configuración de bienvenida para un servidor"""
        config = await cls.filter(guild_id=guild_id).first()
        if not config:
            config = await cls.create(
                guild_id=guild_id,
                message="¡Bienvenido/a {user_mention} a **{server_name}**! Esperamos que disfrutes tu estancia.",
                embed_footer="Miembro #{member_count}"
            )
        return config

class AutoRoleConfig(models.Model):
    """Modelo para la configuración de roles automáticos"""
    id = fields.IntField(pk=True)
    guild_id = fields.IntField()
    role_id = fields.IntField()
    enabled = fields.BooleanField(default=True)
    is_join_role = fields.BooleanField(default=True)  # Asignar al unirse
    
    class Meta:
        unique_together = (("guild_id", "role_id"),)
    
    @classmethod
    async def get_join_roles(cls, guild_id):
        """Obtiene los roles a aplicar cuando un usuario se une"""
        return await cls.filter(guild_id=guild_id, enabled=True, is_join_role=True)


class WelcomeSystem(commands.Cog, name="Welcome"):  # Añadido nombre explícito al Cog
    """Sistema de bienvenida para nuevos miembros del servidor"""  # Añadida descripción del Cog

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member):
        """Se ejecuta cuando un miembro se une al servidor"""
        # Procesar mensaje de bienvenida
        await self._send_welcome_message(member)
        # Asignar roles automáticos
        await self._assign_auto_roles(member)
    
    async def _send_welcome_message(self, member):
        """Envía el mensaje de bienvenida según la configuración"""
        try:
            # Obtener configuración
            config = await WelcomeConfig.get_for_guild(member.guild.id)
            
            if not config.enabled or not config.channel_id:
                return
            
            # Obtener el canal
            channel = member.guild.get_channel(config.channel_id)
            if not channel:
                logger.warning(f"Canal de bienvenida {config.channel_id} no encontrado en {member.guild.name}")
                return
            
            # Preparar mensaje
            message = self._format_welcome_message(config.message, member)
            
            # Enviar mensaje
            if config.embed_enabled:
                embed = discord.Embed(
                    title=self._format_welcome_message(config.embed_title, member),
                    description=message,
                    color=config.embed_color
                )
                
                # Añadir avatar del usuario
                embed.set_thumbnail(url=member.display_avatar.url)
                
                # Añadir pie de página si existe
                if config.embed_footer:
                    embed.set_footer(text=self._format_welcome_message(config.embed_footer, member))
                
                await channel.send(embed=embed)
            else:
                await channel.send(message)
                
            logger.info(f"Mensaje de bienvenida enviado para {member.name} en {member.guild.name}")
            
        except Exception as e:
            logger.error(f"Error al enviar mensaje de bienvenida: {str(e)}")
    
    def _format_welcome_message(self, message, member):
        """Formatea el mensaje con variables personalizadas"""
        return message.replace("{user_name}", member.name)\
                     .replace("{user_mention}", member.mention)\
                     .replace("{user_id}", str(member.id))\
                     .replace("{server_name}", member.guild.name)\
                     .replace("{member_count}", str(member.guild.member_count))
    
    async def _assign_auto_roles(self, member):
        """Asigna roles automáticos al miembro"""
        try:
            # Obtener roles configurados para asignación automática
            auto_roles = await AutoRoleConfig.get_join_roles(member.guild.id)
            
            # Asignar cada rol encontrado
            for auto_role in auto_roles:
                role = member.guild.get_role(auto_role.role_id)
                if role:
                    await member.add_roles(role)
                    logger.info(f"Rol automático '{role.name}' asignado a {member.name} en {member.guild.name}")
                else:
                    logger.warning(f"Rol {auto_role.role_id} no encontrado en {member.guild.name}")
                    
        except Exception as e:
            logger.error(f"Error al asignar roles automáticos: {str(e)}")


    @commands.group(name="welcome", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def welcome(self, ctx):
        """Administra el sistema de mensajes de bienvenida
        
        Este comando permite configurar cómo funcionan los mensajes de bienvenida
        para nuevos miembros. Usa los subcomandos para gestionar la configuración.
        """
        # Mostrar ayuda si no se proporciona subcomando
        config = await WelcomeConfig.get_for_guild(ctx.guild.id)
        embed = discord.Embed(
            title="🏠 Configuración de Bienvenida",
            description="Usa los subcomandos para gestionar la configuración de bienvenida.",
            color=discord.Color.blue()
        )
        
        # Mostrar estado actual
        channel = ctx.guild.get_channel(config.channel_id) if config.channel_id else None
        embed.add_field(
            name="Estado", 
            value=f"{'✅ Activado' if config.enabled else '❌ Desactivado'}", 
            inline=True
        )
        embed.add_field(
            name="Canal", 
            value=f"{channel.mention if channel else 'No configurado'}", 
            inline=True
        )
        embed.add_field(
            name="Usar Embed",
            value=f"{'✅ Sí' if config.embed_enabled else '❌ No'}", 
            inline=True
        )
        
        embed.add_field(
            name="Mensaje", 
            value=f"```{config.message[:1000]}```", 
            inline=False
        )
        
        embed.add_field(
            name="Subcomandos Disponibles", 
            value="`channel`, `message`, `toggle`, `embed`, `test`", 
            inline=False
        )
        
        await ctx.send(embed=embed)
    
    @welcome.command(name="channel")
    @commands.has_permissions(manage_guild=True)
    async def welcome_channel(self, ctx, channel: discord.TextChannel = None):
        """Establece el canal para los mensajes de bienvenida
        
        Si no se especifica un canal, se mostrará el canal actual.
        """
        config = await WelcomeConfig.get_for_guild(ctx.guild.id)
        
        if not channel:
            current_channel = ctx.guild.get_channel(config.channel_id) if config.channel_id else None
            await ctx.send(f"El canal de bienvenida actual es: {current_channel.mention if current_channel else 'No configurado'}")
            return
            
        # Actualizar canal
        config.channel_id = channel.id
        await config.save()
        
        await ctx.send(f"✅ Canal de bienvenida establecido a {channel.mention}")
    
    @welcome.command(name="message")
    @commands.has_permissions(manage_guild=True)
    async def welcome_message(self, ctx, *, message=None):
        """Establece el mensaje de bienvenida
        
        Variables disponibles:
        {user_name} - Nombre del usuario
        {user_mention} - Mención del usuario
        {server_name} - Nombre del servidor
        {member_count} - Número de miembros
        """
        config = await WelcomeConfig.get_for_guild(ctx.guild.id)
        
        if not message:
            await ctx.send(f"El mensaje actual es:\n```{config.message}```")
            return
            
        # Actualizar mensaje
        config.message = message
        await config.save()
        
        await ctx.send(f"✅ Mensaje de bienvenida actualizado")
    
    @welcome.command(name="toggle")
    @commands.has_permissions(manage_guild=True)
    async def welcome_toggle(self, ctx):
        """Activa o desactiva los mensajes de bienvenida"""
        config = await WelcomeConfig.get_for_guild(ctx.guild.id)
        
        # Invertir estado
        config.enabled = not config.enabled
        await config.save()
        
        await ctx.send(f"{'✅ Mensajes de bienvenida activados' if config.enabled else '❌ Mensajes de bienvenida desactivados'}")

    @welcome.command(name="embed")
    @commands.has_permissions(manage_guild=True)
    async def welcome_embed(self, ctx, option=None, *, value=None):
        """Configura el embed de bienvenida
        
        Opciones:
        toggle - Activa/desactiva el uso de embeds
        title - Configura el título
        color - Cambia el color (formato: #HEXCOLOR)
        footer - Configura el pie de página
        """
        config = await WelcomeConfig.get_for_guild(ctx.guild.id)
        
        if not option:
            embed = discord.Embed(
                title="Configuración de Embed de Bienvenida",
                description="Usa los parámetros para configurar el embed",
                color=config.embed_color
            )
            embed.add_field(name="Estado", value=f"{'Activado' if config.embed_enabled else 'Desactivado'}", inline=True)
            embed.add_field(name="Título", value=config.embed_title, inline=True)
            embed.add_field(name="Color", value=f"#{config.embed_color:06x}", inline=True)
            embed.add_field(name="Footer", value=config.embed_footer or "No configurado", inline=False)
            
            await ctx.send(embed=embed)
            return
            
        if option == "toggle":
            config.embed_enabled = not config.embed_enabled
            await config.save()
            await ctx.send(f"{'✅ Embed activado' if config.embed_enabled else '❌ Embed desactivado'}")
            
        elif option == "title" and value:
            config.embed_title = value
            await config.save()
            await ctx.send(f"✅ Título del embed actualizado")
            
        elif option == "color" and value:
            try:
                if value.startswith('#'):
                    value = value[1:]
                color = int(value, 16)
                config.embed_color = color
                await config.save()
                
                # Mostrar color actual
                color_embed = discord.Embed(
                    title="Color actualizado",
                    description=f"Nuevo color: #{color:06x}",
                    color=color
                )
                await ctx.send(embed=color_embed)
            except ValueError:
                await ctx.send("❌ El color debe estar en formato hexadecimal (ejemplo: #FF0000)")
                
        elif option == "footer" and value:
            config.embed_footer = value
            await config.save()
            await ctx.send(f"✅ Pie de página del embed actualizado")
            
        else:
            await ctx.send("❌ Opción no válida. Usa `toggle`, `title`, `color` o `footer`.")
    
    @welcome.command(name="test")
    @commands.has_permissions(manage_guild=True)
    async def welcome_test(self, ctx):
        """Envía un mensaje de prueba con la configuración actual"""
        # Usar el miembro actual para probar
        await self._send_welcome_message(ctx.author)
        await ctx.send("✅ Mensaje de prueba enviado")

    @commands.group(name="autorole", invoke_without_command=True)
    @commands.has_permissions(manage_roles=True)
    async def autorole(self, ctx):
        """Administra los roles automáticos
        
        Este comando permite configurar los roles que se
        asignan automáticamente a los nuevos miembros.
        """
        # Mostrar roles actuales
        roles = await AutoRoleConfig.filter(guild_id=ctx.guild.id)
        
        embed = discord.Embed(
            title="🔄 Roles Automáticos",
            description="Roles que se asignan automáticamente a los nuevos miembros.",
            color=discord.Color.blue()
        )
        
        if not roles:
            embed.add_field(
                name="Roles Configurados",
                value="No hay roles automáticos configurados."
            )
        else:
            value = ""
            for role_config in roles:
                role = ctx.guild.get_role(role_config.role_id)
                role_name = role.name if role else f"(Rol eliminado: {role_config.role_id})"
                status = "✅ Activo" if role_config.enabled else "❌ Inactivo"
                
                value += f"{role_name} - {status}\n"
            
            embed.add_field(
                name="Roles Configurados",
                value=value,
                inline=False
            )
        
        embed.add_field(
            name="Subcomandos Disponibles", 
            value="`add`, `remove`, `toggle`", 
            inline=False
        )
        
        await ctx.send(embed=embed)
    
    @autorole.command(name="add")
    @commands.has_permissions(manage_roles=True)
    async def autorole_add(self, ctx, role: discord.Role):
        """Añade un rol a la lista de roles automáticos"""
        # Verificar permisos
        if role >= ctx.guild.me.top_role:
            await ctx.send("❌ No puedo asignar ese rol porque está por encima de mi rol más alto.")
            return
            
        # Comprobar si ya existe
        existing = await AutoRoleConfig.filter(guild_id=ctx.guild.id, role_id=role.id).first()
        if existing:
            # Si existe pero está desactivado, activarlo
            if not existing.enabled:
                existing.enabled = True
                await existing.save()
                await ctx.send(f"✅ El rol {role.mention} ya estaba en la lista pero estaba desactivado. Ha sido activado.")
            else:
                await ctx.send(f"❌ El rol {role.mention} ya está en la lista de roles automáticos.")
            return
            
        # Crear nuevo rol automático
        await AutoRoleConfig.create(
            guild_id=ctx.guild.id,
            role_id=role.id,
            enabled=True,
            is_join_role=True
        )
        
        await ctx.send(f"✅ El rol {role.mention} será asignado automáticamente a los nuevos miembros.")
    
    @autorole.command(name="remove")
    @commands.has_permissions(manage_roles=True)
    async def autorole_remove(self, ctx, role: discord.Role):
        """Elimina un rol de la lista de roles automáticos"""
        # Eliminar de la base de datos
        deleted = await AutoRoleConfig.filter(guild_id=ctx.guild.id, role_id=role.id).delete()
        
        if deleted:
            await ctx.send(f"✅ El rol {role.mention} ya no será asignado automáticamente.")
        else:
            await ctx.send(f"❌ El rol {role.mention} no estaba en la lista de roles automáticos.")
    
    @autorole.command(name="toggle")
    @commands.has_permissions(manage_roles=True)
    async def autorole_toggle(self, ctx, role: discord.Role):
        """Activa o desactiva la asignación automática de un rol"""
        # Buscar configuración
        config = await AutoRoleConfig.filter(guild_id=ctx.guild.id, role_id=role.id).first()
        
        if not config:
            await ctx.send(f"❌ El rol {role.mention} no está en la lista de roles automáticos.")
            return
            
        # Invertir estado
        config.enabled = not config.enabled
        await config.save()
        
        await ctx.send(f"{'✅ Asignación automática activada' if config.enabled else '❌ Asignación automática desactivada'} para el rol {role.mention}.")

    # Comandos Slash para la gestión de bienvenidas
    welcome_group = app_commands.Group(name='welcome', description='Administra el sistema de bienvenida')
    
    @welcome_group.command(name='status', description='Ver la configuración actual de bienvenida')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_status_slash(self, interaction: discord.Interaction):
        config = await WelcomeConfig.get_for_guild(interaction.guild_id)
        embed = discord.Embed(
            title="🏠 Configuración de Bienvenida",
            description="Configuración actual del sistema de bienvenida.",
            color=discord.Color.blue()
        )
        
        # Mostrar estado actual
        channel = interaction.guild.get_channel(config.channel_id) if config.channel_id else None
        embed.add_field(
            name="Estado", 
            value=f"{'✅ Activado' if config.enabled else '❌ Desactivado'}", 
            inline=True
        )
        embed.add_field(
            name="Canal", 
            value=f"{channel.mention if channel else 'No configurado'}", 
            inline=True
        )
        embed.add_field(
            name="Usar Embed", 
            value=f"{'✅ Sí' if config.embed_enabled else '❌ No'}", 
            inline=True
        )
        
        embed.add_field(
            name="Mensaje", 
            value=f"```{config.message[:1000]}```", 
            inline=False
        )
        
        await interaction.response.send_message(embed=embed)
    
    @welcome_group.command(name='channel', description='Establece el canal para los mensajes de bienvenida')
    @app_commands.describe(channel='El canal donde se enviarán los mensajes de bienvenida')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_channel_slash(self, interaction: discord.Interaction, channel: discord.TextChannel):
        config = await WelcomeConfig.get_for_guild(interaction.guild_id)
        config.channel_id = channel.id
        await config.save()
        await interaction.response.send_message(f"✅ Canal de bienvenida establecido a {channel.mention}")
    
    @welcome_group.command(name='toggle', description='Activa o desactiva los mensajes de bienvenida')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_toggle_slash(self, interaction: discord.Interaction):
        config = await WelcomeConfig.get_for_guild(interaction.guild_id)
        config.enabled = not config.enabled
        await config.save()
        await interaction.response.send_message(
            f"{'✅ Mensajes de bienvenida activados' if config.enabled else '❌ Mensajes de bienvenida desactivados'}"
        )
    
    @welcome_group.command(name='message', description='Establece el mensaje de bienvenida')
    @app_commands.describe(message='El mensaje de bienvenida. Puedes usar {user_name}, {user_mention}, {server_name}, {member_count}')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_message_slash(self, interaction: discord.Interaction, message: str):
        config = await WelcomeConfig.get_for_guild(interaction.guild_id)
        config.message = message
        await config.save()
        await interaction.response.send_message(f"✅ Mensaje de bienvenida actualizado")
    
    @welcome_group.command(name='test', description='Envía un mensaje de prueba con la configuración actual')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_test_slash(self, interaction: discord.Interaction):
        await interaction.response.send_message("✅ Enviando mensaje de prueba...")
        await self._send_welcome_message(interaction.user)
    
    # Comandos Slash para la gestión de roles automáticos
    autorole_group = app_commands.Group(name='autorole', description='Administra los roles automáticos')
    
    @autorole_group.command(name='list', description='Muestra los roles automáticos configurados')
    @app_commands.checks.has_permissions(manage_roles=True)
    async def autorole_list_slash(self, interaction: discord.Interaction):
        roles = await AutoRoleConfig.filter(guild_id=interaction.guild_id)
        
        embed = discord.Embed(
            title="🔄 Roles Automáticos",
            description="Roles que se asignan automáticamente a los nuevos miembros.",
            color=discord.Color.blue()
        )
        
        if not roles:
            embed.add_field(
                name="Roles Configurados",
                value="No hay roles automáticos configurados."
            )
        else:
            value = ""
            for role_config in roles:
                role = interaction.guild.get_role(role_config.role_id)
                role_name = role.name if role else f"(Rol eliminado: {role_config.role_id})"
                status = "✅ Activo" if role_config.enabled else "❌ Inactivo"
                
                value += f"{role_name} - {status}\n"
            
            embed.add_field(
                name="Roles Configurados",
                value=value,
                inline=False
            )
        
        await interaction.response.send_message(embed=embed)
    
    @autorole_group.command(name='add', description='Añade un rol a la lista de roles automáticos')
    @app_commands.describe(role='El rol a añadir a la asignación automática')
    @app_commands.checks.has_permissions(manage_roles=True)
    async def autorole_add_slash(self, interaction: discord.Interaction, role: discord.Role):
        # Verificar permisos
        if role >= interaction.guild.me.top_role:
            await interaction.response.send_message("❌ No puedo asignar ese rol porque está por encima de mi rol más alto.")
            return
            
        # Comprobar si ya existe
        existing = await AutoRoleConfig.filter(guild_id=interaction.guild_id, role_id=role.id).first()
        if existing:
            # Si existe pero está desactivado, activarlo
            if not existing.enabled:
                existing.enabled = True
                await existing.save()
                await interaction.response.send_message(f"✅ El rol {role.mention} ya estaba en la lista pero estaba desactivado. Ha sido activado.")
            else:
                await interaction.response.send_message(f"❌ El rol {role.mention} ya está en la lista de roles automáticos.")
            return
            
        # Crear nuevo rol automático
        await AutoRoleConfig.create(
            guild_id=interaction.guild_id,
            role_id=role.id,
            enabled=True,
            is_join_role=True
        )
        
        await interaction.response.send_message(f"✅ El rol {role.mention} será asignado automáticamente a los nuevos miembros.")
    
    @autorole_group.command(name='remove', description='Elimina un rol de la lista de roles automáticos')
    @app_commands.describe(role='El rol a eliminar de la asignación automática')
    @app_commands.checks.has_permissions(manage_roles=True)
    async def autorole_remove_slash(self, interaction: discord.Interaction, role: discord.Role):
        # Eliminar de la base de datos
        deleted = await AutoRoleConfig.filter(guild_id=interaction.guild_id, role_id=role.id).delete()
        
        if deleted:
            await interaction.response.send_message(f"✅ El rol {role.mention} ya no será asignado automáticamente.")
        else:
            await interaction.response.send_message(f"❌ El rol {role.mention} no estaba en la lista de roles automáticos.")
    
    @autorole_group.command(name='toggle', description='Activa o desactiva la asignación automática de un rol')
    @app_commands.describe(role='El rol a activar/desactivar de la asignación automática')
    @app_commands.checks.has_permissions(manage_roles=True)
    async def autorole_toggle_slash(self, interaction: discord.Interaction, role: discord.Role):
        # Buscar configuración
        config = await AutoRoleConfig.filter(guild_id=interaction.guild_id, role_id=role.id).first()
        
        if not config:
            await interaction.response.send_message(f"❌ El rol {role.mention} no está en la lista de roles automáticos.")
            return
            
        # Invertir estado
        config.enabled = not config.enabled
        await config.save()
        
        await interaction.response.send_message(
            f"{'✅ Asignación automática activada' if config.enabled else '❌ Asignación automática desactivada'} para el rol {role.mention}."
        )


# Función para configurar la extensión con el bot
async def setup(bot):
    """Registra la extensión con el bot"""
    try:
        # Registrar el cog con el bot
        await bot.add_cog(WelcomeSystem(bot))
        logger.info("Módulo de bienvenida cargado correctamente")
    except Exception as e:
        logger.error(f"Error al cargar el módulo de bienvenida: {str(e)}")