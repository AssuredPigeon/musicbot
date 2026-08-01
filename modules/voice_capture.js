// modules/voice_capture/voice_capture.js

const fs = require('fs');
const fetch = require('node-fetch');
const FormData = require('form-data');
const path = require('path');

// Configuración
const WHISPER_URL = 'http://127.0.0.1:5000/transcribe';
const BOT_PYTHON_URL = 'http://127.0.0.1:5050/transcription';

async function processAudioFile(audioPath, userId, guildId) {
    try {
        const formData = new FormData();
        formData.append('file', fs.createReadStream(audioPath));
        formData.append('language', 'es'); // o 'en', etc.
        formData.append('model_size', 'base'); // tiny, small, etc.

        console.log('🎧 Enviando audio a Whisper...');
        const whisperResponse = await fetch(WHISPER_URL, {
            method: 'POST',
            body: formData
        });

        const whisperResult = await whisperResponse.json();

        if (!whisperResult.transcription) {
            console.error('❌ Whisper no devolvió transcripción:', whisperResult);
            return;
        }

        console.log('📝 Transcripción:', whisperResult.transcription);

        // Ahora enviamos el texto al bot Python
        const payload = {
            user_id: userId,
            guild_id: guildId,
            transcription: whisperResult.transcription
        };

        const botResponse = await fetch(BOT_PYTHON_URL, {
            method: 'POST',
            body: JSON.stringify(payload),
            headers: { 'Content-Type': 'application/json' }
        });

        const botResult = await botResponse.json();
        console.log('🤖 Bot respondió:', botResult);

        // Elimina el archivo temporal si quieres
        fs.unlinkSync(audioPath);
    } catch (err) {
        console.error('❌ Error procesando audio:', err);
    }
}

// Exportar la función para usar desde otros módulos
module.exports = { processAudioFile };
