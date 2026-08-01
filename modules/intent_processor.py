# Procesador de intenciones para comandos de voz
import logging
import re
import asyncio
import config
from utils.error_handler import log_exception

logger = logging.getLogger('musicbot.intent')

class IntentProcessor:
    """
    Procesa el texto transcrito para detectar intenciones y entidades
    """
    def __init__(self):
        self.intents = {
            "play_song": {
                "patterns": [
                    r"(?:reproduce|play|pon|poner|escuchar|tocar)(?:\s+la\s+canci[oó]n)?(?:\s+de)?\s+(.*)",
                    r"(?:quiero|me\s+gustari[aá]|podri[aá]s?)\s+(?:escuchar|o[ií]r|poner)\s+(.*)",
                    r"(?:pon|reproduce|play)(?:me)?\s+(.*)",
                ]
            },
            "pause": {
                "patterns": [
                    r"(?:pausa|pausar|para|detener|parar|stop|pause)(?:\s+la\s+(?:canci[oó]n|m[uú]sica))?",
                    r"(?:haz|haga)\s+una\s+pausa",
                ]
            },
            "resume": {
                "patterns": [
                    r"(?:continua|continuar|seguir|resume|resumir|sigue|continue)(?:\s+la\s+(?:canci[oó]n|m[uú]sica))?",
                    r"(?:vuelve|volver)\s+a\s+(?:reproducir|poner|tocar)",
                ]
            },
            "skip": {
                "patterns": [
                    r"(?:salta|saltar|skip|next|siguiente|cambiar?)(?:\s+(?:esta|la)\s+(?:canci[oó]n|m[uú]sica))?",
                    r"(?:pasa|pasar)\s+a\s+la\s+siguiente",
                ]
            },
            "stop": {
                "patterns": [
                    r"(?:termina|terminar|finaliza|finalizar|stop|detener)(?:\s+la\s+(?:reproducci[oó]n|m[uú]sica))?",
                    r"(?:apaga|apagar)\s+la\s+m[uú]sica",
                ]
            },
            "volume": {
                "patterns": [
                    r"(?:sube|baja|ajusta|cambia|pon)\s+(?:el\s+)?volumen(?:\s+a\s+(\d+))?",
                    r"volumen\s+(\d+)",
                ]
            },
            "queue": {
                "patterns": [
                    r"(?:muestra|mostrar|ver|lista)\s+(?:la\s+)?(?:cola|queue|lista)(?:\s+de\s+reproducci[oó]n)?",
                    r"(?:qu[eé]|cu[aá]les)(?:\s+canciones)?\s+(?:hay|están)(?:\s+en\s+la\s+cola)?",
                ]
            },
            "help": {
                "patterns": [
                    r"(?:ayuda|ayudar|help|comandos|instrucciones)",
                    r"(?:c[oó]mo|como)(?:\s+(?:funciona|usar|utilizar))(?:\s+el\s+bot)?",
                ]
            }
        }
        
        # Precompliar expresiones regulares
        for intent_name, intent_data in self.intents.items():
            intent_data["compiled"] = [re.compile(pattern, re.IGNORECASE) for pattern in intent_data["patterns"]]
    
    async def process_text(self, text):
        """
        Procesa el texto para detectar intenciones y entidades
        
        Args:
            text: El texto transcrito a procesar
            
        Returns:
            dict: {
                "intent": nombre de la intención detectada,
                "entities": lista de entidades detectadas
            }
        """
        try:
            # Normalizar texto
            text = text.lower().strip()
            
            # Verificar cada intención
            for intent_name, intent_data in self.intents.items():
                for i, pattern in enumerate(intent_data["compiled"]):
                    match = pattern.match(text)
                    if match:
                        logger.debug(f"Intención detectada: {intent_name} (patrón {i+1})")
                        
                        # Procesar entidades según la intención
                        entities = []
                        
                        if intent_name == "play_song" and match.groups():
                            song_name = match.group(1).strip()
                            if song_name:
                                entities.append({
                                    "type": "song",
                                    "value": song_name
                                })
                        
                        elif intent_name == "volume" and match.groups():
                            vol_value = match.group(1)
                            if vol_value:
                                entities.append({
                                    "type": "volume_level",
                                    "value": int(vol_value)
                                })
                        
                        return {
                            "intent": intent_name,
                            "entities": entities
                        }
            
            # Si llegamos aquí, no se detectó ninguna intención específica
            # Verificar si hay palabras clave de música para asumir que es play_song
            if any(keyword in text for keyword in config.TRIGGER_KEYWORDS):
                # Eliminar palabras clave para obtener el nombre de la canción
                song_name = text
                for keyword in config.TRIGGER_KEYWORDS:
                    song_name = song_name.replace(keyword, "")
                
                song_name = song_name.strip()
                
                if song_name:
                    logger.debug(f"Asumiendo intención play_song para: '{text}'")
                    return {
                        "intent": "play_song",
                        "entities": [{
                            "type": "song",
                            "value": song_name
                        }]
                    }
            
            # Caso donde no se detecta ninguna intención
            return {
                "intent": "unknown",
                "entities": []
            }
                
        except Exception as e:
            log_exception(e, "Error procesando el texto para detección de intenciones")
            return {
                "intent": "error",
                "entities": []
            }