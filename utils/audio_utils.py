# utils/audio_utils.py
import numpy as np
import soundfile as sf
import noisereduce as nr
import logging

logger = logging.getLogger('musicbot.audio_utils')

def is_silence(audio_data, threshold=500):
    """
    Detecta si un segmento de audio es silencio
    
    Args:
        audio_data: Datos de audio en numpy array
        threshold: Umbral para considerar silencio
        
    Returns:
        True si es silencio, False en caso contrario
    """
    if len(audio_data) == 0:
        return True
    
    # Calcular RMS (Root Mean Square)
    rms = np.sqrt(np.mean(np.square(audio_data)))
    return rms < threshold

def detect_voice_activity(audio_data, frame_length=1024, hop_length=512, threshold=0.01, min_speech_duration=0.3, sample_rate=48000):
    """
    Detecta actividad de voz en un segmento de audio.

    Args:
        audio_data:          Datos de audio en numpy array (int16 o float)
        frame_length:        Longitud de ventana para análisis
        hop_length:          Salto entre ventanas
        threshold:           Umbral de energía normalizada para considerar actividad
        min_speech_duration: Duración mínima (segundos) para confirmar voz (no usada en
                             este VAD simple, reservada para compatibilidad)
        sample_rate:         Tasa de muestreo (no usada aquí, reservada para extensión)

    Returns:
        bool: True si hay actividad de voz suficiente
    """
    if len(audio_data) < frame_length:
        return False

    # Normalizar a float [-1, 1] si llega como int16
    if audio_data.dtype == np.int16:
        audio_float = audio_data.astype(np.float32) / 32768.0
    else:
        audio_float = audio_data.astype(np.float32)

    # Calcular energía por ventanas
    n_frames = 1 + (len(audio_float) - frame_length) // hop_length
    if n_frames == 0:
        return False

    energies = []
    for i in range(n_frames):
        start = i * hop_length
        end = start + frame_length
        frame = audio_float[start:end]
        energy = np.mean(frame ** 2)
        energies.append(energy)

    # Al menos 20% de ventanas con energía sobre el umbral = hay voz
    active_frames = sum(1 for e in energies if e > threshold)
    return active_frames > n_frames * 0.2

def save_audio_to_file(audio_data, sample_rate, file_path):
    """
    Guarda datos de audio en un archivo
    
    Args:
        audio_data: Datos de audio en numpy array
        sample_rate: Tasa de muestreo
        file_path: Ruta del archivo a guardar
    """
    try:
        sf.write(file_path, audio_data, sample_rate)
        return True
    except Exception as e:
        logger.error(f"Error guardando audio: {str(e)}")
        return False

def apply_noise_reduction(audio_data, sample_rate):
    """
    Aplica reducción de ruido a los datos de audio
    
    Args:
        audio_data: Datos de audio en numpy array
        sample_rate: Tasa de muestreo
        
    Returns:
        Datos de audio con ruido reducido
    """
    try:
        # Tomar los primeros 0.5 segundos como perfil de ruido
        noise_len = min(int(sample_rate * 0.5), len(audio_data) // 4)
        noise_sample = audio_data[:noise_len]
        
        # Aplicar reducción de ruido
        reduced_noise = nr.reduce_noise(
            y=audio_data, 
            sr=sample_rate,
            y_noise=noise_sample,
            prop_decrease=0.75
        )
        
        return reduced_noise
    except Exception as e:
        logger.error(f"Error en reducción de ruido: {str(e)}")
        return audio_data  # Devolver audio original si hay error