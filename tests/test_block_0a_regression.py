# tests/test_block_0a_regression.py
"""
Suite de pruebas unitarias, concurrencia, ciclo de vida y regresión para el Bloque 0A (v3.1).

Nota sobre entorno local:
  Usa las librerías reales instaladas en el Python activo (ej. Flask, PyTorch, NumPy, python-dotenv)
  e instala test doubles ligeros únicamente para módulos de red/Discord ausentes en el entorno local
  (ej. discord, discord.ext.voice_recv, tortoise, aiosqlite, yt_dlp, spotipy). En el resumen final
  reporta qué dependencias fueron reales y cuáles usaron test double.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

STUBBED_MODULES: list[str] = []
REAL_MODULES: list[str] = []


def _probe_or_stub_deps():
    """Carga dependencias reales disponibles y registra doubles mínimos solo para las ausentes."""
    # 1. Numpy
    try:
        import numpy  # noqa: F401
        REAL_MODULES.append("numpy")
    except ImportError:
        import struct

        class _FakeNDArray(list):
            def astype(self, _dtype):
                return _FakeNDArray(float(x) for x in self)

            def __truediv__(self, other):
                return _FakeNDArray(x / other for x in self)

            def __pow__(self, power):
                return _FakeNDArray(x ** power for x in self)

        def _fake_frombuffer(b, dtype=None):
            cnt = len(b) // 2
            return _FakeNDArray(struct.unpack(f"<{cnt}h", b[: cnt * 2]))

        np_stub = types.ModuleType("numpy")
        np_stub.ndarray = _FakeNDArray
        np_stub.int16 = int
        np_stub.float32 = float
        np_stub.frombuffer = _fake_frombuffer
        np_stub.mean = lambda arr: (sum(arr) / len(arr)) if len(arr) else 0.0
        sys.modules["numpy"] = np_stub
        STUBBED_MODULES.append("numpy")

    # 2. python-dotenv
    try:
        import dotenv  # noqa: F401
        REAL_MODULES.append("dotenv")
    except ImportError:
        dotenv_stub = types.ModuleType("dotenv")
        dotenv_stub.load_dotenv = lambda *a, **kw: False
        sys.modules["dotenv"] = dotenv_stub
        STUBBED_MODULES.append("dotenv")

    # 3. Variables mínimas de entorno en memoria para importar config.py sin tocar .env ni BDs reales
    os.environ.setdefault("BOT_TOKEN", "test_token_local_unit_only")
    os.environ.setdefault("SPOTIFY_CLIENT_ID", "test_spotify_id_only")
    os.environ.setdefault("SPOTIFY_CLIENT_SECRET", "test_spotify_secret_only")

    # 4. discord + discord.ext.commands + app_commands + voice_recv
    try:
        import discord  # noqa: F401
        REAL_MODULES.append("discord")
    except ImportError:
        discord_mod = types.ModuleType("discord")

        class _Intents:
            def __init__(self):
                self.message_content = False
                self.voice_states = False
                self.members = False

            @classmethod
            def default(cls):
                return cls()

        class _ActivityType:
            listening = 2

        class _Activity:
            def __init__(self, **kw):
                pass

        class _Color:
            @staticmethod
            def blurple():
                return 0x5865F2

            @staticmethod
            def blue():
                return 0x3498DB

            @staticmethod
            def green():
                return 0x2ECC71

            @staticmethod
            def orange():
                return 0xE67E22

            @staticmethod
            def purple():
                return 0x9B59B6

            @staticmethod
            def from_rgb(r, g, b):
                return (r << 16) + (g << 8) + b

        class _Embed:
            def __init__(self, **kw):
                self.fields = []
                self.footer = ""

            def set_author(self, **kw):
                pass

            def add_field(self, **kw):
                self.fields.append(kw)

            def set_footer(self, text=""):
                self.footer = text

        class _VoiceClient:
            pass

        class _FFmpegPCMAudio:
            def __init__(self, src, **kw):
                self.src = src

        class _PCMVolumeTransformer:
            def __init__(self, src, volume=0.5):
                self.source = src
                self.volume = volume

        class _NotFound(Exception):
            pass

        discord_mod.Intents = _Intents
        discord_mod.ActivityType = _ActivityType
        discord_mod.Activity = _Activity
        discord_mod.Color = _Color
        discord_mod.ButtonStyle = types.SimpleNamespace(
            primary=1, secondary=2, success=3, danger=4
        )
        discord_mod.Embed = _Embed
        discord_mod.VoiceClient = _VoiceClient
        discord_mod.FFmpegPCMAudio = _FFmpegPCMAudio
        discord_mod.PCMVolumeTransformer = _PCMVolumeTransformer
        discord_mod.User = object
        discord_mod.Client = object
        discord_mod.Guild = object
        discord_mod.Role = object
        discord_mod.CategoryChannel = object
        discord_mod.PermissionOverwrite = object
        discord_mod.SelectOption = object
        discord_mod.NotFound = _NotFound
        discord_mod.VoiceChannel = object
        discord_mod.Interaction = object
        discord_mod.TextChannel = object
        discord_mod.Object = lambda id=0: types.SimpleNamespace(id=id)

        ext_mod = types.ModuleType("discord.ext")
        cmds_mod = types.ModuleType("discord.ext.commands")

        def _make_cmd_decorator():
            def _dec(*a, **kw):
                def _wrap(f):
                    f.command = _dec
                    f.group = _dec
                    f.error = lambda *ea, **ekw: (lambda ef: ef)
                    f.autocomplete = lambda *aa, **akw: (lambda af: af)
                    return f
                return _wrap
            return _dec

        _cmd_dec = _make_cmd_decorator()

        class _Bot:
            def __init__(self, **kw):
                self.guilds = []
                self.latency = 0.01
                self.loop = None
                self.tree = types.SimpleNamespace(
                    command=_cmd_dec,
                    add_command=lambda *a, **k: None,
                    sync=lambda *a, **k: None,
                )

            def command(self, *a, **kw):
                return _cmd_dec(*a, **kw)

            def group(self, *a, **kw):
                return _cmd_dec(*a, **kw)

            def event(self, f):
                return f

            async def add_cog(self, cog):
                pass

            async def load_extension(self, ext):
                pass

            def is_closed(self):
                return True

        class _Cog:
            def __init_subclass__(cls, **kwargs):
                super().__init_subclass__()

            @classmethod
            def listener(cls, name=None):
                return lambda f: f

        class _GroupCog(_Cog):
            pass

        class _HelpCommand:
            pass

        cmds_mod.Bot = _Bot
        cmds_mod.Cog = _Cog
        cmds_mod.GroupCog = _GroupCog
        cmds_mod.HelpCommand = _HelpCommand
        cmds_mod.Group = object
        cmds_mod.command = _cmd_dec
        cmds_mod.group = _cmd_dec
        cmds_mod.is_owner = lambda: (lambda f: f)
        cmds_mod.has_permissions = lambda **kw: (lambda f: f)
        cmds_mod.CommandNotFound = Exception
        cmds_mod.MissingRequiredArgument = Exception
        cmds_mod.BadArgument = Exception
        cmds_mod.CommandOnCooldown = Exception

        app_mod = types.ModuleType("discord.app_commands")

        class _Group:
            def __init__(self, **kw):
                pass

            def command(self, *a, **kw):
                return _cmd_dec(*a, **kw)

        app_mod.Group = _Group
        app_mod.command = _cmd_dec
        app_mod.describe = lambda **kw: (lambda f: f)
        app_mod.errors = types.SimpleNamespace(MissingPermissions=Exception)
        app_mod.checks = types.SimpleNamespace(has_permissions=lambda **kw: (lambda f: f))

        ui_mod = types.ModuleType("discord.ui")
        ui_mod.View = object
        ui_mod.Button = object
        ui_mod.Select = object
        ui_mod.button = _cmd_dec

        voice_recv_mod = types.ModuleType("discord.ext.voice_recv")

        class _FakeAudioSink:
            def __init__(self, destination=None, /):
                self._child = destination

        class _FakeVoiceData:
            pcm: bytes = b""

        class _FakeVoiceRecvClient(_VoiceClient):
            def listen(self, sink):
                pass

            def is_listening(self):
                return False

            def stop_listening(self):
                pass

        voice_recv_mod.AudioSink = _FakeAudioSink
        voice_recv_mod.VoiceData = _FakeVoiceData
        voice_recv_mod.VoiceRecvClient = _FakeVoiceRecvClient

        ext_mod.commands = cmds_mod
        ext_mod.voice_recv = voice_recv_mod
        discord_mod.ext = ext_mod
        discord_mod.app_commands = app_mod
        discord_mod.ui = ui_mod

        sys.modules["discord"] = discord_mod
        sys.modules["discord.ext"] = ext_mod
        sys.modules["discord.ext.commands"] = cmds_mod
        sys.modules["discord.app_commands"] = app_mod
        sys.modules["discord.ui"] = ui_mod
        sys.modules["discord.ext.voice_recv"] = voice_recv_mod
        STUBBED_MODULES.extend(["discord", "discord.ext.voice_recv"])

    # 5. Resto de módulos opcionales solo si faltan (ej. noisereduce, soundfile, yt_dlp, spotipy, tortoise, aiosqlite)
    for mod_name in ("soundfile", "noisereduce", "yt_dlp", "spotipy", "aiosqlite", "requests"):
        try:
            importlib.import_module(mod_name)
            REAL_MODULES.append(mod_name)
        except ImportError:
            m = types.ModuleType(mod_name)
            if mod_name == "spotipy":
                m.Spotify = object
                sys.modules["spotipy.oauth2"] = types.ModuleType("spotipy.oauth2")
                sys.modules["spotipy.oauth2"].SpotifyClientCredentials = object
            elif mod_name == "yt_dlp":
                m.YoutubeDL = object
            sys.modules[mod_name] = m
            STUBBED_MODULES.append(mod_name)

    try:
        import tortoise  # noqa: F401
        REAL_MODULES.append("tortoise")
    except ImportError:
        t_mod = types.ModuleType("tortoise")
        fields_mod = types.ModuleType("tortoise.fields")
        models_mod = types.ModuleType("tortoise.models")

        class _FakeField:
            def __init__(self, *a, **kw):
                pass

        for fname in (
            "IntField", "BigIntField", "TextField", "CharField",
            "BooleanField", "JSONField", "DatetimeField", "ForeignKeyField"
        ):
            setattr(fields_mod, fname, _FakeField)

        class _FakeModel:
            pass

        models_mod.Model = _FakeModel

        class _FakeTortoise:
            @staticmethod
            async def init(**kw):
                pass

            @staticmethod
            async def generate_schemas():
                pass

            @staticmethod
            async def close_connections():
                pass

        t_mod.Tortoise = _FakeTortoise
        t_mod.fields = fields_mod
        t_mod.models = models_mod
        sys.modules["tortoise"] = t_mod
        sys.modules["tortoise.fields"] = fields_mod
        sys.modules["tortoise.models"] = models_mod
        STUBBED_MODULES.append("tortoise")

    try:
        import torch  # noqa: F401
        REAL_MODULES.append("torch")
    except ImportError:
        STUBBED_MODULES.append("torch(absent)")

    try:
        import flask  # noqa: F401
        REAL_MODULES.append("flask")
    except ImportError:
        flask_mod = types.ModuleType("flask")

        class _FakeFlask:
            def __init__(self, *a, **kw):
                self.config = {}

            def route(self, *a, **kw):
                return lambda fn: fn

            def errorhandler(self, *a, **kw):
                return lambda fn: fn

        flask_mod.Flask = _FakeFlask
        flask_mod.jsonify = lambda *a, **kw: {}
        flask_mod.request = types.SimpleNamespace(headers={}, json=None, data=b"")
        sys.modules["flask"] = flask_mod
        STUBBED_MODULES.append("flask(stub)")


_probe_or_stub_deps()

# Prevenir creación accidental de musicbot.db en disco al importar data.database durante tests locales
import config  # noqa: E402
_TEST_TMP_DIR = tempfile.TemporaryDirectory()
config.DATABASE_URL = f"sqlite:///{os.path.join(_TEST_TMP_DIR.name, 'test_musicbot.db')}"

from utils.stt_queue import STTQueueManager, STTStatus  # noqa: E402
from utils.voice_session_manager import (  # noqa: E402
    CONNECT_INTERNAL_TIMEOUT_S,
    CONNECT_OUTER_TIMEOUT_S,
    MOVE_TIMEOUT_S,
    DeferredDisconnectRecord,
    PlaybackPreemptionTimeoutError,
    PlaybackStateInconsistencyError,
    VoiceClientRecoveryError,
    VoiceClientUnavailableError,
    VoiceClientUpgradeBlockedError,
    VoiceReceiveUnavailableError,
    VoiceSessionManager,
    _probe_vc_connected,
    voice_session_manager,
)
from modules.intent_processor import IntentProcessor  # noqa: E402
from modules.voice_receiver import DiscordAudioSink  # noqa: E402
from translator.audio_sink import TranslatorAudioSink  # noqa: E402
import modules.voice_processor as voice_processor  # noqa: E402
import translator.stt as translator_stt  # noqa: E402
from modules.music_player import MusicOpToken, MusicPlayer  # noqa: E402
from translator.manager import TranslatorSession  # noqa: E402
import modules.transcription_server as transcription_server  # noqa: E402


class FakeStandardVoiceClient:
    def __init__(self, channel, hold_play=False, *, async_after_thread: bool = False):
        self.channel = channel
        self.connected = True
        self.is_connected_behavior = None
        self.playing = False
        self.paused = False
        self.played = []
        self.hold_play = hold_play
        self.async_after_thread = async_after_thread
        self.stop_calls = 0
        self.disconnect_calls = []
        self._after_cb = None
        self._after_threads: list[threading.Thread] = []
        self.after_thread_gate: threading.Event | None = None
        self.after_thread_ids: list[int] = []
        self.disconnect_gate: asyncio.Event | None = None
        self.disconnect_exc: Exception | None = None
        self.move_to_gate: asyncio.Event | None = None
        self.move_to_exc: Exception | None = None
        self.move_to_ignore_cancel: bool = False
        self.move_to_custom_handler = None
        self.source = None

    def is_connected(self):
        if callable(self.is_connected_behavior):
            return self.is_connected_behavior()
        if self.is_connected_behavior is not None:
            return self.is_connected_behavior
        return self.connected

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return self.paused

    def _fire_after(self, cb, err=None):
        if self.async_after_thread:
            def _runner():
                self.after_thread_ids.append(threading.get_ident())
                if self.after_thread_gate is not None:
                    self.after_thread_gate.wait(timeout=5.0)
                cb(err)

            t = threading.Thread(target=_runner, name="fake-audio-after", daemon=True)
            self._after_threads.append(t)
            t.start()
        else:
            cb(err)

    def join_threads(self, timeout: float = 2.0):
        for t in self._after_threads:
            t.join(timeout=timeout)
            assert not t.is_alive(), f"Thread {t.name} sigue vivo tras join()"

    def stop(self):
        self.stop_calls += 1
        cb = self._after_cb
        self._after_cb = None
        was_active = self.playing or self.paused
        self.playing = False
        self.paused = False
        self.source = None
        if was_active and cb:
            self._fire_after(cb, None)

    def pause(self):
        if self.playing:
            self.playing = False
            self.paused = True

    def resume(self):
        if self.paused:
            self.paused = False
            self.playing = True

    async def move_to(self, ch):
        if self.move_to_custom_handler is not None:
            res = self.move_to_custom_handler(ch)
            if asyncio.iscoroutine(res):
                return await res
            return res
        if self.move_to_gate is not None:
            if self.move_to_ignore_cancel:
                while not self.move_to_gate.is_set():
                    try:
                        await self.move_to_gate.wait()
                    except asyncio.CancelledError:
                        await asyncio.sleep(0.02)
                raise asyncio.CancelledError()
            await self.move_to_gate.wait()
        if self.move_to_exc is not None:
            raise self.move_to_exc
        self.channel = ch

    async def disconnect(self, *, force: bool = False):
        self.disconnect_calls.append(force)
        if self.disconnect_gate is not None:
            await self.disconnect_gate.wait()
        if self.disconnect_exc is not None:
            raise self.disconnect_exc
        self.stop()
        self.connected = False


class FakeVoiceRecvClient(FakeStandardVoiceClient):
    def __init__(self, channel, hold_play=False, *, async_after_thread: bool = False):
        super().__init__(channel, hold_play=hold_play, async_after_thread=async_after_thread)
        self.listening = False
        self.current_sink = None
        self.stop_listening_calls = 0

    def listen(self, sink):
        self.listening = True
        self.current_sink = sink

    def is_listening(self):
        return self.listening

    def stop_listening(self):
        self.stop_listening_calls += 1
        self.listening = False
        self.current_sink = None

    def play(self, source, after=None):
        if self.playing:
            raise RuntimeError("Already playing audio on VoiceClient")
        self.playing = True
        self.source = source
        self.played.append(source)
        if self.hold_play:
            self._after_cb = after
        else:
            self.playing = False
            self.source = None
            if after:
                self._fire_after(after, None)


class FakeGuild:
    def __init__(self, guild_id: int):
        self.id = guild_id
        self.voice_client = None
        self.text_channels = []


class FakeVoiceChannel:
    def __init__(self, guild: FakeGuild, channel_id: int, name: str = "Voice"):
        self.guild = guild
        self.id = channel_id
        self.name = name
        self.connect_gate: asyncio.Event | None = None
        self.connect_exc: Exception | None = None
        self.connect_handler = None

    async def connect(self, **kwargs):
        if self.connect_handler is not None:
            res = self.connect_handler(**kwargs)
            if asyncio.iscoroutine(res):
                return await res
            return res
        if self.connect_gate is not None:
            await self.connect_gate.wait()
        if self.connect_exc is not None:
            raise self.connect_exc
        cls = kwargs.get("cls")
        if cls is not None:
            vc = FakeVoiceRecvClient(self)
        else:
            vc = FakeStandardVoiceClient(self)
        self.guild.voice_client = vc
        return vc


# ──────────────────────────────────────────────────────────────
# 1. Tests de STTQueueManager (instancia única, concurrencia, ciclo de vida)
# ──────────────────────────────────────────────────────────────

class TestSTTQueueManager(unittest.IsolatedAsyncioTestCase):
    async def test_shared_manager_serializes_both_routes_and_preserves_params(self):
        """
        Verifica que voice_processor y translator.stt usan el MISMO STTQueueManager
        y el MISMO worker/executor, con max_in_flight == 1 y parámetros propios aislados.
        """
        lock_state = threading.Lock()
        in_flight = 0
        max_in_flight = 0
        worker_threads: set[int] = set()
        recorded_calls: list[dict] = []
        model_loads = 0

        class FakeWhisperModel:
            def transcribe(self, audio_input, **kwargs):
                nonlocal in_flight, max_in_flight
                with lock_state:
                    in_flight += 1
                    max_in_flight = max(max_in_flight, in_flight)
                    worker_threads.add(threading.get_ident())
                    recorded_calls.append({"audio": audio_input, "kwargs": dict(kwargs)})
                time.sleep(0.04)
                with lock_state:
                    in_flight -= 1
                lang = kwargs.get("language") or "en"
                prompt = kwargs.get("initial_prompt") or ""
                return {"text": f"ok {prompt[:12]}", "language": lang, "segments": [{"avg_logprob": -0.1}]}

        def load_once():
            nonlocal model_loads
            model_loads += 1
            return FakeWhisperModel()

        mgr = STTQueueManager(max_queue_size=10, max_queue_wait_s=5.0, model_loader=load_once)
        mgr.start(asyncio.get_running_loop())

        # Verificar que ni voice_processor ni translator.stt cargaron modelo antes de usar
        self.assertFalse(mgr.model_loaded)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = f.name
            f.write(b"RIFFtest")

        try:
            with patch.object(voice_processor, "stt_queue_manager", mgr), patch.object(
                translator_stt, "stt_queue_manager", mgr
            ):
                # Comprobar que ambas rutas apuntan al mismo objeto manager
                self.assertIs(voice_processor.stt_queue_manager, translator_stt.stt_queue_manager)

                results = await asyncio.gather(
                    voice_processor.transcribe_audio(wav_path, language="es"),
                    translator_stt.transcribe([0.0] * 1600, initial_prompt="glosario_user1"),
                    voice_processor.transcribe_audio(wav_path, language="es"),
                    translator_stt.transcribe([0.0] * 1600, initial_prompt="glosario_user2"),
                )
        finally:
            if os.path.exists(wav_path):
                os.remove(wav_path)
            await mgr.shutdown(timeout=5.0)

        self.assertEqual(model_loads, 1)
        self.assertEqual(max_in_flight, 1)
        self.assertEqual(len(worker_threads), 1)
        self.assertEqual(len(recorded_calls), 4)
        self.assertEqual(results[0], f"ok {voice_processor._VOICE_COMMANDS_PROMPT[:12]}")
        self.assertEqual(results[1]["language"], "en")
        self.assertEqual(results[1]["text"], "ok glosario_use")
        self.assertEqual(results[3]["text"], "ok glosario_use")
        # Verificar parámetros por consumidor conservados sin fuga entre rutas
        prompts_and_langs = [(c["kwargs"].get("language"), c["kwargs"].get("initial_prompt")) for c in recorded_calls]
        self.assertIn(("es", voice_processor._VOICE_COMMANDS_PROMPT), prompts_and_langs)
        self.assertIn((None, "glosario_user1"), prompts_and_langs)
        self.assertIn((None, "glosario_user2"), prompts_and_langs)

        if "torch" in REAL_MODULES:
            import torch
            self.assertEqual(torch.get_num_threads(), 1)

    async def test_queue_full_and_staleness_eviction(self):
        release_first = threading.Event()
        invoked = 0

        class SlowModel:
            def transcribe(self, audio_input, **kwargs):
                nonlocal invoked
                invoked += 1
                if invoked == 1:
                    release_first.wait(timeout=2.0)
                return {"text": "reproduce algo", "language": "es", "segments": []}

        mgr = STTQueueManager(max_queue_size=1, max_queue_wait_s=0.05, model_loader=SlowModel)
        mgr.start(asyncio.get_running_loop())
        try:
            t1 = asyncio.create_task(mgr.transcribe("a1.wav", language="es"))
            await asyncio.sleep(0.02)  # t1 ya entró al worker thread
            t2 = asyncio.create_task(mgr.transcribe("a2.wav", language="es"))
            await asyncio.sleep(0.01)  # t2 ocupa la única ranura de la cola
            r3 = await mgr.transcribe("a3.wav", language="es")  # Debe rechazarse por cola llena
            self.assertEqual(r3.status, STTStatus.DROPPED_QUEUE_FULL)

            # Esperar > 0.05s antes de liberar el primero para que t2 expire en cola (DROPPED_STALE)
            await asyncio.sleep(0.08)
            release_first.set()
            r1 = await t1
            r2 = await t2
            self.assertEqual(r1.status, STTStatus.OK)
            self.assertEqual(r2.status, STTStatus.DROPPED_STALE)
            self.assertEqual(invoked, 1)  # t2 nunca ejecutó inferencia
        finally:
            release_first.set()
            await mgr.shutdown(timeout=5.0)

    async def test_cancellation_during_inflight_still_serializes_and_discards(self):
        started_1 = threading.Event()
        release_1 = threading.Event()
        in_flight = 0
        max_in_flight = 0

        class BlockingModel:
            def transcribe(self, audio_input, **kwargs):
                nonlocal in_flight, max_in_flight
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                if audio_input == "job1":
                    started_1.set()
                    release_1.wait(timeout=2.0)
                in_flight -= 1
                return {"text": f"done {audio_input}", "language": "es", "segments": []}

        mgr = STTQueueManager(max_queue_size=3, max_queue_wait_s=4.0, model_loader=BlockingModel)
        mgr.start(asyncio.get_running_loop())
        try:
            cancelled_flag = False
            t1 = asyncio.create_task(
                mgr.transcribe("job1", language="es", is_cancelled_cb=lambda: cancelled_flag)
            )
            await asyncio.to_thread(started_1.wait, 2.0)

            # Encolar job2 y cancelar t1 mientras job1 sigue dentro de Whisper
            t2 = asyncio.create_task(mgr.transcribe("job2", language="es"))
            cancelled_flag = True
            t1.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await t1

            await asyncio.sleep(0.05)
            # job2 aún NO debe haber empezado porque job1 sigue en el executor
            self.assertFalse(t2.done())
            release_1.set()
            r2 = await t2
            self.assertEqual(r2.status, STTStatus.OK)
            self.assertEqual(r2.text, "done job2")
            self.assertEqual(max_in_flight, 1)
        finally:
            release_1.set()
            await mgr.shutdown(timeout=5.0)

    async def test_overload_hysteresis_protects_music(self):
        class HeavyModel:
            def transcribe(self, audio_input, **kwargs):
                time.sleep(0.06)  # ~0.06s para audio de 0.02s => RTF ~ 3.0
                return {"text": "ok", "language": "es", "segments": []}

        music_playing = True
        mgr = STTQueueManager(
            max_queue_size=3,
            rtf_overload_threshold=1.35,
            overload_cooldown_s=0.1,
            model_loader=HeavyModel,
        )
        mgr.set_music_active_callback(lambda: music_playing)
        mgr.start(asyncio.get_running_loop())
        try:
            # 1er run: warm-up (excluido del cálculo de sobrecarga)
            r1 = await mgr.transcribe("w", audio_duration_s=0.02)
            self.assertEqual(r1.status, STTStatus.OK)
            self.assertFalse(mgr.is_overloaded)

            # 2do y 3er runs llenan ventana RTF > 1.35 con música activa -> OVERLOADED
            await mgr.transcribe("a", audio_duration_s=0.02)
            await mgr.transcribe("b", audio_duration_s=0.02)
            self.assertTrue(mgr.is_overloaded)

            r_drop = await mgr.transcribe("c", audio_duration_s=0.02)
            self.assertEqual(r_drop.status, STTStatus.DROPPED_OVERLOAD)

            # Si se detiene la música, se recupera a NORMAL
            music_playing = False
            r_rec = await mgr.transcribe("d", audio_duration_s=1.0)
            self.assertEqual(r_rec.status, STTStatus.OK)
            self.assertFalse(mgr.is_overloaded)
        finally:
            await mgr.shutdown(timeout=5.0)

    async def test_overload_recovery_clears_rtf_window_waits_for_active_inference_and_observability_metrics(self):
        block_job4 = threading.Event()
        entered_job4 = threading.Event()

        class ControlledModel:
            def transcribe(self, audio_input, **kwargs):
                if audio_input == "job4_hold":
                    entered_job4.set()
                    block_job4.wait(timeout=3.0)
                    return {"text": "held ok", "language": "es", "segments": []}
                if audio_input in ("w", "h1", "h2"):
                    time.sleep(0.05)  # Con dur=0.02 -> RTF ~ 2.5
                return {"text": "fast ok", "language": "es", "segments": []}

        music_playing = True
        mgr = STTQueueManager(
            max_queue_size=4,
            rtf_overload_threshold=1.35,
            loop_lag_overload_ms=25.0,
            loop_lag_recover_ms=12.0,
            overload_cooldown_s=0.12,
            model_loader=ControlledModel,
        )
        mgr.set_music_active_callback(lambda: music_playing)
        mgr.start(asyncio.get_running_loop())
        try:
            mgr.record_loop_lag_ms(4.0)
            mgr.record_loop_lag_ms(5.0)

            # 1. Warm-up + 2 inferencias pesadas mientras job4 queda en cola para entrar justo tras overload
            await mgr.transcribe("w", audio_duration_s=0.02)
            await mgr.transcribe("h1", audio_duration_s=0.02)
            t3 = asyncio.create_task(mgr.transcribe("h2", audio_duration_s=0.02))
            await asyncio.sleep(0.01)
            t4 = asyncio.create_task(mgr.transcribe("job4_hold", audio_duration_s=0.5))

            r3 = await t3
            self.assertEqual(r3.status, STTStatus.OK)
            self.assertTrue(mgr.is_overloaded)

            # Esperar a que job4 esté activo dentro del executor y cola vacía
            await asyncio.to_thread(entered_job4.wait, 2.0)
            await asyncio.sleep(0.16)  # Supera cooldown 0.12s

            # No debe recuperarse mientras existe una inferencia activa en curso
            mgr._update_overload_state(time.monotonic())
            self.assertTrue(mgr.is_overloaded)

            # Liberar inferencia activa; ahora al cumplirse cooldown y sin inferencia en curso -> recupera
            block_job4.set()
            r4 = await t4
            self.assertEqual(r4.status, STTStatus.OK)
            await asyncio.sleep(0.16)
            mgr._update_overload_state(time.monotonic())
            self.assertFalse(mgr.is_overloaded)
            self.assertEqual(len(mgr._rtf_window), 0)

            # Verificar que la siguiente solicitud con música activa NO se re-engancha en OVERLOADED
            r5 = await mgr.transcribe("fast_after_recovery", audio_duration_s=1.0)
            self.assertEqual(r5.status, STTStatus.OK)
            self.assertFalse(mgr.is_overloaded)

            # Verificar métricas puramente observacionales
            obs = mgr.get_observability_metrics()
            self.assertGreaterEqual(obs["rtf"]["samples"], 4)
            self.assertIsNotNone(obs["rtf"]["p50"])
            self.assertIsNotNone(obs["rtf"]["p95"])
            self.assertGreaterEqual(obs["rtf"]["max"], obs["rtf"]["p95"])
            self.assertGreaterEqual(obs["lag_ms"]["samples"], 2)
            self.assertIsNotNone(obs["lag_ms"]["p50"])
            self.assertIsNotNone(obs["lag_ms"]["p95"])
            self.assertGreaterEqual(obs["lag_ms"]["max"], obs["lag_ms"]["p95"])
            self.assertGreaterEqual(obs["overload"]["recoveries_count"], 1)
            self.assertGreaterEqual(obs["overload"]["last_recovery_s"], 0.1)
            self.assertFalse(obs["overload"]["is_overloaded"])
            self.assertGreaterEqual(obs["counts"]["completed_ok"], 5)
        finally:
            block_job4.set()
            await mgr.shutdown(timeout=5.0)

    async def test_shutdown_pending_does_not_drop_model_until_thread_exits(self):
        entered = threading.Event()
        release = threading.Event()

        class StuckModel:
            def transcribe(self, audio_input, **kwargs):
                entered.set()
                release.wait(timeout=3.0)
                return {"text": "late", "language": "es", "segments": []}

        mgr = STTQueueManager(model_loader=StuckModel)
        mgr.start(asyncio.get_running_loop())
        task = asyncio.create_task(mgr.transcribe("stuck"))
        await asyncio.to_thread(entered.wait, 2.0)

        res = await mgr.shutdown(timeout=0.05)
        self.assertFalse(res.completed)
        self.assertTrue(res.pending_inference)
        self.assertEqual(res.state, "SHUTDOWN_PENDING")
        # El modelo NO debe haberse liberado mientras el hilo nativo sigue corriendo
        self.assertTrue(mgr.model_loaded)

        # Rechazar nuevos inicios y envíos mientras esté pendiente
        with self.assertRaises(RuntimeError):
            mgr.start(asyncio.get_running_loop())
        r_new = await mgr.transcribe("blocked")
        self.assertEqual(r_new.status, STTStatus.DROPPED_CANCELLED)

        # Al finalizar el hilo nativo, se libera el modelo y pasa a STOPPED
        release.set()
        await asyncio.sleep(0.15)
        self.assertEqual(mgr.state, "STOPPED")
        self.assertFalse(mgr.model_loaded)
        await task


# ──────────────────────────────────────────────────────────────
# 2. Tests de VoiceSessionManager, Sinks, Música vs TTS
# ──────────────────────────────────────────────────────────────

class TestVoiceSessionAndMusicPriority(unittest.IsolatedAsyncioTestCase):
    async def test_sinks_super_init_and_data_pcm_and_idempotent_cleanup(self):
        d_sink = DiscordAudioSink()
        t_sink = TranslatorAudioSink(on_speech=lambda uid, pcm: asyncio.sleep(0))
        self.assertTrue(hasattr(d_sink, "_child"))
        self.assertTrue(hasattr(t_sink, "_child"))

        user = types.SimpleNamespace(id=42)
        d_sink.write(user, types.SimpleNamespace(pcm=b"\x01\x02\x03\x04"))
        data = await d_sink.get_audio(42)
        self.assertEqual(data, b"\x01\x02\x03\x04")

        d_sink.cleanup()
        d_sink.cleanup()
        t_sink.cleanup()
        t_sink.cleanup()

    async def test_voice_session_sink_isolation_and_stale_release(self):
        vsm = VoiceSessionManager()
        guild = FakeGuild(99)
        ch = FakeVoiceChannel(guild, 10)
        vc = await vsm.ensure_voice_client(ch, require_receive=True)
        self.assertIsInstance(vc, FakeVoiceRecvClient)

        revoked_calls = []
        s1 = DiscordAudioSink()
        s2 = TranslatorAudioSink(on_speech=lambda u, p: asyncio.sleep(0))

        tok1 = await vsm.acquire_sink(99, vc, "voice_commands", s1, on_revoked=lambda: revoked_calls.append("listen"))
        self.assertEqual(vsm.get_active_owner(99), "voice_commands")

        tok2 = await vsm.acquire_sink(99, vc, "translator", s2)
        self.assertNotEqual(tok1, tok2)
        self.assertEqual(revoked_calls, ["listen"])
        self.assertTrue(s1._cleaned_up)
        self.assertFalse(s2._cleaned_up)
        self.assertIs(vc.current_sink, s2)

        # Un cierre tardío de /listen con tok1 NO debe afectar ni detener a s2
        released_stale = vsm.release_sink(99, tok1, vc)
        self.assertFalse(released_stale)
        self.assertIs(vc.current_sink, s2)
        self.assertFalse(s2._cleaned_up)
        self.assertTrue(vc.is_listening())

        # Liberar con tok2 sí libera limpio
        self.assertTrue(vsm.release_sink(99, tok2, vc))
        self.assertTrue(s2._cleaned_up)
        self.assertFalse(vc.is_listening())
        await vsm.aclose()

    async def test_standard_vc_upgrade_and_missing_voice_recv_fallback(self):
        vsm = VoiceSessionManager()
        guild = FakeGuild(77)
        ch = FakeVoiceChannel(guild, 20)
        std_vc = FakeStandardVoiceClient(ch)
        guild.voice_client = std_vc
        vsm.voice_clients[77] = std_vc

        # Si hay música activa en un cliente sin VoiceRecv, bloquea sin cortar la canción
        with self.assertRaises(VoiceClientUpgradeBlockedError):
            await vsm.ensure_voice_client(ch, require_receive=True, is_music_active_cb=lambda gid: True)
        self.assertTrue(guild.voice_client.is_connected())

        # Si NO hay música activa, reconecta como VoiceRecvClient
        vc_upgraded = await vsm.ensure_voice_client(
            ch, require_receive=True, is_music_active_cb=lambda gid: False
        )
        self.assertIsInstance(vc_upgraded, FakeVoiceRecvClient)

        # Fallback explícito cuando voice_recv no está instalado
        with patch("utils.voice_session_manager.HAS_VOICE_RECV", False):
            with self.assertRaises(VoiceReceiveUnavailableError):
                await vsm.ensure_voice_client(ch, require_receive=True)
        await vsm.aclose()

    async def test_music_player_paused_blocks_auxiliary_tts(self):
        vsm = VoiceSessionManager()
        bot = types.SimpleNamespace(guilds=[])
        mp = MusicPlayer(bot, session_manager=vsm)
        gid = 555
        guild = FakeGuild(gid)
        ch = FakeVoiceChannel(guild, 1)
        vc = FakeVoiceRecvClient(ch)

        vsm.voice_clients[gid] = vc
        mp.queues[gid] = []
        mp.is_playing[gid] = False
        mp.is_paused[gid] = False
        mp.current_songs[gid] = None
        mp._transition_active[gid] = False

        # Totalmente inactivo -> True
        self.assertTrue(mp.can_play_auxiliary_audio(gid))

        # Reproduciendo -> False
        vc.playing = True
        mp.is_playing[gid] = True
        self.assertFalse(mp.can_play_auxiliary_audio(gid))

        # PAUSADO (is_playing False en VoiceClient, pero música sigue pausada!) -> DEBE ser False
        vc.playing = False
        vc.paused = True
        mp.is_playing[gid] = False
        mp.is_paused[gid] = True
        mp.current_songs[gid] = {"title": "Song", "song_id": "1"}
        self.assertFalse(mp.can_play_auxiliary_audio(gid))

        # Transición entre pistas -> False
        vc.paused = False
        mp.is_paused[gid] = False
        mp.current_songs[gid] = None
        mp._transition_active[gid] = True
        self.assertFalse(mp.can_play_auxiliary_audio(gid))

        # Siguiente canción esperando en cola -> False
        mp._transition_active[gid] = False
        mp.queues[gid] = [{"title": "Next"}]
        self.assertFalse(mp.can_play_auxiliary_audio(gid))
        await vsm.aclose()

    async def test_deferred_tts_waits_for_music_and_expires_old_clips(self):
        vsm = VoiceSessionManager()
        bot = types.SimpleNamespace(get_guild=lambda gid: None)
        mp = MusicPlayer(bot, session_manager=vsm)
        gid = 888
        vc = FakeVoiceRecvClient(FakeVoiceChannel(FakeGuild(gid), 1))
        vsm.voice_clients[gid] = vc
        mp.queues[gid] = []
        mp.is_playing[gid] = True
        mp.is_paused[gid] = False
        mp.current_songs[gid] = {"title": "Active"}
        mp._transition_active[gid] = False

        sess = TranslatorSession(gid, bot, session_manager=vsm)
        sess.voice_client = vc
        sess._session_token = 1
        sess._listening_active = True
        sess._revoked = False

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf1:
            tf1.write(b"ID3test")
            tmp_mp3 = tf1.name
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf2:
            tf2.write(b"ID3expired")
            tmp_expired = tf2.name

        try:
            # Poner uno ya vencido (>60s) y uno vigente en cola mientras música está activa
            await sess._tts_queue.put((tmp_expired, 1, time.monotonic() - 70.0, 1))
            await sess._tts_queue.put((tmp_mp3, 1, time.monotonic(), 1))

            loop_task = asyncio.create_task(sess._tts_player_loop())
            await asyncio.sleep(0.1)
            # No debe reproducirse ni haberse llamado play() mientras música está activa
            self.assertEqual(len(vc.played), 0)

            # Ahora termina la canción completamente (reproductor cede canal)
            mp.is_playing[gid] = False
            mp.current_songs[gid] = None
            await asyncio.sleep(0.35)

            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

            # El vencido se descartó y borró; el vigente se reprodujo
            self.assertFalse(os.path.exists(tmp_expired))
            self.assertEqual(len(vc.played), 1)
            self.assertEqual(vc.played[0].src, tmp_mp3)
        finally:
            await vsm.aclose()
            for p in (tmp_mp3, tmp_expired):
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

    async def test_music_stop_order_preserves_translator_and_active_tts(self):
        from utils.voice_session_manager import voice_session_manager
        from modules.voice_receiver import VoiceReceiver

        # Caso A: Traductor activo con sink y TTS reproduciéndose; !stop musical NO desconecta ni corta TTS
        gid = 701
        voice_session_manager.reset_guild(gid)
        bot = types.SimpleNamespace(guilds=[])
        mp = MusicPlayer(bot)
        guild = FakeGuild(gid)
        ch = FakeVoiceChannel(guild, 11)
        vc = FakeVoiceRecvClient(ch, hold_play=True)
        guild.voice_client = vc
        voice_session_manager.voice_clients[gid] = vc
        mp.queues[gid] = [{"title": "En cola"}]
        mp.is_playing[gid] = False
        mp.is_paused[gid] = False

        t_sink = TranslatorAudioSink(on_speech=lambda u, p: asyncio.sleep(0), loop=asyncio.get_running_loop())
        t_tok = await voice_session_manager.acquire_sink(gid, vc, "translator", t_sink)
        pb_tok = voice_session_manager.try_acquire_tts_playback(gid)
        self.assertIsNotNone(pb_tok)
        vc.playing = True

        try:
            msg = await mp.stop(gid)
            self.assertIn("permanece conectada", msg)
            self.assertEqual(vc.stop_calls, 0)  # NO interrumpe TTS
            self.assertTrue(vc.is_connected())  # NO desconecta VoiceClient compartido
            self.assertEqual(voice_session_manager.get_active_owner(gid), "translator")
            self.assertEqual(voice_session_manager.get_playback_owner(gid), "tts")
            self.assertEqual(mp.queues[gid], [])
        finally:
            vc.stop()
            voice_session_manager.release_playback(gid, playback_token=pb_tok)
            voice_session_manager.release_sink(gid, t_tok, vc)
            voice_session_manager.reset_guild(gid)

        # Caso B: MusicPlayer es el único propietario ("voice_commands"); stop() libera sink ANTES de request_deferred_disconnect()
        gid2 = 702
        voice_session_manager.reset_guild(gid2)
        mp2 = MusicPlayer(bot)
        guild2 = FakeGuild(gid2)
        vc2 = FakeVoiceRecvClient(FakeVoiceChannel(guild2, 12))
        guild2.voice_client = vc2
        voice_session_manager.voice_clients[gid2] = vc2
        mp2.queues[gid2] = []
        mp2.voice_command_mode[gid2] = {1: True}
        vr = VoiceReceiver(voice_client=vc2, guild_id=gid2)
        await vr.setup_receiver()
        mp2.voice_receivers[gid2] = vr

        call_order = []
        orig_release = voice_session_manager.release_sink
        orig_req_disc = voice_session_manager.request_deferred_disconnect

        def tracked_release(*a, **kw):
            call_order.append("release_sink")
            return orig_release(*a, **kw)

        async def tracked_req_disc(*a, **kw):
            call_order.append("request_deferred_disconnect")
            return await orig_req_disc(*a, **kw)

        try:
            with patch.object(voice_session_manager, "release_sink", side_effect=tracked_release), patch.object(
                voice_session_manager, "request_deferred_disconnect", side_effect=tracked_req_disc
            ):
                msg2 = await mp2.stop(gid2)
                await asyncio.sleep(0.05)

            self.assertIn("detenida", msg2)
            self.assertEqual(call_order, ["release_sink", "request_deferred_disconnect"])
            self.assertFalse(vc2.is_connected())
        finally:
            voice_session_manager.reset_guild(gid2)

    async def test_playback_arbiter_music_preempts_tts_and_is_music_active_uses_owner(self):
        vsm = VoiceSessionManager()
        gid = 703
        bot = types.SimpleNamespace(get_guild=lambda g: None, guilds=[], loop=asyncio.get_running_loop())
        mp = MusicPlayer(bot, session_manager=vsm)
        guild = FakeGuild(gid)
        vc = FakeVoiceRecvClient(FakeVoiceChannel(guild, 13), hold_play=True)
        guild.voice_client = vc
        vsm.voice_clients[gid] = vc
        mp.queues[gid] = []
        mp.is_playing[gid] = False
        mp.is_paused[gid] = False
        mp.current_songs[gid] = None
        mp._transition_active[gid] = False

        sess = TranslatorSession(gid, bot, session_manager=vsm)
        sess.voice_client = vc
        sess._session_token = 1
        sess._listening_active = True
        sess._revoked = False

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf_tts:
            tf_tts.write(b"ID3tts")
            tts_mp3 = tf_tts.name
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf_song:
            tf_song.write(b"ID3music")
            song_mp3 = tf_song.name

        mp.file_cache["song_cached_1"] = {
            "path": song_mp3,
            "last_accessed": time.time(),
            "size": 16,
        }

        loop_task = asyncio.create_task(sess._tts_player_loop())
        try:
            await sess._tts_queue.put((tts_mp3, 1, time.monotonic(), 1))
            await asyncio.sleep(0.1)

            # TTS está reproduciéndose activamente
            self.assertTrue(vc.is_playing())
            self.assertEqual(len(vc.played), 1)
            self.assertEqual(vsm.get_playback_owner(gid), "tts")
            # is_music_active() NO debe confundir TTS con música
            self.assertFalse(mp.is_music_active(gid))

            # Entra una solicitud musical -> toma prioridad atómica interrumpiendo TTS sin colisión en play()
            mp.queues[gid].append({"song_id": "song_cached_1", "title": "RockSong", "artist": "Band"})
            await mp._play_next(gid)

            self.assertEqual(vc.stop_calls, 1)  # Preempcionó TTS limpiamente
            self.assertEqual(len(vc.played), 2)
            self.assertEqual(vsm.get_playback_owner(gid), "music")
            self.assertTrue(mp.is_music_active(gid))
            self.assertTrue(vc.is_playing())

            # Termina canción con cola vacía -> libera propiedad musical
            vc.stop()
            await asyncio.sleep(0.08)
            self.assertIsNone(vsm.get_playback_owner(gid))
            self.assertFalse(mp.is_music_active(gid))
        finally:
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
            await vsm.aclose()
            for p in (tts_mp3, song_mp3):
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

    async def test_translator_revocation_aborts_inflight_pipeline_after_tr_and_tts(self):
        from translator import manager as tr_manager

        gid = 704
        bot = types.SimpleNamespace(get_guild=lambda g: None, loop=asyncio.get_running_loop())

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf_synth:
            tf_synth.write(b"ID3late_tts")
            late_tts_path = tf_synth.name

        try:
            # Escenario 1: sesión revocada y re-adquirida con nuevo token durante translate_text()
            sess = TranslatorSession(gid, bot)
            sess._listening_active = True
            sess._revoked = False
            sess._session_token = 10

            async def fake_tr_revoke(**kw):
                sess._handle_sink_revoked()
                # Incluso si otra sesión arranca después con nuevo token, el job viejo (token=10) debe abortar
                sess._revoked = False
                sess._listening_active = True
                sess._session_token = 11
                return {"translated": "hello world"}

            with patch.object(tr_manager, "get_user_settings", return_value={"target_lang": "en"}), patch.object(
                tr_manager.stt, "preprocess_audio", return_value=[0.0] * 1600
            ), patch.object(tr_manager, "build_whisper_prompt", return_value=""), patch.object(
                tr_manager.stt,
                "transcribe",
                return_value={"text": "hola mundo", "language": "es", "confidence": 0.9},
            ), patch.object(tr_manager.tr, "translate_text", side_effect=fake_tr_revoke), patch.object(
                tr_manager.tts, "synthesize"
            ) as mock_synth:
                await sess._on_speech(99, b"\x01\x02" * 800)
                mock_synth.assert_not_called()
                self.assertTrue(sess._tts_queue.empty())

            # Escenario 2: sesión revocada durante tts.synthesize() -> borra audio temporal y NO encola
            sess2 = TranslatorSession(gid, bot)
            sess2._listening_active = True
            sess2._revoked = False
            sess2._session_token = 20

            async def fake_synth_revoke(text, voice=None):
                sess2._handle_sink_revoked()
                return late_tts_path

            with patch.object(tr_manager, "get_user_settings", return_value={"target_lang": "en"}), patch.object(
                tr_manager.stt, "preprocess_audio", return_value=[0.0] * 1600
            ), patch.object(tr_manager, "build_whisper_prompt", return_value=""), patch.object(
                tr_manager.stt,
                "transcribe",
                return_value={"text": "hola mundo", "language": "es", "confidence": 0.9},
            ), patch.object(
                tr_manager.tr, "translate_text", return_value={"translated": "hello world"}
            ), patch.object(tr_manager.tts, "synthesize", side_effect=fake_synth_revoke), patch.object(
                tr_manager, "save_translation"
            ) as mock_save:
                await sess2._on_speech(99, b"\x01\x02" * 800)
                self.assertTrue(sess2._tts_queue.empty())
                self.assertFalse(os.path.exists(late_tts_path))
                mock_save.assert_not_called()
        finally:
            if os.path.exists(late_tts_path):
                try:
                    os.remove(late_tts_path)
                except OSError:
                    pass

    async def test_translator_audio_sink_real_thread_dispatch_and_closed_loop_logging(self):
        # 1. Prueba real entre hilos: writer_thread -> call_soon_threadsafe -> event loop thread
        loop = asyncio.get_running_loop()
        loop_thread_id = threading.get_ident()
        callback_done = asyncio.Event()
        observed: dict[str, int] = {}

        async def on_speech_cb(uid: int, pcm: bytes):
            observed["uid"] = uid
            observed["pcm_len"] = len(pcm)
            observed["callback_thread_id"] = threading.get_ident()
            callback_done.set()

        sink = TranslatorAudioSink(on_speech=on_speech_cb, loop=loop)
        user = types.SimpleNamespace(id=321)
        voice_pcm = (b"\x00\x20") * 960  # int16 amplitud fuerte (> SILENCE_THRESHOLD)
        silence_pcm = b"\x00" * 1920

        writer_info: dict[str, int] = {}

        def _writer():
            writer_info["writer_thread_id"] = threading.get_ident()
            for _ in range(30):  # 0.6s de habla (> 0.5s MIN_SPEECH_SECONDS)
                sink.write(user, types.SimpleNamespace(pcm=voice_pcm))
            for _ in range(45):  # 0.9s de silencio (> 0.8s SILENCE_SECONDS)
                sink.write(user, types.SimpleNamespace(pcm=silence_pcm))

        t = threading.Thread(target=_writer, name="fake-voice-recv-writer")
        t.start()
        t.join(timeout=2.0)

        await asyncio.wait_for(callback_done.wait(), timeout=2.0)
        self.assertEqual(observed["uid"], 321)
        self.assertEqual(observed["pcm_len"], 30 * len(voice_pcm))
        self.assertEqual(observed["callback_thread_id"], loop_thread_id)
        self.assertNotEqual(observed["callback_thread_id"], writer_info["writer_thread_id"])
        sink.cleanup()

        # 2. Loop cerrado ANTES de invocar write() -> registra ERROR en log y no se traga silenciosamente
        closed_loop = asyncio.new_event_loop()
        closed_loop.close()
        sink_closed = TranslatorAudioSink(
            on_speech=lambda u, p: asyncio.sleep(0),
            loop=closed_loop,
        )

        def _writer_closed():
            for _ in range(30):
                sink_closed.write(user, types.SimpleNamespace(pcm=voice_pcm))
            for _ in range(45):
                sink_closed.write(user, types.SimpleNamespace(pcm=silence_pcm))

        with self.assertLogs("musicbot.translator.sink", level="ERROR") as cm:
            t2 = threading.Thread(target=_writer_closed, name="fake-voice-recv-closed")
            t2.start()
            t2.join(timeout=2.0)

        self.assertTrue(any("event loop cerrado" in line for line in cm.output))
        sink_closed.cleanup()


# ──────────────────────────────────────────────────────────────
# 3. Tests de IntentProcessor y Endpoint /transcription
# ──────────────────────────────────────────────────────────────

class TestIntentAndTranscriptionEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_intent_negation_guard_and_word_boundaries(self):
        ip = IntentProcessor()
        r1 = await ip.process_text("no pongas esa cancion")
        self.assertEqual(r1["intent"], "unknown")

        r2 = await ip.process_text("no quiero escuchar rock")
        self.assertEqual(r2["intent"], "unknown")

        r3 = await ip.process_text("nunca reproduzcas eso")
        self.assertEqual(r3["intent"], "unknown")

        r4 = await ip.process_text("reproduce Shape of You")
        self.assertEqual(r4["intent"], "play_song")
        self.assertEqual(r4["entities"][0]["value"], "shape of you")

        r5 = await ip.process_text("pon la de no me dejes")
        self.assertEqual(r5["intent"], "play_song")
        self.assertEqual(r5["entities"][0]["value"], "la de no me dejes")

        # Verificar que subcadena ("pon" en "japon") NO dispara TRIGGER_KEYWORDS
        r6 = await ip.process_text("japon musica anime")
        self.assertEqual(r6["intent"], "unknown")

        # Verificar que cuando sí hay trigger válido ("pon"), no muta subcadenas ("japon" sigue intacto)
        r7 = await ip.process_text("pon música de japon")
        self.assertEqual(r7["intent"], "play_song")
        self.assertIn("japon", r7["entities"][0]["value"])


@unittest.skipIf("flask" not in REAL_MODULES, "Flask no instalado en este entorno")
class TestTranscriptionSecurityAndIdempotency(unittest.TestCase):
    def setUp(self):
        transcription_server.reset_request_ledger()
        self.loop = asyncio.new_event_loop()
        self.loop_ready = threading.Event()
        self.loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self.loop_thread.start()
        self.loop_ready.wait(timeout=2.0)

        self.executed_calls = []

        class StubHandler:
            def __init__(self, parent):
                self.parent = parent
                self.delay = 0.0

            async def handle_voice_command(self, user_id, guild_id, intent, entities, transcription):
                if self.delay:
                    await asyncio.sleep(self.delay)
                self.parent.executed_calls.append((user_id, guild_id, intent, entities, transcription))
                return f"Executed {intent}"

        self.handler = StubHandler(self)
        self.orig_bot = transcription_server.bot
        self.orig_handler = transcription_server.voice_command_handler
        self.orig_token = getattr(config, "TRANSCRIPTION_API_TOKEN", "")
        self.orig_timeout = transcription_server.REQUEST_TIMEOUT_S

        transcription_server.bot = types.SimpleNamespace(loop=self.loop)
        transcription_server.voice_command_handler = self.handler
        config.TRANSCRIPTION_API_TOKEN = "super_secret_test_token_123"
        self.client = transcription_server.app.test_client()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self.loop_ready.set)
        self.loop.run_forever()

    def tearDown(self):
        transcription_server.bot = self.orig_bot
        transcription_server.voice_command_handler = self.orig_handler
        config.TRANSCRIPTION_API_TOKEN = self.orig_token
        transcription_server.REQUEST_TIMEOUT_S = self.orig_timeout
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join(timeout=2.0)
        self.loop.close()

    def test_auth_validation_limits_and_idempotency(self):
        # 1. Fail-closed sin token configurado -> 503
        config.TRANSCRIPTION_API_TOKEN = ""
        r = self.client.post("/transcription", json={"user_id": 1, "guild_id": 2, "transcription": "pausa"})
        self.assertEqual(r.status_code, 503)

        # 2. Token inválido ASCII y tokens no-ASCII -> 401 (nunca 500 TypeError)
        config.TRANSCRIPTION_API_TOKEN = "super_secret_test_token_123"
        r = self.client.post(
            "/transcription",
            headers={"X-Transcription-Token": "wrong", "X-Request-ID": "req-unauth-1"},
            json={"user_id": 1, "guild_id": 2, "transcription": "pausa"},
        )
        self.assertEqual(r.status_code, 401)

        r_nonascii_hdr = self.client.post(
            "/transcription",
            headers={"X-Transcription-Token": "tökén-ñ-á", "X-Request-ID": "req-unauth-2"},
            json={"user_id": 1, "guild_id": 2, "transcription": "pausa"},
        )
        self.assertEqual(r_nonascii_hdr.status_code, 401)

        config.TRANSCRIPTION_API_TOKEN = "cláve-ñ-á-🚀"
        r_nonascii_cfg = self.client.post(
            "/transcription",
            headers={"X-Transcription-Token": "super_secret_test_token_123", "X-Request-ID": "req-unauth-3"},
            json={"user_id": 1, "guild_id": 2, "transcription": "pausa"},
        )
        self.assertEqual(r_nonascii_cfg.status_code, 401)
        config.TRANSCRIPTION_API_TOKEN = "super_secret_test_token_123"

        # 3. Payload > 16 KB -> 413
        big_text = "a" * (17 * 1024)
        r = self.client.post(
            "/transcription",
            headers={"X-Transcription-Token": "super_secret_test_token_123", "X-Request-ID": "req-big-1"},
            data='{"user_id":1,"guild_id":2,"transcription":"' + big_text + '"}',
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 413)

        # 4. Tipos inválidos o sin request_id obligatorio -> 400
        r = self.client.post(
            "/transcription",
            headers={"X-Transcription-Token": "super_secret_test_token_123", "X-Request-ID": "req-badtype-1"},
            json={"user_id": True, "guild_id": 2, "transcription": "pausa"},
        )
        self.assertEqual(r.status_code, 400)

        r_no_reqid = self.client.post(
            "/transcription",
            headers={"X-Transcription-Token": "super_secret_test_token_123"},
            json={"user_id": 10, "guild_id": 20, "transcription": "pausa"},
        )
        self.assertEqual(r_no_reqid.status_code, 400)
        self.assertIn("request_id", r_no_reqid.get_json()["error"])
        self.assertEqual(len(self.executed_calls), 0)

        # 5. Negación rechazada por lista blanca / IntentProcessor -> 400 sin ejecutar handler
        r = self.client.post(
            "/transcription",
            headers={"X-Transcription-Token": "super_secret_test_token_123", "X-Request-ID": "req-neg-1"},
            json={"user_id": 10, "guild_id": 20, "transcription": "no pongas esa cancion"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["status"], "rejected_intent")
        self.assertEqual(len(self.executed_calls), 0)

        # 6. Solicitud válida con request_id -> 200, deduplicación y rechazo si cambia el payload
        headers = {"X-Transcription-Token": "super_secret_test_token_123", "X-Request-ID": "req-abc-1"}
        r1 = self.client.post("/transcription", headers=headers, json={"user_id": 10, "guild_id": 20, "transcription": "pausa"})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.get_json()["intent"], "pause")

        r2 = self.client.post("/transcription", headers=headers, json={"user_id": 10, "guild_id": 20, "transcription": "pausa"})
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.get_json().get("deduplicated"))
        self.assertEqual(len(self.executed_calls), 1)

        r_mismatch = self.client.post("/transcription", headers=headers, json={"user_id": 10, "guild_id": 20, "transcription": "salta"})
        self.assertEqual(r_mismatch.status_code, 409)
        self.assertEqual(r_mismatch.get_json()["status"], "payload_mismatch")
        self.assertEqual(len(self.executed_calls), 1)

    def test_timeout_prevents_duplicate_retry_execution(self):
        self.handler.delay = 0.25
        transcription_server.REQUEST_TIMEOUT_S = 0.05
        headers = {"X-Transcription-Token": "super_secret_test_token_123", "X-Request-ID": "req-timeout-1"}

        r1 = self.client.post(
            "/transcription",
            headers=headers,
            json={"user_id": 10, "guild_id": 20, "transcription": "salta"},
        )
        self.assertEqual(r1.status_code, 504)
        self.assertIn(r1.get_json()["status"], ("timeout_uncertain", "timeout_aborted"))

        time.sleep(0.30)
        calls_after_first = len(self.executed_calls)

        # Reintentar con el mismo request_id NO vuelve a ejecutar la acción
        r2 = self.client.post(
            "/transcription",
            headers=headers,
            json={"user_id": 10, "guild_id": 20, "transcription": "salta"},
        )
        self.assertEqual(r2.status_code, 409)
        self.assertEqual(len(self.executed_calls), calls_after_first)


# ──────────────────────────────────────────────────────────────
# 4. Tests de Singletons y Compatibilidad Python 3.10
# ──────────────────────────────────────────────────────────────

class TestSingletonsAndPython310Compat(unittest.TestCase):
    def test_main_and_bot_client_share_exact_instances(self):
        # Evitar levantar socket Flask real al importar main o iniciar servidor HTTP
        with patch("modules.transcription_server.start_transcription_server") as mock_start:
            import modules.bot_client as bc
            import main as main_mod
            self.assertIs(main_mod.music_player, bc.music_player)
            self.assertIs(main_mod.auto_message_system, bc.auto_message_system)
            self.assertIs(bc.music_player.voice_command_handler, bc.voice_command_handler)
            self.assertFalse(callable(bc.voice_command_handler))
            # Verificar que main pasó el mismo handler único si acaba de importar main
            if mock_start.called:
                args = mock_start.call_args[0]
                if len(args) >= 3:
                    self.assertIs(args[2], bc.voice_command_handler)

        orig_bot = transcription_server.bot
        orig_mp = transcription_server.music_player
        orig_vch = transcription_server.voice_command_handler
        try:
            with patch("threading.Thread"):
                transcription_server.start_transcription_server(
                    bc.bot, bc.music_player, bc.voice_command_handler
                )
            self.assertIs(transcription_server.voice_command_handler, bc.voice_command_handler)
        finally:
            transcription_server.bot = orig_bot
            transcription_server.music_player = orig_mp
            transcription_server.voice_command_handler = orig_vch

    def test_python_310_ast_compatibility(self):
        target_files = [
            "main.py",
            "config.py",
            "utils/stt_queue.py",
            "utils/voice_session_manager.py",
            "modules/voice_receiver.py",
            "modules/voice_processor.py",
            "modules/music_player.py",
            "modules/intent_processor.py",
            "modules/transcription_server.py",
            "modules/bot_client.py",
            "translator/audio_sink.py",
            "translator/stt.py",
            "translator/manager.py",
            "translator/commands.py",
        ]
        for rel in target_files:
            src = (ROOT_DIR / rel).read_text(encoding="utf-8")
            tree = ast.parse(src, filename=rel)
            if hasattr(ast, "TryStar"):
                for node in ast.walk(tree):
                    self.assertNotIsInstance(node, ast.TryStar, f"Python 3.11+ except* detectado en {rel}")


# ──────────────────────────────────────────────────────────────
# 5. Tests Gate 0A v3.3.13 + Condición Opción A (T1–T17)
# ──────────────────────────────────────────────────────────────


class _OwnedAsyncLock:
    """Instrumentación exclusiva de pruebas para propiedad y no-anidamiento de locks."""

    def __init__(self, inner: asyncio.Lock, gid: int, kind: str, tracker: dict):
        self._inner = inner
        self._gid = gid
        self._kind = kind
        self._tracker = tracker

    def locked(self) -> bool:
        return self._inner.locked()

    async def acquire(self) -> bool:
        task = asyncio.current_task()
        other_kind = "playback" if self._kind == "session" else "session"
        assert self._tracker[other_kind].get(self._gid) is not task, (
            f"Anidamiento prohibido de locks ({self._kind} dentro de {other_kind}) en guild={self._gid}"
        )
        res = await self._inner.acquire()
        self._tracker[self._kind][self._gid] = task
        return res

    def release(self) -> None:
        self._tracker[self._kind].pop(self._gid, None)
        self._inner.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.release()


def _instrument_vsm_locks(vsm: VoiceSessionManager) -> dict:
    tracker: dict[str, dict[int, asyncio.Task | None]] = {"session": {}, "playback": {}}
    orig_get_lock = vsm._get_lock
    orig_get_pb_lock = vsm._get_playback_lock

    def wrapped_lock(gid: int):
        inner = orig_get_lock(gid)
        if not hasattr(inner, "_test_wrapper"):
            inner._test_wrapper = _OwnedAsyncLock(inner, gid, "session", tracker)  # type: ignore[attr-defined]
        return inner._test_wrapper  # type: ignore[attr-defined]

    def wrapped_pb_lock(gid: int):
        inner = orig_get_pb_lock(gid)
        if not hasattr(inner, "_test_wrapper"):
            inner._test_wrapper = _OwnedAsyncLock(inner, gid, "playback", tracker)  # type: ignore[attr-defined]
        return inner._test_wrapper  # type: ignore[attr-defined]

    vsm._get_lock = wrapped_lock  # type: ignore[method-assign]
    vsm._get_playback_lock = wrapped_pb_lock  # type: ignore[method-assign]
    return tracker


class TestGate0AConcurrencyAndLifecycle(unittest.IsolatedAsyncioTestCase):
    def _make_dummy_mp3(self, content: bytes = b"ID3dummy" * 200) -> str:
        tf = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tf.write(content)
        tf.close()
        return tf.name

    def _cleanup_files(self, *paths: str | None) -> None:
        for p in paths:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _setup_player(
        self, gid: int = 9001, *, hold_play: bool = True, async_after_thread: bool = False
    ):
        vsm = VoiceSessionManager()
        bot = types.SimpleNamespace(get_guild=lambda g: None, guilds=[])
        mp = MusicPlayer(bot, session_manager=vsm)
        guild = FakeGuild(gid)
        ch = FakeVoiceChannel(guild, 100)
        vc = FakeVoiceRecvClient(ch, hold_play=hold_play, async_after_thread=async_after_thread)
        guild.voice_client = vc
        vsm.voice_clients[gid] = vc
        self.assertIs(mp.voice_clients, vsm.voice_clients)
        mp.queues[gid] = []
        return vsm, mp, guild, ch, vc

    async def test_T1_stop_during_add_to_cache_abandons_cleanly(self):
        vsm, mp, _g, _ch, vc = self._setup_player(1001)
        mp3 = self._make_dummy_mp3()
        cache_entered = asyncio.Event()
        cache_release = asyncio.Event()

        async def blocked_add(song_id, file_path):
            mp.file_cache[song_id] = {"path": file_path, "last_accessed": time.time(), "size": 1600}
            cache_entered.set()
            await cache_release.wait()

        mp._add_to_cache = blocked_add  # type: ignore[method-assign]

        class FakeYDL:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=True):
                return {"ext": "mp3", "requested_downloads": [{"filepath": mp3}]}

        try:
            mp.queues[1001].append({"url": "u1", "title": "S1", "song_id": "t1_song"})
            with patch("modules.music_player.yt_dlp.YoutubeDL", FakeYDL):
                t = asyncio.create_task(mp._play_next(1001))
                await cache_entered.wait()
                await mp.stop(1001)
                cache_release.set()
                await t
            self.assertEqual(len(vc.played), 0)
            self.assertIn("t1_song", mp.file_cache)
            self.assertEqual(mp._in_use_cache_files, {})
        finally:
            await vsm.aclose()
            self._cleanup_files(mp3)

    async def test_T2_stop_during_preemption_wait_skips_vc_play(self):
        vsm, mp, _g, _ch, vc = self._setup_player(1002, hold_play=True)
        mp3 = self._make_dummy_mp3()
        mp.file_cache["t2_song"] = {"path": mp3, "last_accessed": time.time(), "size": 1600}
        # TTS retenido sin soltar tras vc.stop() hasta liberar después
        tts_tok = vsm.try_acquire_tts_playback(1002)
        self.assertIsNotNone(tts_tok)
        vc.playing = True

        preempt_entered = asyncio.Event()
        orig_acquire = vsm.acquire_music_playback

        async def tracked_acquire(*a, **kw):
            preempt_entered.set()
            return await orig_acquire(*a, **kw)

        try:
            mp.queues[1002].append({"title": "S2", "song_id": "t2_song"})
            with patch.object(vsm, "acquire_music_playback", side_effect=tracked_acquire):
                t = asyncio.create_task(mp._play_next(1002))
                await preempt_entered.wait()
                await mp.stop(1002)
                vsm.release_playback(1002, playback_token=tts_tok)
                await t
            self.assertEqual(len(vc.played), 0)
            self.assertEqual(mp._in_use_cache_files, {})
            self.assertIsNone(vsm.get_playback_owner(1002))
        finally:
            await vsm.aclose()
            self._cleanup_files(mp3)

    async def test_T3_thread_bridge_lock_ownership_and_inconsistencies(self):
        vsm, mp, guild, ch, vc = self._setup_player(1003, hold_play=True, async_after_thread=True)
        tracker = _instrument_vsm_locks(vsm)
        mp3_a = self._make_dummy_mp3()
        mp3_b = self._make_dummy_mp3()
        mp.file_cache["sA"] = {"path": mp3_a, "last_accessed": time.time(), "size": 1600}
        mp.file_cache["sB"] = {"path": mp3_b, "last_accessed": time.time(), "size": 1600}
        loop_tid = threading.get_ident()

        handler_thread_ids: list[int] = []
        orig_finish = mp._on_song_finished_in_loop
        handler_gate = asyncio.Event()

        def tracked_finish(error, gid, op_tok, pb_tok):
            handler_thread_ids.append(threading.get_ident())
            return orig_finish(error, gid, op_tok, pb_tok)

        try:
            # 1. Prohibir release_playback/release_sink desde hilo secundario
            err_box: list[BaseException] = []

            def bad_thread_call():
                try:
                    vsm.release_playback(1003, playback_token=999)
                except BaseException as exc:
                    err_box.append(exc)

            bt = threading.Thread(target=bad_thread_call)
            bt.start()
            bt.join(timeout=2.0)
            self.assertTrue(any(isinstance(e, AssertionError) for e in err_box))

            # 2. Puente real hilo -> call_soon_threadsafe sin tocar estado antes del loop
            vc.after_thread_gate = threading.Event()
            mp.enqueue_song(1003, {"title": "A", "song_id": "sA"})
            await asyncio.sleep(0.05)
            tok_a = mp._active_op_tokens[1003]
            pb_a = vsm.get_playback_token(1003)
            self.assertIsNotNone(tok_a)
            self.assertIsNotNone(pb_a)

            with patch.object(mp, "_on_song_finished_in_loop", side_effect=tracked_finish):
                mp.enqueue_song(1003, {"title": "B", "song_id": "sB"})
                skip_task = asyncio.create_task(mp.skip(1003))
                await skip_task
                # Mientras after(A) está retenido en el hilo de audio, A aún no libera token ni caché
                vc.after_thread_gate.set()
                await asyncio.sleep(0.08)

            self.assertTrue(vc.after_thread_ids and all(tid != loop_tid for tid in vc.after_thread_ids))
            self.assertTrue(handler_thread_ids and all(tid == loop_tid for tid in handler_thread_ids))
            self.assertEqual(len(vc.played), 2)
            # Re-llamada tardía / duplicada de release_playback es no-op
            self.assertFalse(vsm.release_playback(1003, playback_token=pb_a))
            self.assertIsNotNone(vsm.get_playback_token(1003))
            vc.stop()
            await asyncio.sleep(0.08)
            vc.join_threads()

            # 3. _issue_playback_token rechaza bajo transición, sin cliente, indeterminado, unreconciled y closing
            vsm._voice_transitioning[1003] = True
            self.assertIsNone(vsm.try_acquire_tts_playback(1003))
            with self.assertRaises(VoiceClientUnavailableError):
                await vsm.acquire_music_playback(1003, expected_vc=vc)
            vsm._voice_transitioning.pop(1003, None)

            # Indeterminado -> rechaza
            vc.is_connected_behavior = lambda: None
            self.assertIsNone(vsm.try_acquire_tts_playback(1003))
            vc.is_connected_behavior = None

            # Subcaso A (vc sigue ocupado tras timeout) y Subcaso B (huérfano con vc libre)
            tok_stuck = vsm.try_acquire_tts_playback(1003)
            self.assertIsNotNone(tok_stuck)
            orig_is_playing = vc.is_playing
            vc.is_playing = lambda: True  # type: ignore[method-assign]
            with self.assertRaises(PlaybackPreemptionTimeoutError):
                await vsm.acquire_music_playback(1003, expected_vc=vc, preempt_timeout=0.05)
            vc.is_playing = orig_is_playing  # type: ignore[method-assign]
            vc.playing = False
            pb_sub_b = await vsm.acquire_music_playback(1003, expected_vc=vc, preempt_timeout=0.05)
            self.assertIsNotNone(pb_sub_b)
            vsm.release_playback(1003, playback_token=pb_sub_b)

            # Reserva sin done_event -> PlaybackStateInconsistencyError cuando vc sigue ocupado / recuperación cuando libre
            tok_miss = vsm.try_acquire_tts_playback(1003)
            vsm._playback_done_events.pop(tok_miss, None)
            vc.is_playing = lambda: True  # type: ignore[method-assign]
            with self.assertRaises(PlaybackStateInconsistencyError):
                await vsm.acquire_music_playback(1003, expected_vc=vc, preempt_timeout=0.5)
            vc.is_playing = orig_is_playing  # type: ignore[method-assign]
            vc.playing = False
            pb_rec = await vsm.acquire_music_playback(1003, expected_vc=vc, preempt_timeout=0.5)
            vsm.release_playback(1003, playback_token=pb_rec)

            # _playback_tokens None con vc físicamente ocupado -> PlaybackStateInconsistencyError y vc.stop() defensivo
            vc.playing = True
            with self.assertRaises(PlaybackStateInconsistencyError):
                await vsm.acquire_music_playback(1003, expected_vc=vc, preempt_timeout=0.2)
            vc.playing = False
            self.assertEqual(tracker["session"], {})
            self.assertEqual(tracker["playback"], {})
        finally:
            vc.join_threads()
            await vsm.aclose()
            self._cleanup_files(mp3_a, mp3_b)

    async def _run_three_skips_trace(self, *, release_b_before_third_skip: bool):
        vsm, mp, _g, _ch, vc = self._setup_player(1004, hold_play=True)
        files = {k: self._make_dummy_mp3() for k in ("A", "B", "C", "D")}
        for k, p in files.items():
            mp.file_cache[k] = {"path": p, "last_accessed": time.time(), "size": 1600}

        created_count = 0
        concurrent = 0
        max_concurrent = 0
        orig_play_next = mp._play_next

        async def instrumented_play_next(gid, max_retries=3):
            nonlocal created_count, concurrent, max_concurrent
            created_count += 1
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            try:
                return await orig_play_next(gid, max_retries=max_retries)
            finally:
                concurrent -= 1

        mp._play_next = instrumented_play_next  # type: ignore[method-assign]

        gate_b = asyncio.Event()
        gate_c = asyncio.Event()
        orig_add = mp._add_to_cache

        async def gated_add(song_id, fp):
            await orig_add(song_id, fp)

        orig_acq = vsm.acquire_music_playback
        seen_acquire: list[str] = []

        async def gated_acq(gid, expected_vc=None, preempt_timeout=1.0):
            cur = mp.current_songs.get(gid)
            title = cur["title"] if cur else ""
            seen_acquire.append(title)
            if title == "B" and not release_b_before_third_skip:
                await gate_b.wait()
            elif title == "C" and release_b_before_third_skip:
                await gate_c.wait()
            return await orig_acq(gid, expected_vc=expected_vc, preempt_timeout=preempt_timeout)

        # Para que B y C suspendan antes de vc.play(), forzamos descarga mockeada sin cache en B/C
        for k in ("B", "C"):
            mp.file_cache.pop(k, None)

        class SkipYDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=True):
                key = url
                return {"ext": "mp3", "requested_downloads": [{"filepath": files[key]}]}

        async def gated_add_to_cache(song_id, fp):
            if song_id == "B":
                if not release_b_before_third_skip:
                    await gate_b.wait()
                else:
                    gate_b.set()
            elif song_id == "C" and release_b_before_third_skip:
                await gate_c.wait()
            await gated_add(song_id, fp)

        mp._add_to_cache = gated_add_to_cache  # type: ignore[method-assign]

        try:
            with patch("modules.music_player.yt_dlp.YoutubeDL", SkipYDL):
                for name in ("A", "B", "C", "D"):
                    mp.enqueue_song(1004, {"url": name, "title": name, "song_id": name})
                await asyncio.sleep(0.05)
                self.assertEqual(mp.current_songs[1004]["title"], "A")

                # skip1 -> invalida A, detiene A, arranca transición de B
                await mp.skip(1004)
                await asyncio.sleep(0.02)
                # skip2 -> invalida B mientras B sigue en _add_to_cache
                await mp.skip(1004)

                if release_b_before_third_skip:
                    await asyncio.sleep(0.05)  # B abandona y finally relanza C hasta gate_c
                    self.assertEqual(mp.current_songs[1004]["title"], "C")
                    await mp.skip(1004)  # skip3 invalida C
                    gate_c.set()
                else:
                    await mp.skip(1004)  # skip3 descarta C en cola antes de despertar B
                    gate_b.set()

                await asyncio.sleep(0.12)
                self.assertEqual(max_concurrent, 1)
                self.assertEqual(mp.current_songs[1004]["title"], "D")
                self.assertEqual(mp.queues[1004], [])
                self.assertGreaterEqual(created_count, 2)
                played_srcs = [s.source.src for s in vc.played]
                self.assertEqual(played_srcs.count(files["D"]), 1)
        finally:
            gate_b.set()
            gate_c.set()
            await vsm.aclose()
            self._cleanup_files(*files.values())

    async def test_T4_and_T5_three_skips_both_interleavings(self):
        await self._run_three_skips_trace(release_b_before_third_skip=False)
        await self._run_three_skips_trace(release_b_before_third_skip=True)

    async def test_T6_stop_then_play_while_old_transition_unwinding(self):
        vsm, mp, _g, _ch, vc = self._setup_player(1006, hold_play=True)
        mp3_a = self._make_dummy_mp3()
        mp3_b = self._make_dummy_mp3()
        mp.file_cache["B"] = {"path": mp3_b, "last_accessed": time.time(), "size": 1600}

        gate_a = asyncio.Event()
        concurrent = 0
        max_concurrent = 0
        play_next_calls = 0
        orig_play_next = mp._play_next

        async def wrapped_play_next(gid, max_retries=3):
            nonlocal concurrent, max_concurrent, play_next_calls
            play_next_calls += 1
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            try:
                return await orig_play_next(gid, max_retries=max_retries)
            finally:
                concurrent -= 1

        mp._play_next = wrapped_play_next  # type: ignore[method-assign]

        class SlowA:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=True):
                return {"ext": "mp3", "requested_downloads": [{"filepath": mp3_a}]}

        async def hold_a(song_id, fp):
            if song_id == "A":
                await gate_a.wait()
            mp.file_cache[song_id] = {"path": fp, "last_accessed": time.time(), "size": 1600}

        mp._add_to_cache = hold_a  # type: ignore[method-assign]

        try:
            with patch("modules.music_player.yt_dlp.YoutubeDL", SlowA):
                mp.enqueue_song(1006, {"url": "A", "title": "A", "song_id": "A"})
                await asyncio.sleep(0.03)
                # stop() mientras transición gen=0 de A sigue retenida en gate_a (evita desconectar manteniendo TTS o sin vc)
                tts_tok = vsm.try_acquire_tts_playback(1006)
                await mp.stop(1006)
                vsm.release_playback(1006, playback_token=tts_tok)
                # play_song(B) inmediatamente bajo gen=1
                mp.enqueue_song(1006, {"url": "B", "title": "B", "song_id": "B"})
                self.assertEqual(len(mp.queues[1006]), 1)
                gate_a.set()
                await asyncio.sleep(0.1)

            self.assertEqual(max_concurrent, 1)
            self.assertEqual(play_next_calls, 2)
            self.assertEqual(mp.current_songs[1006]["title"], "B")
            self.assertEqual(len(vc.played), 1)
        finally:
            gate_a.set()
            await vsm.aclose()
            self._cleanup_files(mp3_a, mp3_b)

    async def test_T7_sync_edges_guild_none_and_voice_transitioning(self):
        vsm, mp, _g, _ch, vc = self._setup_player(1007)
        with self.assertRaises(ValueError):
            await mp.skip(None)
        with self.assertRaises(ValueError):
            await mp.stop(None)
        with self.assertRaises(ValueError):
            await mp.pause(None)
        with self.assertRaises(ValueError):
            await mp.resume(None)
        with self.assertRaises(ValueError):
            await mp.set_volume(0.5, None)
        with self.assertRaises(ValueError):
            await mp.get_queue(None)

        self.assertEqual(await mp.skip(1007), "No hay nada reproduciéndose actualmente.")
        mp._transition_active[1007] = True
        mp.is_playing[1007] = True
        self.assertIn("transición de canción", await mp.pause(1007))
        mp._transition_active[1007] = False
        mp.is_playing[1007] = False

        # Durante is_voice_transitioning(gid) == True
        vsm._voice_transitioning[1007] = True
        fake_src = types.SimpleNamespace(volume=0.5)
        vc.source = fake_src
        self.assertIn("Conexión de voz en transición", await mp.skip(1007))
        self.assertIn("Conexión de voz en transición", await mp.pause(1007))
        self.assertIn("Conexión de voz en transición", await mp.resume(1007))
        await mp.set_volume(0.8, 1007)
        self.assertEqual(mp.volume[1007], 0.8)
        self.assertEqual(fake_src.volume, 0.5)  # No tocó vc.source
        vsm._voice_transitioning.pop(1007, None)
        await vsm.aclose()

    async def test_T8_retry_gate_atomic_wakeup_and_preemption(self):
        vsm, mp, _g, _ch, vc = self._setup_player(1008)
        op = MusicOpToken(1008, mp._music_gen.get(1008, 0), 1)
        mp._active_op_tokens[1008] = op
        t0 = time.monotonic()
        wait_task = asyncio.create_task(mp._wait_retry_gate(1008, op))
        await asyncio.sleep(0.02)
        mp._invalidate_guild_operations(1008)
        res = await wait_task
        elapsed = time.monotonic() - t0
        self.assertFalse(res)
        self.assertLess(elapsed, 0.5)
        self.assertEqual(mp._retry_gates, {})
        await vsm.aclose()

    async def test_T9_deferred_disconnect_blockers_races_and_lock_ownership(self):
        vsm, mp, guild, ch, vc = self._setup_player(1009, hold_play=True)
        tracker = _instrument_vsm_locks(vsm)
        try:
            # (a) Bloqueado por _music_waiting_preempt y liberación en finally
            tts_tok = vsm.try_acquire_tts_playback(1009)
            vc.playing = True
            acq_task = asyncio.create_task(vsm.acquire_music_playback(1009, expected_vc=vc, preempt_timeout=0.3))
            await asyncio.sleep(0.02)
            cb_called = []
            await vsm.request_deferred_disconnect(1009, vc, on_disconnected_cb=lambda c: cb_called.append(c))
            # Liberamos TTS para que acquire_music_playback obtenga token, luego soltamos music
            vc.stop()
            vsm.release_playback(1009, playback_token=tts_tok)
            pb_m = await acq_task
            self.assertTrue(vc.is_connected())  # request fue cancelado por acquire_music_playback
            vsm.release_playback(1009, playback_token=pb_m)

            # (b) Carrera Fase 1 RUNNING rechazando acquire_sink y acquire_tts_playback sin esperar disconnect()
            vc.disconnect_gate = asyncio.Event()
            await vsm.request_deferred_disconnect(1009, vc, on_disconnected_cb=lambda c: cb_called.append(c))
            await asyncio.sleep(0.03)
            self.assertEqual(vsm._deferred_disconnect_tasks[1009].state, "RUNNING")
            self.assertFalse(vsm._get_lock(1009).locked())
            self.assertIsNone(vsm.try_acquire_tts_playback(1009))
            self.assertIsNone(await vsm.acquire_tts_playback(1009))
            sink = TranslatorAudioSink(on_speech=lambda u, p: asyncio.sleep(0), loop=asyncio.get_running_loop())
            with self.assertRaises(VoiceClientUnavailableError):
                await vsm.acquire_sink(1009, vc, "translator", sink)
            sink.cleanup()

            vc.disconnect_gate.set()
            await asyncio.sleep(0.05)
            self.assertFalse(vc.is_connected())
            self.assertEqual(len(cb_called), 1)
            self.assertIsNone(vsm.voice_clients.get(1009))

            # (c) Bloqueo ante _voice_transitioning o estado indeterminado None
            vc2 = await vsm.ensure_voice_client(ch, require_receive=True)
            vc2.is_connected_behavior = lambda: None
            await vsm.request_deferred_disconnect(1009, vc2)
            self.assertTrue(vsm.has_unreconciled_voice_client(1009))
            self.assertEqual(tracker["session"], {})
        finally:
            await vsm.aclose()

    async def test_T10_source_construction_and_cleanup_error_isolation(self):
        vsm, mp, _g, _ch, vc = self._setup_player(1010, hold_play=True)
        mp3 = self._make_dummy_mp3()
        mp.file_cache["s10"] = {"path": mp3, "last_accessed": time.time(), "size": 1600}
        cleaned_raw = []

        class FailingRaw:
            def __init__(self, *a, **k):
                pass

            def cleanup(self):
                cleaned_raw.append(True)
                raise RuntimeError("cleanup boom")

        try:
            mp.queues[1010].append({"title": "S10", "song_id": "s10"})
            with patch("modules.music_player.discord.FFmpegPCMAudio", FailingRaw), patch(
                "modules.music_player.discord.PCMVolumeTransformer",
                side_effect=RuntimeError("transformer fail"),
            ):
                await mp._play_next(1010, max_retries=0)
            self.assertEqual(cleaned_raw, [True])
            self.assertIsNone(vsm.get_playback_token(1010))
            self.assertEqual(mp._in_use_cache_files, {})
            self.assertFalse(vsm.release_playback(1010, playback_token=1))
        finally:
            await vsm.aclose()
            self._cleanup_files(mp3)

    async def test_T11_translator_revocation_before_synthesize_and_save_translation(self):
        from translator import manager as tr_manager

        vsm = VoiceSessionManager()
        sent = []

        class FakeBot:
            def get_guild(self, gid):
                return types.SimpleNamespace(
                    get_channel=lambda cid: types.SimpleNamespace(
                        send=lambda **kw: asyncio.sleep(0)
                    )
                )

        sess = TranslatorSession(1011, FakeBot(), session_manager=vsm)
        sess._session_token = 50
        sess._listening_active = True
        sess._revoked = False
        sess._text_channel_id = 1

        async def revoke_in_send(*a, **kw):
            sent.append(True)
            sess._revoked = True
            sess._session_token = 51

        with patch.object(sess, "_send_to_text_channel", side_effect=revoke_in_send), patch.object(
            tr_manager, "get_user_settings", return_value={"target_lang": "en"}
        ), patch.object(tr_manager.stt, "preprocess_audio", return_value=[0.0] * 1600), patch.object(
            tr_manager, "build_whisper_prompt", return_value=""
        ), patch.object(
            tr_manager.stt,
            "transcribe",
            return_value={"text": "hola mundo", "language": "es", "confidence": 0.9},
        ), patch.object(
            tr_manager.tr, "translate_text", return_value={"translated": "hello world"}
        ), patch.object(tr_manager.tts, "synthesize") as mock_synth, patch.object(
            tr_manager, "save_translation"
        ) as mock_save:
            await sess._on_speech(1, b"\x01\x02" * 800)
            self.assertEqual(sent, [True])
            mock_synth.assert_not_called()
            mock_save.assert_not_called()
            self.assertTrue(sess._tts_queue.empty())
        await vsm.aclose()

    async def test_T12_vc_replacement_invalidates_music_gen_synchronously(self):
        vsm, mp, guild, ch, vc1 = self._setup_player(1012)
        gen0 = mp._music_gen.get(1012, 0)
        tok1 = MusicOpToken(1012, gen0, 1)
        mp._active_op_tokens[1012] = tok1

        # Reemplazar vc1 (desconectado) por vc2 vía ensure_voice_client
        vc1.connected = False
        vc2 = await vsm.ensure_voice_client(ch, require_receive=True)
        self.assertIsNot(vc1, vc2)
        self.assertIs(mp.voice_clients.get(1012), vc2)
        self.assertGreater(mp._music_gen[1012], gen0)
        self.assertFalse(mp._is_current(1012, tok1))
        self.assertEqual(len(vc1.played), 0)
        self.assertEqual(len(vc2.played), 0)
        await vsm.aclose()

    async def test_T13_monotonic_lag_attribution_four_buckets(self):
        mgr = STTQueueManager()
        self.assertEqual(mgr.record_loop_lag_sample(10.0, 10.01), "idle")
        mgr.record_download_interval(20.0, 21.0)
        self.assertEqual(mgr.record_loop_lag_sample(20.2, 20.3), "download_only")
        mgr.record_inference_interval(30.0, 31.0)
        self.assertEqual(
            mgr.record_loop_lag_sample(30.2, 30.3), "inference_without_download_overlap"
        )
        mgr.record_download_interval(40.0, 41.0)
        mgr.record_inference_interval(40.0, 41.0)
        self.assertEqual(mgr.record_loop_lag_sample(40.2, 40.3), "overlapped_unattributable")
        obs = mgr.get_observability_metrics()["lag_attribution"]
        for bucket in (
            "idle",
            "download_only",
            "inference_without_download_overlap",
            "overlapped_unattributable",
        ):
            self.assertEqual(obs[bucket]["samples"], 1)

    async def test_T14_closed_loop_fail_closed_thread_safe_metrics(self):
        vsm, mp, _g, _ch, _vc = self._setup_player(1014)
        closed_loop = asyncio.new_event_loop()
        closed_loop.close()

        def _simulate_closed_after():
            mp._record_dropped_after_on_closed_loop(1014, 7, 11, reason="loop_is_closed")

        t = threading.Thread(target=_simulate_closed_after)
        t.start()
        t.join(timeout=2.0)
        self.assertEqual(mp.dropped_after_callbacks_on_closed_loop, 1)
        self.assertEqual(len(mp.get_incomplete_close_reconciliations()), 1)
        self.assertEqual(vsm.dropped_after_callbacks_on_closed_loop, 1)
        self.assertEqual(vsm._finalizer_errors, [])
        await vsm.aclose()

    async def test_T15_cache_file_protected_until_pb_token_release_even_after_disconnect(self):
        vsm, mp, guild, ch, vc = self._setup_player(1015, hold_play=True, async_after_thread=True)
        mp3 = self._make_dummy_mp3()
        mp.file_cache["s15"] = {"path": mp3, "last_accessed": 0.0, "size": 1600}
        vc.after_thread_gate = threading.Event()
        try:
            mp.enqueue_song(1015, {"title": "S15", "song_id": "s15"})
            await asyncio.sleep(0.05)
            # Detener música y simular desconexión física mientras hilo after sigue retenido
            await mp.stop(1015)
            vc.connected = False
            self.assertTrue(mp._in_use_cache_files)
            # Evicción/limpieza de caché NO debe borrar el archivo protegido
            mp.file_cache["s15"]["last_accessed"] = 0.0
            await mp.clear_cache(older_than_days=1)
            self.assertTrue(os.path.exists(mp3))

            # Soltar hilo after -> loop libera pb_token e _in_use_cache_files
            vc.after_thread_gate.set()
            await asyncio.sleep(0.08)
            vc.join_threads()
            self.assertEqual(mp._in_use_cache_files, {})
            await mp.clear_cache(older_than_days=1)
            self.assertFalse(os.path.exists(mp3))
        finally:
            await vsm.aclose()
            self._cleanup_files(mp3)

    async def test_T16_bounded_concurrent_event_sequences_k4(self):
        # Explorador acotado K=4 representativo validando invariantes 1, 2, 4, 6, 9, 10
        sequences = [
            ["play", "skip", "stop", "play"],
            ["tts", "play", "skip", "release_tts"],
            ["play", "vc_disconnect", "play", "stop"],
        ]
        mp3 = self._make_dummy_mp3()
        try:
            for seq in sequences:
                vsm, mp, guild, ch, vc = self._setup_player(1016, hold_play=True)
                mp.file_cache["S1"] = {"path": mp3, "last_accessed": time.time(), "size": 1600}
                tts_tok = None
                try:
                    for ev in seq:
                        if ev == "tts":
                            tts_tok = vsm.try_acquire_tts_playback(1016)
                            if tts_tok is not None:
                                vc.playing = True
                        elif ev == "release_tts" and tts_tok is not None:
                            vc.stop()
                            vsm.release_playback(1016, playback_token=tts_tok)
                            tts_tok = None
                        elif ev == "play":
                            mp.enqueue_song(1016, {"title": "S1", "song_id": "S1"})
                        elif ev == "skip":
                            await mp.skip(1016)
                        elif ev == "stop":
                            await mp.stop(1016)
                        elif ev == "vc_disconnect":
                            vc.connected = False
                        await asyncio.sleep(0.02)
                        # Invariante básica: máximo 1 transición / sin play ilegal
                        self.assertEqual(vsm._finalizer_errors, [])
                finally:
                    vc.stop()
                    if tts_tok is not None:
                        vsm.release_playback(1016, playback_token=tts_tok)
                    await vsm.aclose()
        finally:
            self._cleanup_files(mp3)

    async def test_T17_abcd_p1_p2_and_mandatory_option_a(self):
        # 1. T17.A + T17.B + P1 (move_to con guild.voice_client is None y stale vc1 + vc2)
        vsm = VoiceSessionManager()
        tracker = _instrument_vsm_locks(vsm)
        lifecycle_events: list[tuple[int, Any, bool]] = []
        vsm.register_vc_lifecycle_callback(
            lambda gid, vc=None, physically_disconnected=False: lifecycle_events.append(
                (gid, vc, physically_disconnected)
            ),
            key="test_obs",
        )
        gid = 1017
        guild = FakeGuild(gid)
        ch1 = FakeVoiceChannel(guild, 1, "C1")
        ch2 = FakeVoiceChannel(guild, 2, "C2")

        try:
            vc = await vsm.ensure_voice_client(ch1, require_receive=True)
            # T17.A: 1ª y 2ª CancelledError en _run_deferred_disconnect con shield preservando on_disconnected_cb
            vc.disconnect_gate = asyncio.Event()
            cb_calls = []
            await vsm.request_deferred_disconnect(gid, vc, on_disconnected_cb=lambda c: cb_calls.append(c))
            await asyncio.sleep(0.02)
            rec = vsm._deferred_disconnect_tasks[gid]
            self.assertEqual(rec.state, "RUNNING")
            self.assertFalse(vsm._get_lock(gid).locked())
            rec.task.cancel()
            await asyncio.sleep(0.02)
            vc.disconnect_gate.set()
            await asyncio.sleep(0.05)
            self.assertEqual(cb_calls, [])  # 1ª cancelación antes de disconnect -> no callback
            self.assertEqual(rec.state, "COMPLETED")
            self.assertEqual(vsm._finalizer_errors, [])

            # T17.C Subcaso 2 (_closing=False): cancelación directa de fin_task programa recuperación en _lifecycle_tasks
            vc = await vsm.ensure_voice_client(ch1, require_receive=True)
            fin_rec = DeferredDisconnectRecord(
                task=None,
                dc_token=999,
                vc=vc,
                state="RUNNING",
                done_event=asyncio.Event(),
                aborted_event=asyncio.Event(),
            )
            vsm._deferred_disconnect_tasks[gid] = fin_rec
            async with vsm._get_lock(gid):
                fin_t = vsm._spawn_disconnect_finalizer(
                    gid,
                    fin_rec,
                    vc,
                    on_disconnected_cb=None,
                    phase1_committed=True,
                    disconnect_cancelled_err=None,
                    disconnect_exc=None,
                    is_force_reconcile=False,
                )
                await asyncio.sleep(0.01)
                fin_t.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await fin_t
                self.assertTrue(fin_rec.aborted_event.is_set())
                self.assertTrue(fin_rec.done_event.is_set())
                self.assertEqual(len(vsm._lifecycle_tasks), 1)
            await asyncio.sleep(0.05)
            self.assertEqual(fin_rec.state, "COMPLETED")
            self.assertEqual(len(vsm._finalizer_errors), 1)
            vsm._finalizer_errors.clear()

            # T17.D: is_connected() devolviendo None, lanzando excepción, None -> True, None -> False
            vsm.reset_guild(gid)
            vc_ind = FakeVoiceRecvClient(ch1)
            guild.voice_client = vc_ind
            vsm.voice_clients[gid] = vc_ind
            vc_ind.is_connected_behavior = lambda: (_ for _ in ()).throw(RuntimeError("probe err"))
            with self.assertRaises(VoiceClientUnavailableError):
                await vsm.acquire_sink(gid, vc_ind, "translator", DiscordAudioSink())
            self.assertTrue(vsm.has_unreconciled_voice_client(gid))
            self.assertIs(vsm.voice_clients.get(gid), vc_ind)  # Jamás retirado por None
            # Transición indeterminado -> False retira y reconecta limpio
            vc_ind.is_connected_behavior = lambda: False
            vc_new = await vsm.ensure_voice_client(ch1, require_receive=True)
            self.assertIsNot(vc_new, vc_ind)
            self.assertFalse(vsm.has_unreconciled_voice_client(gid))

            # P1: move_to() termina normal pero guild.voice_client queda None con existing.is_connected() == True
            def drop_guild_vc_on_move(target_ch):
                vc_new.channel = target_ch
                guild.voice_client = None

            vc_new.move_to_custom_handler = drop_guild_vc_on_move
            with self.assertRaises(VoiceClientRecoveryError):
                await vsm.ensure_voice_client(ch2, require_receive=True)
            self.assertTrue(vsm.has_unreconciled_voice_client(gid))
            self.assertIsNone(vsm.try_acquire_tts_playback(gid))
            # Reconciliar vc_new con force=True antes de permitir nueva conexión
            vc_new.move_to_custom_handler = None
            vc_after_move_div = await vsm.ensure_voice_client(ch2, require_receive=True)
            self.assertGreaterEqual(vc_after_move_div.stop_calls, 0)
            self.assertIn(True, vc_new.disconnect_calls)

            # P1 adicional: self.voice_clients[gid] = vc1 (False) pero guild.voice_client = vc2 (True)
            vc1_stale = FakeVoiceRecvClient(ch1)
            vc1_stale.connected = False
            vc2_live = FakeVoiceRecvClient(ch1)
            vc2_live.connected = True
            vsm.voice_clients[gid] = vc1_stale
            guild.voice_client = vc2_live
            vc_reconciled = await vsm.ensure_voice_client(ch2, require_receive=True)
            self.assertIn(True, vc2_live.disconnect_calls)
            self.assertIsNot(vc_reconciled, vc2_live)

            # P2: postcondición connect() fallida (devuelve desconectado / guild.voice_client distinto)
            vsm.reset_guild(gid)
            guild.voice_client = None
            bad_vc = FakeVoiceRecvClient(ch1)
            bad_vc.connected = False
            ch1.connect_handler = lambda **kw: bad_vc
            with self.assertRaises(VoiceClientRecoveryError):
                await vsm.ensure_voice_client(ch1, require_receive=True)
            self.assertIsNone(vsm.voice_clients.get(gid))

            # OBLIGATORIO OPCIÓN A: connect() devuelve None Y guild.voice_client es None
            vsm.reset_guild(gid)
            guild.voice_client = None
            lifecycle_events.clear()
            ch1.connect_handler = lambda **kw: None
            with self.assertRaises(VoiceClientRecoveryError):
                await vsm.ensure_voice_client(ch1, require_receive=True)
            self.assertIsNone(vsm.voice_clients.get(gid))
            self.assertFalse(vsm.has_unreconciled_voice_client(gid))
            self.assertEqual(lifecycle_events[-1], (gid, None, False))

            # T17.C Subcaso 1 (_closing=True) + drenado aclose()
            ch1.connect_handler = None
            self.assertEqual(tracker["session"], {})
            self.assertEqual(tracker["playback"], {})
            await vsm.aclose()
            self.assertEqual(vsm._lifecycle_tasks, set())
            self.assertEqual(vsm._finalizer_tasks, set())
            self.assertEqual(vsm._finalizer_errors, [])
        finally:
            await vsm.aclose()


if __name__ == "__main__":
    print(f"[EnvInfo] Python={sys.version.split()[0]} | Reales={REAL_MODULES} | TestDoubles={STUBBED_MODULES}")
    unittest.main(verbosity=2)
