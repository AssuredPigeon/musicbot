# translator/__init__.py
# Modulo de traduccion en tiempo real para Discord.
#
# Stack gratuito:
#   STT  : Whisper local (ya instalado)
#   TR   : deep-translator (Google Translate, sin API key)
#   TTS  : edge-tts (Microsoft Edge, gratis, 400+ voces)
#
# Completamente separado del MusicPlayer.
# Se carga como Cog en bot_client.py:
#   await bot.load_extension("translator.commands")
