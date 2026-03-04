from __future__ import annotations

import asyncio
import contextlib
import io
import os
import secrets
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Header, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from PIL import Image, ImageDraw
import torch


JobStatus = Literal["queued", "running", "succeeded", "failed"]
TaskType = Literal["image_gen", "asr_whisper_small", "ui_parse_omniparser"]


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    negative_prompt: str | None = Field(default=None, max_length=2000)
    width: int = Field(default=512, ge=256)
    height: int = Field(default=512, ge=256)
    num_inference_steps: int = Field(default=20, ge=1)
    guidance_scale: float = Field(default=7.0, ge=0.0, le=20.0)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)


class GenerateAccepted(BaseModel):
    job_id: str
    status: JobStatus


class AsrSegment(BaseModel):
    start_sec: float
    end_sec: float
    text: str


class UiElement(BaseModel):
    element: str
    bbox: list[float] = Field(min_length=4, max_length=4)
    confidence: float = Field(ge=0.0, le=1.0)
    text: str | None = None


class AsrTranscribeResponse(BaseModel):
    job_id: str
    upload_id: str
    status: Literal["succeeded"]
    filename: str
    content_type: str | None
    size_bytes: int
    text: str
    segments: list[AsrSegment]
    language: str | None
    model_id: str
    elapsed_ms: int


class UiParseResponse(BaseModel):
    job_id: str
    parse_id: str
    status: Literal["succeeded"]
    filename: str
    content_type: str | None
    size_bytes: int
    model_id: str
    engine_mode: str
    elements: list[UiElement]
    elapsed_ms: int


class JobResponse(BaseModel):
    job_id: str
    task_type: TaskType
    status: JobStatus
    created_at: float
    started_at: float | None
    completed_at: float | None
    error: str | None
    width: int | None = None
    height: int | None = None
    num_inference_steps: int | None = None
    input_filename: str | None = None


@dataclass
class QueueItem:
    job_id: str
    request: GenerateRequest


@dataclass
class SavedAsrUpload:
    upload_id: str
    filename: str
    content_type: str | None
    size_bytes: int
    path: Path


@dataclass
class SavedUiUpload:
    parse_id: str
    filename: str
    content_type: str | None
    size_bytes: int
    path: Path


def _parse_csv_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or ["*"]


def _parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class ImageGenerator:
    def __init__(self) -> None:
        self.model_id = os.getenv("MODEL_ID", "runwayml/stable-diffusion-v1-5")
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.torch_dtype = torch.float16 if self.device == "cuda" else torch.float32
        self.mock_mode = os.getenv("MOCK_IMAGEGEN", "0") == "1"
        self._pipe: object | None = None

    def load(self) -> None:
        if self.mock_mode:
            self._pipe = "mock"
            return

        from diffusers import StableDiffusionPipeline

        pipe = StableDiffusionPipeline.from_pretrained(
            self.model_id,
            torch_dtype=self.torch_dtype,
            use_safetensors=True,
            safety_checker=None,
            requires_safety_checker=False,
        )
        pipe = pipe.to(self.device)
        pipe.enable_attention_slicing()

        if self.device == "cuda":
            try:
                pipe.enable_xformers_memory_efficient_attention()
            except Exception:
                # xformers may not be available in every Colab runtime.
                pass

        self._pipe = pipe

    def generate(self, req: GenerateRequest) -> bytes:
        if self._pipe is None:
            raise RuntimeError("Pipeline has not been loaded")

        if self.mock_mode:
            image = Image.new("RGB", (req.width, req.height), color=(25, 42, 78))
            draw = ImageDraw.Draw(image)
            draw.text((16, 16), f"MOCK\\n{req.prompt[:80]}", fill=(240, 240, 240))
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()

        generator = None
        if req.seed is not None:
            generator = torch.Generator(device=self.device).manual_seed(req.seed)

        with torch.inference_mode():
            result = self._pipe(
                prompt=req.prompt,
                negative_prompt=req.negative_prompt,
                width=req.width,
                height=req.height,
                num_inference_steps=req.num_inference_steps,
                guidance_scale=req.guidance_scale,
                generator=generator,
            )

        image: Image.Image = result.images[0]
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


class WhisperTranscriber:
    def __init__(self) -> None:
        self.model_id = os.getenv("WHISPER_MODEL_ID", "openai/whisper-small")
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.torch_dtype = torch.float16 if self.device == "cuda" else torch.float32
        self.mock_mode = os.getenv("MOCK_ASR", "0") == "1"
        self._asr_pipe: object | None = None
        self._load_lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        if self._asr_pipe is not None:
            return
        with self._load_lock:
            if self._asr_pipe is not None:
                return
            self._load()

    def _load(self) -> None:
        if self.mock_mode:
            self._asr_pipe = "mock"
            return

        from transformers import pipeline

        device = 0 if self.device == "cuda" else -1
        self._asr_pipe = pipeline(
            task="automatic-speech-recognition",
            model=self.model_id,
            device=device,
            model_kwargs={"torch_dtype": self.torch_dtype},
        )

    @staticmethod
    def _normalize_segment_timestamp(raw: object) -> tuple[float, float] | None:
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            return None
        start_raw, end_raw = raw
        if start_raw is None or end_raw is None:
            return None
        try:
            start = float(start_raw)
            end = float(end_raw)
        except (TypeError, ValueError):
            return None
        if start < 0 or end < 0 or end < start:
            return None
        return start, end

    @classmethod
    def _normalize_segments(cls, raw_chunks: object) -> list[AsrSegment]:
        if not isinstance(raw_chunks, list):
            return []
        segments: list[AsrSegment] = []
        for chunk in raw_chunks:
            if not isinstance(chunk, dict):
                continue
            text = str(chunk.get("text", "")).strip()
            if not text:
                continue
            ts = cls._normalize_segment_timestamp(chunk.get("timestamp"))
            if ts is None:
                continue
            start, end = ts
            segments.append(AsrSegment(start_sec=start, end_sec=end, text=text))
        return segments

    def transcribe(self, audio_path: Path) -> tuple[str, str | None, list[AsrSegment]]:
        self._ensure_loaded()
        if self._asr_pipe is None:
            raise RuntimeError("Whisper pipeline has not been loaded")

        if self.mock_mode:
            mock_text = f"MOCK transcription for {audio_path.name}"
            return mock_text, "mock", [AsrSegment(start_sec=0.0, end_sec=1.0, text=mock_text)]

        with torch.inference_mode():
            result = self._asr_pipe(
                str(audio_path),
                generate_kwargs={"task": "transcribe"},
                return_timestamps="chunk",
            )

        if not isinstance(result, dict):
            raise RuntimeError("Unexpected ASR result")
        text = str(result.get("text", "")).strip()
        language_raw = result.get("language")
        language = str(language_raw).strip() if language_raw else None
        if not text:
            raise RuntimeError("Empty transcription result")
        segments = self._normalize_segments(result.get("chunks"))
        if not segments:
            segments = [AsrSegment(start_sec=0.0, end_sec=0.0, text=text)]
        return text, language, segments


class OmniParserEngine:
    def __init__(self, *, enabled: bool) -> None:
        self.model_id = os.getenv("OMNIPARSER_MODEL_ID", "microsoft/OmniParser-v2.0")
        self.enabled = enabled
        self.mock_mode = _parse_bool_env("MOCK_UIPARSE", True)

    def parse(self, image_path: Path) -> tuple[str, list[UiElement]]:
        with Image.open(image_path) as image:
            width, height = image.size

        width = max(width, 1)
        height = max(height, 1)
        elements = [
            UiElement(
                element="screen",
                bbox=[0.0, 0.0, float(width), float(height)],
                confidence=0.99,
                text=None,
            ),
            UiElement(
                element="center_region",
                bbox=[
                    round(width * 0.2, 2),
                    round(height * 0.2, 2),
                    round(width * 0.8, 2),
                    round(height * 0.8, 2),
                ],
                confidence=0.8,
                text="placeholder",
            ),
        ]

        if self.mock_mode or not self.enabled:
            return "mock", elements

        # Placeholder path after dependency/weights bootstrap in M2-1.
        # The real OmniParser execution flow will replace this in later task.
        return "placeholder", elements


class ServiceState:
    def __init__(self) -> None:
        self.max_queue_size = int(os.getenv("MAX_QUEUE_SIZE", "16"))
        self.max_steps = int(os.getenv("MAX_STEPS", "30"))
        self.max_width = int(os.getenv("MAX_WIDTH", "768"))
        self.max_height = int(os.getenv("MAX_HEIGHT", "768"))
        self.max_jobs = int(os.getenv("MAX_JOBS", "256"))
        self.asr_max_upload_bytes = int(os.getenv("ASR_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
        self.ui_parse_max_upload_bytes = int(os.getenv("UI_PARSE_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
        self.gpu_breaker_threshold_ratio = float(os.getenv("GPU_MEMORY_BREAKER_THRESHOLD_RATIO", "0.92"))
        self.gpu_breaker_force_open = _parse_bool_env("GPU_MEMORY_FORCE_OPEN", False)
        self.omniparser_enabled = _parse_bool_env("OMNIPARSER_ENABLED", False)
        self.omniparser_repo_dir = Path(os.getenv("OMNIPARSER_DIR", "/content/.cache/omniparser/repo")).expanduser()
        self.omniparser_weights_dir = Path(
            os.getenv("OMNIPARSER_WEIGHTS_DIR", "/content/.cache/omniparser/weights")
        ).expanduser()
        self.output_dir = Path(os.getenv("OUTPUT_DIR", "/tmp/colab-imagegen/outputs")).expanduser()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.asr_upload_dir = self.output_dir / "asr_uploads"
        self.asr_upload_dir.mkdir(parents=True, exist_ok=True)
        self.ui_parse_upload_dir = self.output_dir / "ui_parse_uploads"
        self.ui_parse_upload_dir.mkdir(parents=True, exist_ok=True)

        token = os.getenv("API_BEARER_TOKEN", "").strip()
        if not token:
            raise RuntimeError("API_BEARER_TOKEN is required")
        self.api_bearer_token = token

        self.generator = ImageGenerator()
        self.whisper = WhisperTranscriber()
        self.omniparser_engine = OmniParserEngine(enabled=self.omniparser_enabled)
        self.queue: asyncio.Queue[QueueItem] = asyncio.Queue(maxsize=self.max_queue_size)
        self.jobs: dict[str, JobResponse] = {}
        self.image_paths: dict[str, Path] = {}
        self.worker: asyncio.Task[None] | None = None
        self.image_submitted_total = 0
        self.image_succeeded_total = 0
        self.image_failed_total = 0
        self.image_duration_ms_sum = 0
        self.image_last_duration_ms = 0
        self.asr_submitted_total = 0
        self.asr_succeeded_total = 0
        self.asr_failed_total = 0
        self.asr_duration_ms_sum = 0
        self.asr_last_duration_ms = 0
        self.gpu_breaker_triggered_total = 0

    def require_auth(self, authorization: str | None) -> None:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token")

        incoming = authorization.removeprefix("Bearer ").strip()
        if not incoming or not secrets.compare_digest(incoming, self.api_bearer_token):
            raise HTTPException(status_code=401, detail="Invalid bearer token")

    async def start(self) -> None:
        await asyncio.to_thread(self.generator.load)
        self.worker = asyncio.create_task(self._worker_loop(), name="imagegen-worker")

    async def stop(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker

    async def submit(self, req: GenerateRequest) -> GenerateAccepted:
        self._validate_request(req)
        self._check_heavy_task_guard()
        self.image_submitted_total += 1

        job_id = str(uuid.uuid4())
        self._create_job(
            job_id=job_id,
            task_type="image_gen",
            status="queued",
            width=req.width,
            height=req.height,
            num_inference_steps=req.num_inference_steps,
        )

        try:
            self.queue.put_nowait(QueueItem(job_id=job_id, request=req))
        except asyncio.QueueFull:
            self.jobs.pop(job_id, None)
            raise HTTPException(status_code=429, detail="Queue is full")

        return GenerateAccepted(job_id=job_id, status="queued")

    def get_job(self, job_id: str) -> JobResponse:
        job = self.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    def _create_job(
        self,
        *,
        job_id: str,
        task_type: TaskType,
        status: JobStatus,
        width: int | None = None,
        height: int | None = None,
        num_inference_steps: int | None = None,
        input_filename: str | None = None,
    ) -> JobResponse:
        now = time.time()
        job = JobResponse(
            job_id=job_id,
            task_type=task_type,
            status=status,
            created_at=now,
            started_at=None,
            completed_at=None,
            error=None,
            width=width,
            height=height,
            num_inference_steps=num_inference_steps,
            input_filename=input_filename,
        )
        self.jobs[job_id] = job
        self._evict_old_jobs()
        return job

    def get_image(self, job_id: str) -> bytes:
        job = self.get_job(job_id)
        if job.status == "failed":
            raise HTTPException(status_code=409, detail=f"Job failed: {job.error}")
        if job.status != "succeeded":
            raise HTTPException(status_code=409, detail="Job is not complete")

        path = self.image_paths.get(job_id)
        if path is None or not path.exists():
            raise HTTPException(status_code=404, detail="Image payload is unavailable")
        return path.read_bytes()

    def health(self) -> dict[str, object]:
        image_completed_total = self.image_succeeded_total + self.image_failed_total
        asr_completed_total = self.asr_succeeded_total + self.asr_failed_total
        image_avg_ms = int(self.image_duration_ms_sum / image_completed_total) if image_completed_total else 0
        asr_avg_ms = int(self.asr_duration_ms_sum / asr_completed_total) if asr_completed_total else 0
        gpu_memory = self._gpu_memory_metrics()
        gpu_guard_open, guard_reason = self._gpu_guard_state(gpu_memory)
        omniparser = self._omniparser_state()

        return {
            "status": "ok",
            "model_id": self.generator.model_id,
            "device": self.generator.device,
            "queue_size": self.queue.qsize(),
            "queue_capacity": self.max_queue_size,
            "max_width": self.max_width,
            "max_height": self.max_height,
            "max_steps": self.max_steps,
            "output_dir": str(self.output_dir),
            "asr_upload_dir": str(self.asr_upload_dir),
            "asr_max_upload_bytes": self.asr_max_upload_bytes,
            "ui_parse_upload_dir": str(self.ui_parse_upload_dir),
            "ui_parse_max_upload_bytes": self.ui_parse_max_upload_bytes,
            "whisper_model_id": self.whisper.model_id,
            "whisper_device": self.whisper.device,
            "omniparser": omniparser,
            "metrics": {
                "queue": {
                    "size": self.queue.qsize(),
                    "capacity": self.max_queue_size,
                },
                "image_jobs": {
                    "submitted_total": self.image_submitted_total,
                    "succeeded_total": self.image_succeeded_total,
                    "failed_total": self.image_failed_total,
                    "last_duration_ms": self.image_last_duration_ms,
                    "avg_duration_ms": image_avg_ms,
                },
                "asr_jobs": {
                    "submitted_total": self.asr_submitted_total,
                    "succeeded_total": self.asr_succeeded_total,
                    "failed_total": self.asr_failed_total,
                    "last_duration_ms": self.asr_last_duration_ms,
                    "avg_duration_ms": asr_avg_ms,
                },
                "gpu_memory": {
                    **gpu_memory,
                    "guard": {
                        "open": gpu_guard_open,
                        "reason": guard_reason,
                        "threshold_ratio": self.gpu_breaker_threshold_ratio,
                        "triggered_total": self.gpu_breaker_triggered_total,
                    },
                },
            },
        }

    def _omniparser_state(self) -> dict[str, object]:
        if not self.omniparser_enabled:
            return {
                "enabled": False,
                "ready": False,
                "reason": "disabled",
                "engine_mode": "mock" if self.omniparser_engine.mock_mode else "disabled",
                "model_id": self.omniparser_engine.model_id,
                "repo_dir": str(self.omniparser_repo_dir),
                "weights_dir": str(self.omniparser_weights_dir),
            }

        required_paths = [
            self.omniparser_repo_dir / "util" / "omniparser.py",
            self.omniparser_weights_dir / "icon_detect" / "model.pt",
            self.omniparser_weights_dir / "icon_caption_florence" / "model.safetensors",
        ]
        missing = [str(path) for path in required_paths if not path.exists()]
        return {
            "enabled": True,
            "ready": len(missing) == 0,
            "engine_mode": "mock" if self.omniparser_engine.mock_mode else "placeholder",
            "model_id": self.omniparser_engine.model_id,
            "repo_dir": str(self.omniparser_repo_dir),
            "weights_dir": str(self.omniparser_weights_dir),
            "missing_files": missing,
        }

    def _gpu_memory_metrics(self) -> dict[str, object]:
        if not torch.cuda.is_available():
            return {
                "available": False,
                "device": "cpu",
            }

        try:
            free_bytes, total_bytes = torch.cuda.mem_get_info()
            used_bytes = max(total_bytes - free_bytes, 0)
            used_ratio = (used_bytes / total_bytes) if total_bytes > 0 else 0.0
            return {
                "available": True,
                "device": "cuda",
                "free_bytes": int(free_bytes),
                "used_bytes": int(used_bytes),
                "total_bytes": int(total_bytes),
                "used_ratio": round(float(used_ratio), 6),
            }
        except Exception as exc:
            return {
                "available": False,
                "device": "cuda",
                "error": str(exc),
            }

    def _gpu_guard_state(self, gpu_memory: dict[str, object] | None = None) -> tuple[bool, str]:
        if self.gpu_breaker_force_open:
            return True, "force_open"

        stats = gpu_memory if gpu_memory is not None else self._gpu_memory_metrics()
        if not bool(stats.get("available")):
            return False, "gpu_unavailable"

        used_ratio_raw = stats.get("used_ratio")
        if isinstance(used_ratio_raw, (float, int)) and used_ratio_raw >= self.gpu_breaker_threshold_ratio:
            return True, f"used_ratio={float(used_ratio_raw):.4f}"

        return False, "ok"

    def _check_heavy_task_guard(self) -> None:
        guard_open, guard_reason = self._gpu_guard_state()
        if not guard_open:
            return

        self.gpu_breaker_triggered_total += 1
        raise HTTPException(
            status_code=429,
            detail=f"gpu memory guard is open ({guard_reason}), reject heavy task",
        )

    async def _save_asr_upload(self, file: UploadFile) -> SavedAsrUpload:
        filename = Path((file.filename or "").strip()).name
        if not filename:
            raise HTTPException(status_code=400, detail="audio file name is required")

        content_type = (file.content_type or "").lower()
        suffix = Path(filename).suffix.lower()
        allowed_suffixes = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm", ".mp4"}
        if suffix not in allowed_suffixes and not content_type.startswith("audio/"):
            raise HTTPException(status_code=400, detail="unsupported audio format")

        upload_id = str(uuid.uuid4())
        path = self.asr_upload_dir / f"{upload_id}{suffix or '.bin'}"
        size_bytes = 0

        try:
            with path.open("wb") as output:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size_bytes += len(chunk)
                    if size_bytes > self.asr_max_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"audio file too large, max {self.asr_max_upload_bytes} bytes",
                        )
                    output.write(chunk)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

        if size_bytes == 0:
            path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="empty audio file")

        return SavedAsrUpload(
            upload_id=upload_id,
            filename=filename,
            content_type=file.content_type,
            size_bytes=size_bytes,
            path=path,
        )

    async def transcribe_audio(self, file: UploadFile) -> AsrTranscribeResponse:
        self.asr_submitted_total += 1
        saved = await self._save_asr_upload(file)
        job = self._create_job(
            job_id=saved.upload_id,
            task_type="asr_whisper_small",
            status="running",
            input_filename=saved.filename,
        )
        started_at = time.time()
        job.started_at = started_at
        try:
            text, language, segments = await asyncio.to_thread(self.whisper.transcribe, saved.path)
        except Exception as exc:
            elapsed_ms = int((time.time() - started_at) * 1000)
            self.asr_failed_total += 1
            self.asr_duration_ms_sum += elapsed_ms
            self.asr_last_duration_ms = elapsed_ms
            job.status = "failed"
            job.completed_at = time.time()
            job.error = str(exc)
            raise HTTPException(status_code=500, detail=f"asr transcription failed: {exc}") from exc

        elapsed_ms = int((time.time() - started_at) * 1000)
        self.asr_succeeded_total += 1
        self.asr_duration_ms_sum += elapsed_ms
        self.asr_last_duration_ms = elapsed_ms
        job.status = "succeeded"
        job.completed_at = time.time()
        job.error = None
        return AsrTranscribeResponse(
            job_id=saved.upload_id,
            upload_id=saved.upload_id,
            status="succeeded",
            filename=saved.filename,
            content_type=saved.content_type,
            size_bytes=saved.size_bytes,
            text=text,
            segments=segments,
            language=language,
            model_id=self.whisper.model_id,
            elapsed_ms=elapsed_ms,
        )

    async def _save_ui_upload(self, file: UploadFile) -> SavedUiUpload:
        filename = Path((file.filename or "").strip()).name
        if not filename:
            raise HTTPException(status_code=400, detail="image file name is required")

        content_type = (file.content_type or "").lower()
        suffix = Path(filename).suffix.lower()
        allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
        if suffix not in allowed_suffixes and not content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="unsupported image format")

        parse_id = str(uuid.uuid4())
        path = self.ui_parse_upload_dir / f"{parse_id}{suffix or '.bin'}"
        size_bytes = 0

        try:
            with path.open("wb") as output:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size_bytes += len(chunk)
                    if size_bytes > self.ui_parse_max_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"image file too large, max {self.ui_parse_max_upload_bytes} bytes",
                        )
                    output.write(chunk)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

        if size_bytes == 0:
            path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="empty image file")

        return SavedUiUpload(
            parse_id=parse_id,
            filename=filename,
            content_type=file.content_type,
            size_bytes=size_bytes,
            path=path,
        )

    async def parse_ui_image(self, file: UploadFile) -> UiParseResponse:
        saved = await self._save_ui_upload(file)
        job = self._create_job(
            job_id=saved.parse_id,
            task_type="ui_parse_omniparser",
            status="running",
            input_filename=saved.filename,
        )
        started_at = time.time()
        job.started_at = started_at
        try:
            engine_mode, elements = await asyncio.to_thread(self.omniparser_engine.parse, saved.path)
        except Exception as exc:
            job.status = "failed"
            job.completed_at = time.time()
            job.error = str(exc)
            raise HTTPException(status_code=500, detail=f"ui parse failed: {exc}") from exc

        elapsed_ms = int((time.time() - started_at) * 1000)
        job.status = "succeeded"
        job.completed_at = time.time()
        job.error = None
        return UiParseResponse(
            job_id=saved.parse_id,
            parse_id=saved.parse_id,
            status="succeeded",
            filename=saved.filename,
            content_type=saved.content_type,
            size_bytes=saved.size_bytes,
            model_id=self.omniparser_engine.model_id,
            engine_mode=engine_mode,
            elements=elements,
            elapsed_ms=elapsed_ms,
        )

    def _validate_request(self, req: GenerateRequest) -> None:
        if req.width > self.max_width or req.height > self.max_height:
            raise HTTPException(
                status_code=400,
                detail=f"width/height must be <= {self.max_width}/{self.max_height}",
            )
        if req.width % 8 != 0 or req.height % 8 != 0:
            raise HTTPException(status_code=400, detail="width and height must be multiples of 8")
        if req.num_inference_steps > self.max_steps:
            raise HTTPException(status_code=400, detail=f"num_inference_steps must be <= {self.max_steps}")

    def _write_image(self, job_id: str, image_bytes: bytes) -> Path:
        path = self.output_dir / f"{job_id}.png"
        path.write_bytes(image_bytes)
        return path

    def _evict_old_jobs(self) -> None:
        if len(self.jobs) <= self.max_jobs:
            return

        removable = [job for job in self.jobs.values() if job.status in {"succeeded", "failed"}]
        removable.sort(key=lambda item: item.completed_at or item.created_at)

        while len(self.jobs) > self.max_jobs and removable:
            victim = removable.pop(0)
            self.jobs.pop(victim.job_id, None)
            path = self.image_paths.pop(victim.job_id, None)
            if path is not None:
                path.unlink(missing_ok=True)

    async def _worker_loop(self) -> None:
        while True:
            item = await self.queue.get()
            job = self.jobs.get(item.job_id)
            if job is None:
                self.queue.task_done()
                continue

            job.status = "running"
            job.started_at = time.time()

            try:
                image_bytes = await asyncio.to_thread(self.generator.generate, item.request)
                path = await asyncio.to_thread(self._write_image, item.job_id, image_bytes)
                self.image_paths[item.job_id] = path
                job.status = "succeeded"
                job.completed_at = time.time()
                job.error = None
                elapsed_ms = int((job.completed_at - (job.started_at or job.completed_at)) * 1000)
                self.image_succeeded_total += 1
                self.image_duration_ms_sum += elapsed_ms
                self.image_last_duration_ms = elapsed_ms
            except Exception as exc:
                job.status = "failed"
                job.completed_at = time.time()
                job.error = str(exc)
                elapsed_ms = int((job.completed_at - (job.started_at or job.completed_at)) * 1000)
                self.image_failed_total += 1
                self.image_duration_ms_sum += elapsed_ms
                self.image_last_duration_ms = elapsed_ms
            finally:
                self.queue.task_done()


state = ServiceState()
BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "static" / "index.html"


@asynccontextmanager
async def lifespan(_: FastAPI):
    await state.start()
    try:
        yield
    finally:
        await state.stop()


app = FastAPI(title="colab-imagegen", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_parse_csv_env("CORS_ALLOW_ORIGINS", "*"),
    allow_credentials=_parse_bool_env("CORS_ALLOW_CREDENTIALS", False),
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_auth(authorization: str | None = Header(default=None)) -> None:
    state.require_auth(authorization)


@app.get("/")
def index() -> Response:
    if INDEX_FILE.exists():
        return FileResponse(INDEX_FILE)
    return JSONResponse({"message": "Frontend file not found"}, status_code=404)


@app.get("/healthz")
def healthz() -> dict[str, object]:
    return state.health()


@app.post("/generate", response_model=GenerateAccepted, status_code=202)
async def generate(req: GenerateRequest, _: None = Depends(require_auth)) -> GenerateAccepted:
    return await state.submit(req)


@app.post("/asr/whisper/transcribe", response_model=AsrTranscribeResponse)
async def transcribe_whisper(
    file: UploadFile = File(...),
    _: None = Depends(require_auth),
) -> AsrTranscribeResponse:
    return await state.transcribe_audio(file)


@app.post("/ui/parse", response_model=UiParseResponse)
async def parse_ui(
    file: UploadFile = File(...),
    _: None = Depends(require_auth),
) -> UiParseResponse:
    return await state.parse_ui_image(file)


@app.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, _: None = Depends(require_auth)) -> JobResponse:
    return state.get_job(job_id)


@app.get("/jobs/{job_id}/image")
def get_image(job_id: str, _: None = Depends(require_auth)) -> Response:
    image = state.get_image(job_id)
    return Response(content=image, media_type="image/png")
