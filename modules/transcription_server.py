# modules/transcription_server.py

from flask import Flask, request, jsonify
import asyncio
import threading
import logging
from modules.voice_commands import VoiceCommandHandler
from modules.music_player import MusicPlayer

# Crear instancia de Flask
app = Flask(__name__)

# Logger
logger = logging.getLogger('musicbot.transcription_server')

# Estas referencias serán configuradas más tarde
bot = None
music_player = None

# Inicializar el handler
voice_command_handler = None

@app.route('/transcription', methods=['POST'])
def handle_transcription():
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'No se envió JSON'}), 400

        user_id = data.get('user_id')
        guild_id = data.get('guild_id')
        transcription = data.get('transcription')

        if not all([user_id, guild_id, transcription]):
            return jsonify({'error': 'Faltan parámetros'}), 400

        logger.info(f"📝 Transcripción recibida: '{transcription}' (User {user_id}, Guild {guild_id})")

        # Procesar el comando en el loop de asyncio
        asyncio.run_coroutine_threadsafe(
            voice_command_handler.handle_voice_command(
                user_id, guild_id, None, None, transcription
            ),
            bot.loop
        )

        return jsonify({'status': 'Comando recibido'}), 200

    except Exception as e:
        logger.error(f"Error procesando transcripción: {e}")
        return jsonify({'error': 'Error interno'}), 500

def run_server():
    app.run(host='0.0.0.0', port=5050)

def start_transcription_server(bot_ref, music_player_ref):
    global bot, music_player, voice_command_handler
    bot = bot_ref
    music_player = music_player_ref
    voice_command_handler = VoiceCommandHandler(bot, music_player)

    thread = threading.Thread(target=run_server)
    thread.daemon = True
    thread.start()
