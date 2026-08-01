# 🎵 Voice-Controlled Discord Music Bot

> **Python · Discord.py · OpenAI Whisper · Flask · SQLite · Docker · Oracle Cloud · PM2**

A production-ready Discord bot that lets users control music playback through **voice commands** instead of text commands. Built as a full-stack backend engineering project combining speech recognition, real-time audio processing, caching, and cloud deployment.

---

## 📋 Table of Contents

- [Features](#-features)
- [Architecture](#-architecture)
- [Voice Command Pipeline](#-voice-command-pipeline)
- [Technical Challenges](#-technical-challenges)
- [Getting Started](#-getting-started)
- [Commands](#-commands)
- [Configuration](#-configuration)
- [Deployment](#-deployment)
- [Skills Demonstrated](#-skills-demonstrated)

---

## ✨ Features

| Feature | Details |
|---|---|
| 🎙️ Voice commands | Speak instead of type — Whisper transcribes your audio in real time |
| 🎵 Music playback | YouTube search, Spotify lookup, URL support |
| 📋 Queue management | Add, skip, stop, multi-server isolated queues |
| 💾 Listening history | Per-user history stored in SQLite |
| 🤖 Recommendations | Personalized suggestions based on listening patterns |
| ⚡ LRU audio cache | Reduces repeated downloads, lowers playback latency |
| 🌍 Multi-server | Each guild gets its own queue and voice session |
| 🐳 Dockerized | Reproducible deployments on Oracle Cloud via PM2 |

---

## 🏗️ Architecture

The project follows a modular backend architecture where each service has a single responsibility:

```
┌─────────────────────────────────────────────────────────┐
│                     Discord Gateway                      │
│               (discord.py  ·  WebSocket)                 │
└──────────────────────┬──────────────────────────────────┘
                       │
          ┌────────────▼────────────┐
          │      Bot Client         │  bot_client.py
          │  Slash + Prefix cmds    │
          └──┬──────────────────────┘
             │
    ┌────────▼─────────┐     ┌──────────────────────┐
    │   Voice Pipeline │     │    Music Player       │
    │                  │     │  Queue · Cache · Play │
    │ DiscordAudioSink │     │  LRU · History · Vol  │
    │ VoiceReceiver    │────▶│  Spotify · YouTube    │
    │ voice_processor  │     └──────────┬────────────┘
    │ IntentProcessor  │                │
    │ VoiceCmdHandler  │     ┌──────────▼────────────┐
    └──────────────────┘     │   Database Layer       │
                             │   SQLite · ORM async   │
    ┌──────────────────┐     │   History · Models     │
    │  Flask Server    │     └───────────────────────-┘
    │  /transcription  │
    │  port 5050       │
    └──────────────────┘
```

---

## 🎤 Voice Command Pipeline

The end-to-end flow when a user speaks in a Discord voice channel:

```
1. User speaks in Discord voice channel
         ↓
2. DiscordAudioSink captures PCM packets (48kHz stereo)
         ↓
3. VoiceReceiver accumulates audio + VAD (Voice Activity Detection)
   • Stops recording on silence
   • Resamples 48kHz → 16kHz mono for Whisper
         ↓
4. Whisper transcribes audio → text
   • Local model (default, free)   ← WHISPER_USE_API=false
   • OpenAI API (opt-in, paid)     ← WHISPER_USE_API=true
   • Hallucination filter applied
         ↓
5. IntentProcessor extracts intent + entities via regex
   • play_song, pause, resume, skip, stop, volume, queue, help
         ↓
6. VoiceCommandHandler executes action on MusicPlayer
         ↓
7. Bot responds in the text channel where !listen was invoked
```

### Transcription engine selection

| Mode | Config | Cost | Internet required |
|---|---|---|---|
| Whisper local (`base` model) | `WHISPER_USE_API=false` | Free | No |
| OpenAI API (`whisper-1`) | `WHISPER_USE_API=true` + `OPENAI_API_KEY` | Pay per use | Yes |

The API mode automatically falls back to local Whisper if the API call fails.

---

## 🧩 Technical Challenges

### 1. Making speech recognition reliable in real environments

**Problem:** Whisper produced inconsistent results due to background noise, different microphone qualities, Spanish/English accents, and varying speaking speeds.

**Iterations explored:**
- Preprocessing audio with noise reduction (`noisereduce`) before transcription
- Using an initial prompt to bias Whisper toward music command vocabulary
- Filtering known Whisper hallucinations ("gracias", "suscríbete", etc.)
- VAD (Voice Activity Detection) to avoid sending silence to Whisper
- Resampling Discord's 48kHz stereo to 16kHz mono (Whisper's optimal format)

### 2. Real-time audio capture from Discord

**Problem:** discord.py doesn't natively expose a simple audio capture API. Initial implementation used simulated audio (numpy random arrays) as a placeholder.

**Solution:** Implemented a custom `DiscordAudioSink` using discord.py's `AudioSink` interface, which receives raw PCM packets per user and accumulates them into buffers until VAD detects silence.

### 3. Async architecture

The bot juggles multiple concurrent tasks:
- Music playback stream per guild
- Voice capture loops per listening user
- Database writes (listening history)
- Flask endpoint for external transcription requests
- Periodic LRU cache cleanup

All heavy CPU operations (Whisper inference) are offloaded to a `ThreadPoolExecutor` to avoid blocking the asyncio event loop.

---

## 🚀 Getting Started

### Prerequisites

- Python 3.10+
- FFmpeg installed and on PATH
- A Discord application with a bot token ([Discord Developer Portal](https://discord.com/developers/applications))
- Spotify app credentials ([Spotify for Developers](https://developer.spotify.com/dashboard))

### Installation

```bash
# 1. Clone the repository
git clone https://github.com/your-username/musicbot.git
cd musicbot

# 2. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate       # Windows
# source .venv/bin/activate  # Linux / macOS

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
cp .env.example .env
# Edit .env with your tokens (see Configuration section)

# 5. Run the bot
python main.py
```

### Docker

```bash
docker build -t musicbot .
docker run --env-file .env musicbot
```

---

## 🎮 Commands

### Slash commands (recommended)

| Command | Description |
|---|---|
| `/play <song>` | Search and play a song |
| `/skip` | Skip to the next song |
| `/stop` | Stop playback and disconnect |
| `/join` | Join your voice channel |
| `/listen` | **Activate voice command mode** |
| `/stop_listen` | Deactivate voice command mode |
| `/recommend` | Get personalized song recommendations |
| `/ping` | Check bot latency |

### Voice commands (after `/listen`)

Speak naturally in your language:

| What you say | Action |
|---|---|
| *"reproduce Shape of You"* | Play "Shape of You" |
| *"pausa"* | Pause playback |
| *"continúa"* | Resume playback |
| *"salta"* | Skip current song |
| *"detén la música"* | Stop and disconnect |
| *"volumen 70"* | Set volume to 70% |
| *"muestra la cola"* | Show queue |
| *"detén escucha"* | Deactivate voice mode |

---

## ⚙️ Configuration

All configuration is done via a `.env` file. Copy `.env.example` to get started:

```env
# Required
BOT_TOKEN=your_discord_bot_token
SPOTIFY_CLIENT_ID=your_spotify_client_id
SPOTIFY_CLIENT_SECRET=your_spotify_client_secret

# Optional
BOT_PREFIX=!                  # Default command prefix
DATABASE_URL=sqlite:///musicbot.db

# Whisper transcription engine
WHISPER_USE_API=false         # true = OpenAI API, false = local model (default)
OPENAI_API_KEY=               # Only needed if WHISPER_USE_API=true
```

> ⚠️ **Never commit `.env` to version control.** It is listed in `.gitignore`. Use `.env.example` as a template.

---

## 🚢 Deployment

The bot is deployed on an **Oracle Cloud** Always Free VM running Ubuntu.

### Stack

```
Oracle Cloud VM (Ubuntu)
└── Docker container
    └── Python bot process
        └── PM2 process manager
            ├── Auto-restart on crash
            ├── Startup on system boot
            └── Log management
```

### PM2 setup

```bash
# Start the bot
pm2 start musicbot_pm2.json

# Monitor
pm2 status
pm2 logs discord-musicbot

# Auto-start on system reboot
pm2 startup
pm2 save
```

---

## 🍪 Cookie Management (YouTube)

YouTube requires authenticated cookies to avoid bot detection and rate limiting. These cookies come from a logged-in Google session and **expire periodically**.

### How it works

| Event | What happens |
|---|---|
| Bot starts | Checks cookie expiry, logs status |
| Every 24h | `CookieMonitor` re-checks and DMs owners if expiring |
| Cookie expires | `yt-dlp` returns a clear error instead of a generic failure |
| `/cookies` command | Owner can check status directly from Discord |

### Check status from Discord

```
/cookies   →  🟡 Expiran en 3 días  /  ✅ Válidas (45 días)  /  🔴 Expiradas
```
*(Solo visible para owners del bot — respuesta ephemeral)*

### Renew cookies (from your local PC)

```bash
# 1. Edit the variables at the top of the script
nano scripts/refresh_cookies.sh   # Set SERVER_HOST, SERVER_USER, BROWSER

# 2. Run it — extracts fresh cookies from your browser and uploads via SSH
bash scripts/refresh_cookies.sh
```

The script uses `yt-dlp --cookies-from-browser` to extract cookies from your local Chrome/Firefox session and uploads them to the Oracle Cloud server via `scp`. **No bot restart needed.**

> ⚠️ `cookies.txt` is in `.gitignore` and must never be committed to the repository.

---

## 🛠️ Skills Demonstrated

- **Python** — asyncio, OOP, modular architecture
- **Backend Engineering** — service decomposition, async task management
- **Discord.py** — AudioSink, slash commands, multi-server support
- **OpenAI Whisper** — local inference, API integration, prompt engineering
- **Audio Processing** — PCM manipulation, VAD, resampling, noise reduction
- **REST APIs** — Flask internal endpoint, Spotify API, YouTube extraction
- **Database** — SQLite, Tortoise ORM, async queries
- **Caching** — LRU eviction policy, size-based cleanup, TTL expiry
- **Docker** — containerization, reproducible deployments
- **Linux / Oracle Cloud** — server setup, SSH, process management
- **PM2** — production process management, auto-restart, logging
- **Software Architecture** — pipeline design, separation of concerns
#   m u s i c b o t  
 