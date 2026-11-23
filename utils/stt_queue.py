# utils/stt_queue.py
#
# Administrador centralizado de cola e instancia única de Speech-to-Text (STT).
#
# Objetivos (Bloque 0A v3.1):
#   1. Único punto de entrada para comandos de voz (musicbot) y traductor.
#   2. Única instancia lazy de Whisper en memoria (evita cargar ~348+ MB duplicados).
#   3. Ejecución estrictamente serializada (asyncio.Queue + 1 worker en ThreadPoolExecutor(max_workers=1)).
#   4. Ciclo de vida explícito (STOPPED -> RUNNING -> STOPPING / SHUTDOWN_PENDING).
#   5. Prioridad musical y control de admisión con ventana de observación e histéresis.
#   6. Verificación de antigüedad/cancelación pre y post inferencia con estados explícitos.

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

import config

logger = logging.getLogger("musicbot.stt_queue")


class STTStatus(str, Enum):
    OK = "ok"
    EMPTY_OR_HALLUCINATION = "empty_or_hallucination"
    DROPPED_QUEUE_FULL = "dropped_queue_full"
    DROPPED_STALE = "dropped_stale"
    DROPPED_CANCELLED = "dropped_cancelled"
    DROPPED_OVERLOAD = "dropped_overload"
    ERROR = "error"


@dataclass
class STTResult:
    status: STTStatus
    text: str = ""
    language: str = "unknown"
    confidence: float = 0.0
    latency_s: float = 0.0
    queue_wait_s: float = 0.0
    error: str | None = None


@dataclass
class ShutdownResult:
    completed: bool
    pending_inference: bool
    state: str


@dataclass
class _STTJob:
    audio_input: Any
    language: str | None
    task: str
    initial_prompt: str | None
    hallucination_set: set[str]
    min_chars: int
    audio_duration_s: float | None
    enqueued_at: float
    future: asyncio.Future
    is_cancelled_cb: Callable[[], bool] | None = None
    caller_cancelled: bool = False


class STTQueueManager:
    """
    Cola centralizada de STT con worker único y ciclo de vida explícito.
    """

    def __init__(
        self,
        max_queue_size: int = 3,
        max_queue_wait_s: float = 4.0,
        rtf_overload_threshold: float = 1.35,
        loop_lag_overload_ms: float = 25.0,
        loop_lag_recover_ms: float = 12.0,
        overload_cooldown_s: float = 3.0,
        torch_num_threads: int = 1,
        model_loader: Callable[[], Any] | None = None,
    ):
        self.max_queue_size = max_queue_size
        self.max_queue_wait_s = max_queue_wait_s
        self.rtf_overload_threshold = rtf_overload_threshold
        self.loop_lag_overload_ms = loop_lag_overload_ms
        self.loop_lag_recover_ms = loop_lag_recover_ms
        self.overload_cooldown_s = overload_cooldown_s
        self.torch_num_threads = torch_num_threads
        self._custom_model_loader = model_loader

        self._state: str = "STOPPED"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[_STTJob | None] | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._worker_task: asyncio.Task | None = None
        self._lag_task: asyncio.Task | None = None
        self._active_exec_future: asyncio.Future | None = None

        self._whisper_model: Any = None
        self._threads_configured: bool = False
        self._completed_count: int = 0

        # Ventana de observación e histéresis de sobrecarga
        self._rtf_window: deque[float] = deque(maxlen=3)
        self._lag_window_ms: deque[float] = deque(maxlen=5)
        self._overloaded: bool = False
        self._overloaded_at: float = 0.0

        # Colecciones puramente observacionales (no modifican umbrales ni decisiones)
        self._rtf_history: deque[float] = deque(maxlen=200)
        self._lag_history_ms: deque[float] = deque(maxlen=500)
        self._recovery_durations_s: deque[float] = deque(maxlen=50)
        self._last_recovery_duration_s: float = 0.0

        # Telemetría unificada de lag por intervalos monotónicos (time.monotonic)
        self._download_intervals: deque[tuple[float, float]] = deque(maxlen=500)
        self._inference_intervals: deque[tuple[float, float]] = deque(maxlen=500)
        self._active_downloads: dict[int, float] = {}
        self._download_counter: int = 0
        self._active_inference_start: float | None = None
        self._lag_buckets: dict[str, deque[float]] = {
            "idle": deque(maxlen=500),
            "download_only": deque(maxlen=500),
            "inference_without_download_overlap": deque(maxlen=500),
            "overlapped_unattributable": deque(maxlen=500),
        }

        self._is_music_active_cb: Callable[[], bool] | None = None

        # Métricas básicas observables
        self.metrics: dict[str, int] = {
            "submitted": 0,
            "completed_ok": 0,
            "dropped_full": 0,
            "dropped_stale": 0,
            "dropped_cancelled": 0,
            "dropped_overload": 0,
            "errors": 0,
        }

    # ── Propiedades y configuración ────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_overloaded(self) -> bool:
        return self._overloaded

    @property
    def model_loaded(self) -> bool:
        return self._whisper_model is not None

    def set_music_active_callback(self, cb: Callable[[], bool] | None) -> None:
        self._is_music_active_cb = cb

    def _music_is_active(self) -> bool:
        if self._is_music_active_cb is None:
            return False
        try:
            return bool(self._is_music_active_cb())
        except Exception:
            return False

    def record_loop_lag_ms(self, lag_ms: float) -> None:
        val = max(0.0, float(lag_ms))
        self._lag_window_ms.append(val)
        self._lag_history_ms.append(val)

    def begin_download(self) -> int:
        self._download_counter += 1
        token = self._download_counter
        self._active_downloads[token] = time.monotonic()
        return token

    def end_download(self, token: int) -> None:
        t_start = self._active_downloads.pop(token, None)
        if t_start is not None:
            self.record_download_interval(t_start, time.monotonic())

    def record_download_interval(self, t_start: float, t_end: float) -> None:
        if t_end >= t_start:
            self._download_intervals.append((float(t_start), float(t_end)))

    def record_inference_interval(self, t_start: float, t_end: float) -> None:
        if t_end >= t_start:
            self._inference_intervals.append((float(t_start), float(t_end)))

    @staticmethod
    def _intervals_overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
        lo = max(a_start, b_start)
        hi = min(a_end, b_end)
        return lo < hi or (a_start == a_end and b_start <= a_start <= b_end)

    def _classify_lag_interval(self, t_expected: float, t_actual: float) -> str:
        dl_overlap = any(
            self._intervals_overlap(t_expected, t_actual, s, e) for s, e in self._download_intervals
        ) or any(
            s <= t_actual for s in self._active_downloads.values()
        )
        inf_overlap = any(
            self._intervals_overlap(t_expected, t_actual, s, e) for s, e in self._inference_intervals
        ) or (self._active_inference_start is not None and self._active_inference_start <= t_actual)

        if dl_overlap and inf_overlap:
            return "overlapped_unattributable"
        if dl_overlap:
            return "download_only"
        if inf_overlap:
            return "inference_without_download_overlap"
        return "idle"

    def record_loop_lag_sample(self, t_expected: float, t_actual: float) -> str:
        lag_ms = max(0.0, (t_actual - t_expected) * 1000.0)
        self.record_loop_lag_ms(lag_ms)
        bucket = self._classify_lag_interval(t_expected, t_actual)
        self._lag_buckets[bucket].append(lag_ms)
        return bucket

    # ── Ciclo de vida explícito ────────────────────────────────

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Inicializa la cola, el executor único y el worker en el event loop activo."""
        if self._state in ("STOPPING", "SHUTDOWN_PENDING"):
            raise RuntimeError(
                f"No se puede iniciar STTQueueManager mientras el estado es '{self._state}'."
            )

        running_loop = loop or asyncio.get_running_loop()

        if self._state == "RUNNING":
            # Mismo loop y worker vivo -> idempotente (no duplicar workers)
            if (
                self._loop is running_loop
                and not self._loop.is_closed()
                and self._worker_task is not None
                and not self._worker_task.done()
            ):
                return
            raise RuntimeError(
                "STTQueueManager ya tiene un worker activo; ejecuta shutdown() antes de reiniciar."
            )

        self._loop = running_loop
        self._queue = asyncio.Queue(maxsize=self.max_queue_size)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt-worker")
        self._active_exec_future = None
        self._state = "RUNNING"
        self._worker_task = running_loop.create_task(self._worker_loop(), name="stt-queue-worker")
        self._lag_task = running_loop.create_task(self._loop_lag_sampler(), name="stt-lag-sampler")
        logger.info("STTQueueManager iniciado (worker único, max_queue=%d).", self.max_queue_size)

    async def _loop_lag_sampler(self) -> None:
        """Mide atraso real del event loop sobre time.monotonic() y lo clasifica por solapamiento."""
        interval = 0.05
        try:
            while self._state == "RUNNING":
                await asyncio.sleep(0.20)
                if self._state != "RUNNING":
                    break
                t_expected = time.monotonic() + interval
                await asyncio.sleep(interval)
                t_actual = time.monotonic()
                self.record_loop_lag_sample(t_expected, t_actual)
        except asyncio.CancelledError:
            return

    def _drain_pending_queue(self) -> None:
        if self._queue is None:
            return
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is not None:
                if not item.future.done():
                    item.future.set_result(
                        STTResult(
                            status=STTStatus.DROPPED_CANCELLED,
                            error="STTQueueManager en cierre",
                        )
                    )
                self.metrics["dropped_cancelled"] += 1
            self._queue.task_done()

    async def shutdown(self, timeout: float = 10.0) -> ShutdownResult:
        """
        Cierra ordenadamente la cola y el executor.

        Si una inferencia nativa ya está corriendo en el hilo y supera `timeout`,
        pasa a estado SHUTDOWN_PENDING y NO libera la referencia del modelo ni
        declara STOPPED hasta que el hilo finalice realmente.
        """
        if self._state == "STOPPED":
            return ShutdownResult(completed=True, pending_inference=False, state="STOPPED")

        if self._state == "SHUTDOWN_PENDING":
            return ShutdownResult(completed=False, pending_inference=True, state="SHUTDOWN_PENDING")

        self._state = "STOPPING"
        self._drain_pending_queue()

        if self._lag_task is not None and not self._lag_task.done():
            self._lag_task.cancel()
            try:
                await self._lag_task
            except asyncio.CancelledError:
                pass
        self._lag_task = None

        # Desbloquear _worker_loop si está esperando un item
        if self._queue is not None:
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                self._drain_pending_queue()
                self._queue.put_nowait(None)

        if self._worker_task is not None and not self._worker_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._worker_task), timeout=timeout)
            except asyncio.TimeoutError:
                # Hay una inferencia nativa bloqueante en el hilo único
                self._state = "SHUTDOWN_PENDING"
                logger.warning(
                    "Cierre de STTQueueManager pendiente: inferencia activa en hilo nativo "
                    "tras esperar %.1fs.",
                    timeout,
                )
                self._attach_deferred_cleanup()
                return ShutdownResult(
                    completed=False,
                    pending_inference=True,
                    state=self._state,
                )

        self._finalize_shutdown()
        return ShutdownResult(completed=True, pending_inference=False, state=self._state)

    def _attach_deferred_cleanup(self) -> None:
        """Programa la limpieza real cuando la inferencia pendiente termine."""
        if self._worker_task is not None and not self._worker_task.done():
            self._worker_task.add_done_callback(lambda _t: self._finalize_shutdown())
        elif self._active_exec_future is not None and not self._active_exec_future.done():
            self._active_exec_future.add_done_callback(lambda _f: self._finalize_shutdown())
        else:
            self._finalize_shutdown()

    def _finalize_shutdown(self) -> None:
        if self._active_exec_future is not None and not self._active_exec_future.done():
            return
        if self._executor is not None:
            try:
                self._executor.shutdown(wait=False)
            except Exception as exc:
                logger.warning("Error cerrando executor STT: %s", exc)
            self._executor = None
        self._whisper_model = None
        self._worker_task = None
        self._queue = None
        self._loop = None
        self._state = "STOPPED"
        logger.info("STTQueueManager detenido completamente y memoria liberada.")

    # ── Carga lazy de Whisper y ejecución síncrona en worker ───

    def _load_model_sync(self) -> Any:
        if self._whisper_model is not None:
            return self._whisper_model

        if not self._threads_configured:
            try:
                import torch
                torch.set_num_threads(self.torch_num_threads)
                logger.info("PyTorch num_threads fijado a %d para STT.", self.torch_num_threads)
            except Exception as exc:
                logger.debug("No se pudo configurar torch.set_num_threads: %s", exc)
            self._threads_configured = True

        if self._custom_model_loader is not None:
            self._whisper_model = self._custom_model_loader()
            return self._whisper_model

        import whisper
        model_size = getattr(config, "WHISPER_MODEL_SIZE", "base")
        logger.info("Cargando instancia única de Whisper '%s' (lazy load)...", model_size)
        self._whisper_model = whisper.load_model(model_size)
        logger.info("Instancia única de Whisper '%s' lista.", model_size)
        return self._whisper_model

    def _run_whisper_sync(self, job: _STTJob) -> dict[str, Any]:
        model = self._load_model_sync()
        kwargs: dict[str, Any] = {
            "task": job.task,
            "fp16": False,
            "verbose": False,
        }
        if job.language is not None:
            kwargs["language"] = job.language
        if job.initial_prompt:
            kwargs["initial_prompt"] = job.initial_prompt

        result = model.transcribe(job.audio_input, **kwargs)
        return result or {}

    # ── Histéresis de sobrecarga ───────────────────────────────

    def _update_overload_state(self, now: float) -> None:
        music_active = self._music_is_active()

        # Excluir el primer run (warm-up) del cálculo de sobrecarga por RTF
        rtf_over = (
            self._completed_count >= 1
            and len(self._rtf_window) >= 2
            and statistics.mean(self._rtf_window) > self.rtf_overload_threshold
        )
        lag_over = (
            len(self._lag_window_ms) >= 3
            and statistics.median(self._lag_window_ms) > self.loop_lag_overload_ms
        )

        if not self._overloaded:
            if music_active and (rtf_over or lag_over):
                self._overloaded = True
                self._overloaded_at = now
                logger.warning(
                    "STTQueueManager entró en modo OVERLOADED (music=%s, rtf_mean=%.3f, lag_med=%.2f ms).",
                    music_active,
                    statistics.mean(self._rtf_window) if self._rtf_window else 0.0,
                    statistics.median(self._lag_window_ms) if self._lag_window_ms else 0.0,
                )
        else:
            queue_empty = self._queue is None or self._queue.empty()
            no_active_inference = (
                self._active_exec_future is None or self._active_exec_future.done()
            )
            lag_ok = (
                not self._lag_window_ms
                or statistics.median(self._lag_window_ms) < self.loop_lag_recover_ms
            )
            cooldown_passed = (now - self._overloaded_at) >= self.overload_cooldown_s
            if (not music_active) or (
                queue_empty and no_active_inference and cooldown_passed and lag_ok
            ):
                self._overloaded = False
                self._rtf_window.clear()
                recovery_s = max(0.0, now - self._overloaded_at)
                self._last_recovery_duration_s = recovery_s
                self._recovery_durations_s.append(recovery_s)
                logger.info("STTQueueManager recuperado a modo NORMAL (%.2fs).", recovery_s)

    @staticmethod
    def _compute_percentile(values: deque[float], pct: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        if len(ordered) == 1:
            return float(ordered[0])
        k = (len(ordered) - 1) * (pct / 100.0)
        lo = int(k)
        hi = min(lo + 1, len(ordered) - 1)
        frac = k - lo
        return float(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)

    def get_observability_metrics(self) -> dict[str, Any]:
        """
        Devuelve métricas puramente observacionales de RTF, lag del event loop,
        atribución por intervalos monotónicos, tiempos de recuperación y contadores.
        No altera umbrales ni admisión.
        """
        lag_attribution = {
            bucket: {
                "samples": len(vals),
                "p50": self._compute_percentile(vals, 50.0),
                "p95": self._compute_percentile(vals, 95.0),
                "max": max(vals) if vals else None,
            }
            for bucket, vals in self._lag_buckets.items()
        }
        return {
            "rtf": {
                "samples": len(self._rtf_history),
                "p50": self._compute_percentile(self._rtf_history, 50.0),
                "p95": self._compute_percentile(self._rtf_history, 95.0),
                "max": max(self._rtf_history) if self._rtf_history else None,
            },
            "lag_ms": {
                "samples": len(self._lag_history_ms),
                "p50": self._compute_percentile(self._lag_history_ms, 50.0),
                "p95": self._compute_percentile(self._lag_history_ms, 95.0),
                "max": max(self._lag_history_ms) if self._lag_history_ms else None,
            },
            "lag_attribution": lag_attribution,
            "overload": {
                "is_overloaded": self._overloaded,
                "last_recovery_s": self._last_recovery_duration_s,
                "recoveries_count": len(self._recovery_durations_s),
                "max_recovery_s": max(self._recovery_durations_s) if self._recovery_durations_s else 0.0,
            },
            "counts": dict(self.metrics),
        }

    # ── Loop único del worker ──────────────────────────────────

    async def _worker_loop(self) -> None:
        assert self._queue is not None
        assert self._loop is not None
        assert self._executor is not None

        while True:
            job = await self._queue.get()
            if job is None:
                self._queue.task_done()
                break

            try:
                now = time.monotonic()
                queue_wait_s = max(0.0, now - job.enqueued_at)

                if self._state in ("STOPPING", "SHUTDOWN_PENDING") or job.caller_cancelled or (
                    job.is_cancelled_cb is not None and job.is_cancelled_cb()
                ):
                    self.metrics["dropped_cancelled"] += 1
                    if not job.future.done():
                        job.future.set_result(
                            STTResult(status=STTStatus.DROPPED_CANCELLED, queue_wait_s=queue_wait_s)
                        )
                    continue

                if queue_wait_s > self.max_queue_wait_s:
                    self.metrics["dropped_stale"] += 1
                    if not job.future.done():
                        job.future.set_result(
                            STTResult(
                                status=STTStatus.DROPPED_STALE,
                                queue_wait_s=queue_wait_s,
                                error=f"Expirado en cola ({queue_wait_s:.2f}s > {self.max_queue_wait_s:.2f}s)",
                            )
                        )
                    continue

                # Ejecutar una única inferencia a la vez dentro del executor
                t0 = time.perf_counter()
                t_inf_start = time.monotonic()
                self._active_inference_start = t_inf_start
                exec_future = self._loop.run_in_executor(self._executor, self._run_whisper_sync, job)
                self._active_exec_future = exec_future
                try:
                    raw = await exec_future
                finally:
                    self._active_inference_start = None
                    self.record_inference_interval(t_inf_start, time.monotonic())
                    self._active_exec_future = None

                latency_s = time.perf_counter() - t0

                # Registrar RTF solo si conocemos duración de audio y tras warm-up
                dur = job.audio_duration_s
                if dur is None and hasattr(job.audio_input, "shape"):
                    try:
                        dur = float(len(job.audio_input)) / 16000.0
                    except Exception:
                        dur = None
                if self._completed_count >= 1 and dur and dur > 0:
                    rtf = latency_s / dur
                    self._rtf_window.append(rtf)
                    self._rtf_history.append(rtf)
                self._completed_count += 1
                self._update_overload_state(time.monotonic())

                # Comprobar cancelación posterior a la inferencia para jamás ejecutar comandos atrasados
                if self._state in ("STOPPING", "SHUTDOWN_PENDING") or job.caller_cancelled or (
                    job.is_cancelled_cb is not None and job.is_cancelled_cb()
                ):
                    self.metrics["dropped_cancelled"] += 1
                    if not job.future.done():
                        job.future.set_result(
                            STTResult(
                                status=STTStatus.DROPPED_CANCELLED,
                                latency_s=latency_s,
                                queue_wait_s=queue_wait_s,
                            )
                        )
                    continue

                text = str(raw.get("text", "")).strip()
                lang = str(raw.get("language", job.language or "unknown"))
                cleaned = text.lower().strip(" .,!?")
                if not text or len(cleaned) < job.min_chars or cleaned in job.hallucination_set:
                    if not job.future.done():
                        job.future.set_result(
                            STTResult(
                                status=STTStatus.EMPTY_OR_HALLUCINATION,
                                text="",
                                language=lang,
                                confidence=0.0,
                                latency_s=latency_s,
                                queue_wait_s=queue_wait_s,
                            )
                        )
                    continue

                segments = raw.get("segments") or []
                if segments:
                    avg_logprob = sum(s.get("avg_logprob", -1.0) for s in segments) / len(segments)
                    confidence = max(0.0, min(1.0, 1.0 + avg_logprob))
                else:
                    confidence = 0.7

                self.metrics["completed_ok"] += 1
                if not job.future.done():
                    job.future.set_result(
                        STTResult(
                            status=STTStatus.OK,
                            text=text,
                            language=lang,
                            confidence=confidence,
                            latency_s=latency_s,
                            queue_wait_s=queue_wait_s,
                        )
                    )

            except Exception as exc:
                self.metrics["errors"] += 1
                logger.error("Error en inferencia STT: %s", exc, exc_info=True)
                if not job.future.done():
                    job.future.set_result(
                        STTResult(status=STTStatus.ERROR, error=f"{type(exc).__name__}: {exc}")
                    )
            finally:
                self._queue.task_done()

    # ── Punto de entrada único público ─────────────────────────

    async def transcribe(
        self,
        audio_input: Any,
        *,
        language: str | None = "es",
        task: str = "transcribe",
        initial_prompt: str | None = None,
        hallucination_set: set[str] | None = None,
        min_chars: int = 2,
        audio_duration_s: float | None = None,
        is_cancelled_cb: Callable[[], bool] | None = None,
    ) -> STTResult:
        """Encola una petición de transcripción y espera su STTResult tipado."""
        if self._state in ("STOPPING", "SHUTDOWN_PENDING"):
            self.metrics["dropped_cancelled"] += 1
            return STTResult(
                status=STTStatus.DROPPED_CANCELLED,
                error=f"STTQueueManager no acepta trabajos en estado {self._state}",
            )

        running_loop = asyncio.get_running_loop()
        if self._state == "STOPPED":
            self.start(running_loop)

        assert self._queue is not None

        if is_cancelled_cb is not None and is_cancelled_cb():
            self.metrics["dropped_cancelled"] += 1
            return STTResult(status=STTStatus.DROPPED_CANCELLED)

        now = time.monotonic()
        self._update_overload_state(now)
        if self._overloaded:
            self.metrics["dropped_overload"] += 1
            return STTResult(
                status=STTStatus.DROPPED_OVERLOAD,
                error="Cola STT en protección por sobrecarga durante reproducción musical",
            )

        job_future = running_loop.create_future()
        job = _STTJob(
            audio_input=audio_input,
            language=language,
            task=task,
            initial_prompt=initial_prompt,
            hallucination_set=hallucination_set or set(),
            min_chars=min_chars,
            audio_duration_s=audio_duration_s,
            enqueued_at=now,
            future=job_future,
            is_cancelled_cb=is_cancelled_cb,
        )

        self.metrics["submitted"] += 1
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            self.metrics["dropped_full"] += 1
            return STTResult(
                status=STTStatus.DROPPED_QUEUE_FULL,
                error=f"Cola STT llena (máximo {self.max_queue_size})",
            )

        try:
            return await asyncio.shield(job_future)
        except asyncio.CancelledError:
            # La cancelación de la corrutina NO detiene el worker; marca el trabajo para descartar resultado
            job.caller_cancelled = True
            raise


stt_queue_manager = STTQueueManager()


def get_stt_queue_manager() -> STTQueueManager:
    return stt_queue_manager
