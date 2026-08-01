# whisper_server.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import whisper
import tempfile
import os
import logging

# Crear instancia de Flask
app = Flask(__name__)

# Configurar CORS
CORS(app)

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('whisper_server')

# Configuración
DEFAULT_MODEL_SIZE = "base"  # tiny, base, small, medium, large
ALLOWED_EXTENSIONS = {'wav', 'mp3', 'm4a', 'flac'}
MAX_CONTENT_LENGTH = 20 * 1024 * 1024  # 20 MB máximo
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

# Cargar modelo Whisper
logger.info(f"Cargando modelo Whisper ({DEFAULT_MODEL_SIZE})...")
model = whisper.load_model(DEFAULT_MODEL_SIZE)
logger.info("Modelo Whisper cargado exitosamente.")

def allowed_file(filename):
    """Verificar si el archivo tiene una extensión permitida"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/transcribe', methods=['POST'])
def transcribe_audio():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No se envió ningún archivo."}), 400

        audio_file = request.files['file']

        if audio_file.filename == '':
            return jsonify({"error": "Nombre de archivo vacío."}), 400

        if not allowed_file(audio_file.filename):
            return jsonify({"error": "Formato de archivo no permitido."}), 400

        # Obtener parámetros de la petición
        language = request.form.get('language', 'es')
        requested_model = request.form.get('model_size', DEFAULT_MODEL_SIZE)

        # Cambiar de modelo si es necesario
        global model
        if requested_model != DEFAULT_MODEL_SIZE:
            logger.info(f"Cambiando modelo Whisper a {requested_model}...")
            model = whisper.load_model(requested_model)

        # Guardar archivo temporal
        with tempfile.NamedTemporaryFile(delete=False, suffix='.wav') as temp_audio:
            audio_file.save(temp_audio.name)
            temp_audio_path = temp_audio.name

        logger.info(f"Archivo recibido: {audio_file.filename}, transcribiendo...")

        # Transcribir el archivo
        result = model.transcribe(temp_audio_path, language=language)

        # Borrar el archivo temporal
        os.remove(temp_audio_path)

        return jsonify({
            "transcription": result["text"],
            "segments": result.get("segments", [])
        })

    except Exception as e:
        logger.error(f"Error en transcripción: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({"error": "Archivo demasiado grande. Límite: 20MB"}), 413

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
