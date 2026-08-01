// modules/voice_capture/index.js
const { joinVoiceChannel, createAudioReceiver } = require('./voice_connection');
const { Writable } = require('stream');
const prism = require('prism-media');
const fs = require('fs');
const fetch = require('node-fetch');
const path = require('path');

// Configuración básica
const TEMP_DIR = path.join(__dirname, '../../data/temp_audio');
const WHISPER_SERVER_URL = 'http://127.0.0.1:5000/transcribe'; // Cambia si tu Whisper Server está en otro lado
const MIN_AUDIO_DURATION_SEC = 2; // Duración mínima para enviar

// Asegurarse de que el directorio temporal existe
if (!fs.existsSync(TEMP_DIR)) {
    fs.mkdirSync(TEMP_DIR, { recursive: true });
}

async function startListeningToUser(voiceChannel, userId) {
    const connection = joinVoiceChannel(voiceChannel);
    const receiver = createAudioReceiver(connection);

    receiver.speaking.on('start', (speakingUserId) => {
        if (speakingUserId !== userId) return;

        console.log(`🎤 Usuario ${speakingUserId} comenzó a hablar.`);

        const opusStream = receiver.subscribe(userId, {
            end: {
                behavior: 'afterSilence',
                duration: 1000, // Considera silencio después de 1 segundo
            },
        });

        const pcmStream = new prism.opus.Decoder({ frameSize: 960, channels: 1, rate: 48000 });
        const outputPath = path.join(TEMP_DIR, `user_${userId}_${Date.now()}.wav`);
        const outputStream = fs.createWriteStream(outputPath);

        opusStream.pipe(pcmStream).pipe(outputStream);

        opusStream.on('end', async () => {
            console.log(`🛑 Usuario ${speakingUserId} terminó de hablar.`);

            outputStream.end();

            // Esperar a que termine de escribir completamente
            outputStream.on('finish', async () => {
                try {
                    // Verificar tamaño mínimo antes de enviar
                    const stats = fs.statSync(outputPath);
                    const fileSizeSec = stats.size / (2 * 48000); // 2 bytes por muestra, 48000 muestras por segundo

                    if (fileSizeSec < MIN_AUDIO_DURATION_SEC) {
                        console.log('⚠️ Audio demasiado corto, ignorado.');
                        fs.unlinkSync(outputPath);
                        return;
                    }

                    // Enviar a Whisper
                    await sendAudioToWhisper(outputPath, speakingUserId);
                } catch (err) {
                    console.error('Error procesando audio:', err);
                }
            });
        });
    });
}

async function sendAudioToWhisper(audioPath, userId) {
  console.log(`🚀 Enviando audio de usuario ${userId} a Whisper...`);

  const formData = new FormData();
  formData.append('audio', fs.createReadStream(audioPath));

  try {
      const response = await fetch('http://127.0.0.1:5000/transcribe', {
          method: 'POST',
          body: formData,
      });

      const data = await response.json();
      console.log(`📝 Texto transcripto:`, data.transcription);

      // Mandar texto al bot Python
      await fetch('http://127.0.0.1:5050/transcription', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
              user_id: userId,
              guild_id: YOUR_GUILD_ID,  // Reemplaza o pásalo dinámico
              transcription: data.transcription
          })
      });

  } catch (error) {
      console.error('Error al enviar audio:', error);
  } finally {
      fs.unlinkSync(audioPath);
  }
}


module.exports = { startListeningToUser };
