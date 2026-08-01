# modules/voice_commands.py
#
# Ejecuta acciones del MusicPlayer a partir de las intenciones
# detectadas por IntentProcessor.
#
# Intents soportados (deben coincidir con intent_processor.py):
#   play_song, pause, resume, skip, stop, volume, queue, help, unknown

import logging

logger = logging.getLogger('musicbot.voice_commands')


class VoiceCommandHandler:
    def __init__(self, bot, music_player):
        self.bot = bot
        self.music_player = music_player

    async def handle_voice_command(
        self,
        user_id: int,
        guild_id: int,
        intent: str,
        entities: list,
        transcription: str,
    ) -> str:
        """
        Ejecuta la acción correspondiente al intent detectado.

        Args:
            user_id:       ID del usuario que habló
            guild_id:      ID del servidor
            intent:        nombre del intent (de IntentProcessor)
            entities:      lista de dicts {"type": ..., "value": ...}
            transcription: texto original transcrito (para logs y fallbacks)

        Returns:
            Mensaje de respuesta para enviar al canal de texto.
        """
        try:
            logger.info(f"[Usuario {user_id}] Comando: {intent} | '{transcription}'")

            # ── Reproducir canción ─────────────────────────────
            if intent == "play_song":
                song_entity = next((e for e in entities if e["type"] == "song"), None)
                query = song_entity["value"] if song_entity else transcription
                if not query:
                    return "🎵 ¿Qué canción quieres reproducir?"
                return await self.music_player.play_song(query, user_id)

            # ── Pausa ──────────────────────────────────────────
            elif intent == "pause":
                return await self.music_player.pause(guild_id)

            # ── Reanudar ───────────────────────────────────────
            elif intent == "resume":
                return await self.music_player.resume(guild_id)

            # ── Saltar ─────────────────────────────────────────
            elif intent == "skip":
                return await self.music_player.skip(guild_id)

            # ── Detener ────────────────────────────────────────
            elif intent == "stop":
                return await self.music_player.stop(guild_id)

            # ── Volumen ────────────────────────────────────────
            elif intent == "volume":
                vol_entity = next((e for e in entities if e["type"] == "volume_level"), None)
                if vol_entity:
                    level = max(0, min(100, int(vol_entity["value"]))) / 100.0
                    return await self.music_player.set_volume(level, guild_id)
                return "🔊 Di un nivel de volumen del 0 al 100, por ejemplo: *'volumen 70'*"

            # ── Cola ───────────────────────────────────────────
            elif intent == "queue":
                return await self.music_player.get_queue(guild_id)

            # ── Ayuda ──────────────────────────────────────────
            elif intent == "help":
                return (
                    "🎙️ **Comandos de voz disponibles:**\n"
                    "• *reproduce [canción]* — Busca y reproduce\n"
                    "• *pausa* — Pausa la música\n"
                    "• *continúa* — Reanuda la música\n"
                    "• *salta* — Siguiente canción\n"
                    "• *detén la música* — Para todo\n"
                    "• *volumen [0-100]* — Ajusta el volumen\n"
                    "• *muestra la cola* — Lista de canciones\n"
                    "• *detén escucha* — Desactiva este modo"
                )

            # ── Sin intent reconocido ──────────────────────────
            elif intent == "unknown":
                logger.debug(f"Intent desconocido para: '{transcription}'")
                return f"🤔 No entendí ese comando. Di *'ayuda'* para ver lo que puedo hacer."

            else:
                logger.warning(f"Intent no manejado: {intent}")
                return f"⚠️ Comando '{intent}' no implementado aún."

        except Exception as e:
            logger.error(f"Error manejando comando de voz: {e}", exc_info=True)
            return "❌ Ocurrió un error procesando tu comando de voz."
