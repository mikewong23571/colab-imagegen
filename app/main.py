from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import importlib
import os
import secrets
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Header, HTTPException, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from PIL import Image, ImageDraw
import torch


JobStatus = Literal["queued", "running", "succeeded", "failed"]
TaskType = Literal["image_gen", "asr_whisper_small", "ui_parse_omniparser"]


class RetryStrategy(BaseModel):
    should_retry: bool
    backoff_ms: int | None = None
    max_retries: int | None = None


class ErrorResponse(BaseModel):
    error: str
    message: str
    retry_strategy: RetryStrategy


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
class ImageQueueItem:
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


@dataclass
class AsrQueueItem:
    saved_upload: SavedAsrUpload
    result_future: asyncio.Future[AsrTranscribeResponse]


@dataclass
class UiParseQueueItem:
    saved_upload: SavedUiUpload
    result_future: asyncio.Future[UiParseResponse]


HeavyQueueItem = ImageQueueItem | UiParseQueueItem


def _parse_csv_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or ["*"]


def _parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int_env(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw.strip())
        except ValueError as exc:
            raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return value


def _parse_float_env(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.getenv(name)
    if raw is None:
        value = default
    else:
        try:
            value = float(raw.strip())
        except ValueError as exc:
            raise RuntimeError(f"{name} must be a float") from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return value


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
    def __init__(self, *, enabled: bool, repo_dir: Path, weights_dir: Path) -> None:
        self.model_id = os.getenv("OMNIPARSER_MODEL_ID", "microsoft/OmniParser-v2.0")
        self.enabled = enabled
        self.mock_mode = _parse_bool_env("MOCK_UIPARSE", True)
        self.repo_dir = repo_dir
        self.weights_dir = weights_dir
        self.caption_model_name = os.getenv("OMNIPARSER_CAPTION_MODEL_NAME", "florence2")
        self.box_threshold = _parse_float_env("OMNIPARSER_BOX_THRESHOLD", 0.05, minimum=0.0)
        self.default_confidence = min(
            max(_parse_float_env("OMNIPARSER_DEFAULT_CONFIDENCE", 0.5, minimum=0.0), 0.0),
            1.0,
        )
        self._native_engine: object | None = None
        self._load_error: str | None = None
        self._load_lock = threading.Lock()

    @property
    def required_paths(self) -> list[Path]:
        return [
            self.repo_dir / "util" / "omniparser.py",
            self.weights_dir / "icon_detect" / "model.pt",
            self.weights_dir / "icon_caption_florence" / "model.safetensors",
        ]

    def missing_required_paths(self) -> list[Path]:
        return [path for path in self.required_paths if not path.exists()]

    @property
    def ready(self) -> bool:
        if not self.enabled:
            return False
        if self.mock_mode:
            return True
        return len(self.missing_required_paths()) == 0

    @property
    def loaded(self) -> bool:
        return self._native_engine is not None

    @property
    def load_error(self) -> str | None:
        return self._load_error

    @property
    def engine_mode(self) -> str:
        if not self.enabled:
            return "mock" if self.mock_mode else "disabled"
        if self.mock_mode:
            return "mock"
        if self._native_engine is not None:
            return "native"
        if self._load_error:
            return "native_error"
        return "native_unloaded"

    def _load(self) -> None:
        missing = self.missing_required_paths()
        if missing:
            missing_str = ", ".join(str(path) for path in missing)
            raise RuntimeError(f"omniparser required files are missing: {missing_str}")

        repo_dir_str = str(self.repo_dir)
        if repo_dir_str not in sys.path:
            sys.path.insert(0, repo_dir_str)

        config = {
            "som_model_path": str(self.weights_dir / "icon_detect" / "model.pt"),
            "caption_model_name": self.caption_model_name,
            "caption_model_path": str(self.weights_dir / "icon_caption_florence"),
            "device": "cuda" if torch.cuda.is_available() else "cpu",
            "BOX_TRESHOLD": self.box_threshold,
        }

        module = importlib.import_module("util.omniparser")
        omniparser_cls = getattr(module, "Omniparser", None)
        if omniparser_cls is None:
            raise RuntimeError("OmniParser class `Omniparser` not found in util.omniparser")

        self._native_engine = omniparser_cls(config)
        self._load_error = None

    def _ensure_loaded(self) -> None:
        if self._native_engine is not None:
            return
        if self._load_error is not None:
            raise RuntimeError(f"failed to initialize omniparser engine: {self._load_error}")
        with self._load_lock:
            if self._native_engine is not None:
                return
            if self._load_error is not None:
                raise RuntimeError(f"failed to initialize omniparser engine: {self._load_error}")
            try:
                self._load()
            except Exception as exc:
                self._load_error = str(exc)
                raise RuntimeError(f"failed to initialize omniparser engine: {exc}") from exc

    def _normalize_bbox(self, raw_bbox: object, width: int, height: int) -> list[float] | None:
        if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
            return None
        try:
            x1, y1, x2, y2 = [float(item) for item in raw_bbox]
        except (TypeError, ValueError):
            return None

        # Upstream OmniParser usually returns [x1, y1, x2, y2] in ratio space.
        # A few variants may return pixel space; support both.
        if max(abs(x1), abs(y1), abs(x2), abs(y2)) <= 1.5:
            x1 *= width
            x2 *= width
            y1 *= height
            y2 *= height

        left = max(0.0, min(float(width), min(x1, x2)))
        right = max(0.0, min(float(width), max(x1, x2)))
        top = max(0.0, min(float(height), min(y1, y2)))
        bottom = max(0.0, min(float(height), max(y1, y2)))
        if right <= left or bottom <= top:
            return None
        return [round(left, 2), round(top, 2), round(right, 2), round(bottom, 2)]

    def _normalize_elements(self, raw_items: object, width: int, height: int) -> list[UiElement]:
        if not isinstance(raw_items, list):
            return []
        elements: list[UiElement] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            bbox = self._normalize_bbox(item.get("bbox"), width, height)
            if bbox is None:
                continue
            raw_element = str(item.get("type") or "unknown")
            if item.get("interactivity") is True and raw_element == "icon":
                raw_element = "icon_interactive"
            raw_text = item.get("content")
            text = str(raw_text).strip() if raw_text is not None else None
            if text == "":
                text = None
            confidence_raw = item.get("confidence")
            if isinstance(confidence_raw, (int, float)):
                confidence = min(max(float(confidence_raw), 0.0), 1.0)
            else:
                confidence = self.default_confidence
            elements.append(
                UiElement(
                    element=raw_element,
                    bbox=bbox,
                    confidence=confidence,
                    text=text,
                )
            )
        return elements

    def _placeholder_elements(self, width: int, height: int) -> list[UiElement]:
        width = max(width, 1)
        height = max(height, 1)
        return [
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

    def _parse_native(self, image_path: Path) -> list[UiElement]:
        self._ensure_loaded()
        if self._native_engine is None:
            raise RuntimeError("OmniParser engine is not loaded")

        with Image.open(image_path) as image:
            width, height = image.size
        width = max(width, 1)
        height = max(height, 1)

        image_base64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        parse_fn = getattr(self._native_engine, "parse", None)
        if not callable(parse_fn):
            raise RuntimeError("OmniParser engine does not expose callable `parse`")
        parse_output = parse_fn(image_base64)

        parsed_items: object = None
        if isinstance(parse_output, tuple):
            if len(parse_output) >= 2:
                parsed_items = parse_output[1]
        elif isinstance(parse_output, list):
            parsed_items = parse_output

        elements = self._normalize_elements(parsed_items, width=width, height=height)
        if not elements:
            elements = self._placeholder_elements(width=width, height=height)
        return elements

    def parse(self, image_path: Path) -> tuple[str, list[UiElement]]:
        with Image.open(image_path) as image:
            width, height = image.size

        elements = self._placeholder_elements(width=width, height=height)

        if self.mock_mode or not self.enabled:
            return "mock", elements

        native_elements = self._parse_native(image_path)
        return "native", native_elements


class ServiceState:
    def __init__(self) -> None:
        legacy_max_queue_size = _parse_int_env("MAX_QUEUE_SIZE", 16, minimum=1)
        self.heavy_queue_max_size = _parse_int_env(
            "HEAVY_QUEUE_MAX_SIZE",
            legacy_max_queue_size,
            minimum=1,
        )
        self.light_queue_max_size = _parse_int_env(
            "LIGHT_QUEUE_MAX_SIZE",
            max(legacy_max_queue_size, 16),
            minimum=1,
        )
        self.heavy_queue_concurrency = _parse_int_env("HEAVY_QUEUE_CONCURRENCY", 1, minimum=1)
        self.light_queue_concurrency = _parse_int_env("LIGHT_QUEUE_CONCURRENCY", 2, minimum=1)
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
        self.omniparser_engine = OmniParserEngine(
            enabled=self.omniparser_enabled,
            repo_dir=self.omniparser_repo_dir,
            weights_dir=self.omniparser_weights_dir,
        )
        self.heavy_queue: asyncio.Queue[HeavyQueueItem] = asyncio.Queue(maxsize=self.heavy_queue_max_size)
        self.light_queue: asyncio.Queue[AsrQueueItem] = asyncio.Queue(maxsize=self.light_queue_max_size)
        self.heavy_task_runtime_limit = 1
        self.heavy_task_semaphore = asyncio.Semaphore(self.heavy_task_runtime_limit)
        self.heavy_tasks_running = 0
        self.heavy_tasks_max_running_seen = 0
        self.jobs: dict[str, JobResponse] = {}
        self.image_paths: dict[str, Path] = {}
        self.heavy_workers: list[asyncio.Task[None]] = []
        self.light_workers: list[asyncio.Task[None]] = []
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
        self.ui_parse_submitted_total = 0
        self.ui_parse_succeeded_total = 0
        self.ui_parse_failed_total = 0
        self.ui_parse_duration_ms_sum = 0
        self.ui_parse_last_duration_ms = 0
        self.ui_parse_last_elements_count = 0
        self.ui_parse_last_engine_mode: str | None = None
        self.gpu_breaker_triggered_total = 0

    def require_auth(self, authorization: str | None) -> None:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token")

        incoming = authorization.removeprefix("Bearer ").strip()
        if not incoming or not secrets.compare_digest(incoming, self.api_bearer_token):
            raise HTTPException(status_code=401, detail="Invalid bearer token")

    async def start(self) -> None:
        await asyncio.to_thread(self.generator.load)
        self.heavy_workers = [
            asyncio.create_task(self._heavy_worker_loop(index + 1), name=f"heavy-worker-{index + 1}")
            for index in range(self.heavy_queue_concurrency)
        ]
        self.light_workers = [
            asyncio.create_task(self._light_worker_loop(index + 1), name=f"light-worker-{index + 1}")
            for index in range(self.light_queue_concurrency)
        ]

    async def stop(self) -> None:
        workers = [*self.heavy_workers, *self.light_workers]
        self.heavy_workers = []
        self.light_workers = []
        for worker in workers:
            worker.cancel()
        for worker in workers:
            with contextlib.suppress(asyncio.CancelledError):
                await worker

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
            self.heavy_queue.put_nowait(ImageQueueItem(job_id=job_id, request=req))
        except asyncio.QueueFull:
            self.jobs.pop(job_id, None)
            raise HTTPException(status_code=429, detail="heavy queue is full")

        return GenerateAccepted(job_id=job_id, status="queued")

    def _check_ui_parse_runtime_ready(self) -> None:
        if self.omniparser_engine.mock_mode:
            return
        if not self.omniparser_enabled:
            raise HTTPException(
                status_code=503,
                detail="omniparser is disabled; set OMNIPARSER_ENABLED=1 or use MOCK_UIPARSE=1",
            )
        if self.omniparser_engine.load_error:
            raise HTTPException(
                status_code=503,
                detail=f"omniparser is not ready ({self.omniparser_engine.load_error})",
            )
        missing = [str(path) for path in self.omniparser_engine.missing_required_paths()]
        if missing:
            raise HTTPException(
                status_code=503,
                detail=f"omniparser required files are missing: {', '.join(missing)}",
            )

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
        ui_parse_completed_total = self.ui_parse_succeeded_total + self.ui_parse_failed_total
        image_avg_ms = int(self.image_duration_ms_sum / image_completed_total) if image_completed_total else 0
        asr_avg_ms = int(self.asr_duration_ms_sum / asr_completed_total) if asr_completed_total else 0
        ui_parse_avg_ms = (
            int(self.ui_parse_duration_ms_sum / ui_parse_completed_total) if ui_parse_completed_total else 0
        )
        gpu_memory = self._gpu_memory_metrics()
        gpu_guard_open, guard_reason = self._gpu_guard_state(gpu_memory)
        omniparser = self._omniparser_state()

        return {
            "status": "ok",
            "model_id": self.generator.model_id,
            "device": self.generator.device,
            "queue_size": self.heavy_queue.qsize(),
            "queue_capacity": self.heavy_queue_max_size,
            "heavy_queue_size": self.heavy_queue.qsize(),
            "heavy_queue_capacity": self.heavy_queue_max_size,
            "light_queue_size": self.light_queue.qsize(),
            "light_queue_capacity": self.light_queue_max_size,
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
                    "size": self.heavy_queue.qsize(),
                    "capacity": self.heavy_queue_max_size,
                    "heavy": {
                        "size": self.heavy_queue.qsize(),
                        "capacity": self.heavy_queue_max_size,
                        "concurrency": self.heavy_queue_concurrency,
                        "runtime_limit": self.heavy_task_runtime_limit,
                        "running": self.heavy_tasks_running,
                        "max_running_seen": self.heavy_tasks_max_running_seen,
                    },
                    "light": {
                        "size": self.light_queue.qsize(),
                        "capacity": self.light_queue_max_size,
                        "concurrency": self.light_queue_concurrency,
                    },
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
                "ui_parse_jobs": {
                    "submitted_total": self.ui_parse_submitted_total,
                    "succeeded_total": self.ui_parse_succeeded_total,
                    "failed_total": self.ui_parse_failed_total,
                    "last_duration_ms": self.ui_parse_last_duration_ms,
                    "avg_duration_ms": ui_parse_avg_ms,
                    "last_elements_count": self.ui_parse_last_elements_count,
                    "last_engine_mode": self.ui_parse_last_engine_mode,
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
        missing = [str(path) for path in self.omniparser_engine.missing_required_paths()]
        if not self.omniparser_enabled:
            reason = "disabled"
        elif self.omniparser_engine.mock_mode:
            reason = "mock_mode"
        elif missing:
            reason = "missing_files"
        elif self.omniparser_engine.load_error:
            reason = "load_error"
        else:
            reason = "ok"
        state = {
            "enabled": self.omniparser_enabled,
            "ready": self.omniparser_engine.ready,
            "reason": reason,
            "engine_mode": self.omniparser_engine.engine_mode,
            "model_id": self.omniparser_engine.model_id,
            "repo_dir": str(self.omniparser_repo_dir),
            "weights_dir": str(self.omniparser_weights_dir),
            "caption_model_name": self.omniparser_engine.caption_model_name,
            "box_threshold": self.omniparser_engine.box_threshold,
            "loaded": self.omniparser_engine.loaded,
            "missing_files": missing,
        }
        if self.omniparser_engine.load_error:
            state["load_error"] = self.omniparser_engine.load_error
        return state

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
        loop = asyncio.get_running_loop()
        result_future: asyncio.Future[AsrTranscribeResponse] = loop.create_future()
        job = self._create_job(
            job_id=saved.upload_id,
            task_type="asr_whisper_small",
            status="queued",
            input_filename=saved.filename,
        )

        try:
            self.light_queue.put_nowait(AsrQueueItem(saved_upload=saved, result_future=result_future))
        except asyncio.QueueFull:
            self.jobs.pop(job.job_id, None)
            saved.path.unlink(missing_ok=True)
            raise HTTPException(status_code=429, detail="light queue is full")

        return await result_future

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
        self._check_heavy_task_guard()
        self._check_ui_parse_runtime_ready()
        self.ui_parse_submitted_total += 1
        saved = await self._save_ui_upload(file)
        loop = asyncio.get_running_loop()
        result_future: asyncio.Future[UiParseResponse] = loop.create_future()
        job = self._create_job(
            job_id=saved.parse_id,
            task_type="ui_parse_omniparser",
            status="queued",
            input_filename=saved.filename,
        )

        try:
            self.heavy_queue.put_nowait(UiParseQueueItem(saved_upload=saved, result_future=result_future))
        except asyncio.QueueFull:
            self.jobs.pop(job.job_id, None)
            saved.path.unlink(missing_ok=True)
            raise HTTPException(status_code=429, detail="heavy queue is full")

        return await result_future

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

    @asynccontextmanager
    async def _reserve_heavy_task_slot(self):
        await self.heavy_task_semaphore.acquire()
        try:
            yield
        finally:
            self.heavy_task_semaphore.release()

    @asynccontextmanager
    async def _track_heavy_task_running(self):
        # Tracks active heavy executions for health/metrics without affecting admission.
        # Admission is enforced separately by reserving a semaphore slot before dequeue.
        self.heavy_tasks_running += 1
        if self.heavy_tasks_running > self.heavy_tasks_max_running_seen:
            self.heavy_tasks_max_running_seen = self.heavy_tasks_running
        try:
            yield
        finally:
            self.heavy_tasks_running = max(self.heavy_tasks_running - 1, 0)

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

    async def _process_image_job(self, item: ImageQueueItem) -> None:
        job = self.jobs.get(item.job_id)
        if job is None:
            return

        job.status = "running"
        job.started_at = time.time()

        try:
            async with self._track_heavy_task_running():
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

    async def _process_asr_job(self, item: AsrQueueItem) -> None:
        saved = item.saved_upload
        job = self.jobs.get(saved.upload_id)
        if job is None:
            if not item.result_future.done():
                item.result_future.set_exception(HTTPException(status_code=404, detail="Job not found"))
            return

        job.status = "running"
        started_at = time.time()
        job.started_at = started_at
        try:
            text, language, segments = await asyncio.to_thread(self.whisper.transcribe, saved.path)
            elapsed_ms = int((time.time() - started_at) * 1000)
            self.asr_succeeded_total += 1
            self.asr_duration_ms_sum += elapsed_ms
            self.asr_last_duration_ms = elapsed_ms
            job.status = "succeeded"
            job.completed_at = time.time()
            job.error = None
            response = AsrTranscribeResponse(
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
            if not item.result_future.done():
                item.result_future.set_result(response)
        except Exception as exc:
            elapsed_ms = int((time.time() - started_at) * 1000)
            self.asr_failed_total += 1
            self.asr_duration_ms_sum += elapsed_ms
            self.asr_last_duration_ms = elapsed_ms
            job.status = "failed"
            job.completed_at = time.time()
            job.error = str(exc)
            if not item.result_future.done():
                item.result_future.set_exception(HTTPException(status_code=500, detail=f"asr transcription failed: {exc}"))

    async def _process_ui_parse_job(self, item: UiParseQueueItem) -> None:
        saved = item.saved_upload
        job = self.jobs.get(saved.parse_id)
        if job is None:
            if not item.result_future.done():
                item.result_future.set_exception(HTTPException(status_code=404, detail="Job not found"))
            return

        job.status = "running"
        started_at = time.time()
        job.started_at = started_at
        try:
            async with self._track_heavy_task_running():
                engine_mode, elements = await asyncio.to_thread(self.omniparser_engine.parse, saved.path)
            elapsed_ms = int((time.time() - started_at) * 1000)
            self.ui_parse_succeeded_total += 1
            self.ui_parse_duration_ms_sum += elapsed_ms
            self.ui_parse_last_duration_ms = elapsed_ms
            self.ui_parse_last_elements_count = len(elements)
            self.ui_parse_last_engine_mode = engine_mode
            job.status = "succeeded"
            job.completed_at = time.time()
            job.error = None
            response = UiParseResponse(
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
            if not item.result_future.done():
                item.result_future.set_result(response)
        except Exception as exc:
            elapsed_ms = int((time.time() - started_at) * 1000)
            self.ui_parse_failed_total += 1
            self.ui_parse_duration_ms_sum += elapsed_ms
            self.ui_parse_last_duration_ms = elapsed_ms
            self.ui_parse_last_engine_mode = None
            self.ui_parse_last_elements_count = 0
            job.status = "failed"
            job.completed_at = time.time()
            job.error = str(exc)
            if not item.result_future.done():
                item.result_future.set_exception(HTTPException(status_code=500, detail=f"ui parse failed: {exc}"))

    async def _heavy_worker_loop(self, _: int) -> None:
        while True:
            async with self._reserve_heavy_task_slot():
                item = await self.heavy_queue.get()
                try:
                    if isinstance(item, ImageQueueItem):
                        await self._process_image_job(item)
                    else:
                        await self._process_ui_parse_job(item)
                finally:
                    self.heavy_queue.task_done()

    async def _light_worker_loop(self, _: int) -> None:
        while True:
            item = await self.light_queue.get()
            try:
                await self._process_asr_job(item)
            finally:
                self.light_queue.task_done()


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


def _build_error_response(status_code: int, detail: str) -> dict[str, object]:
    cat = "internal_error"
    retry = False
    backoff = None
    max_retries = None

    if status_code == 401:
        cat = "auth_error"
    elif status_code == 503:
        cat = "service_unavailable"
        retry = True
        backoff = 5000
        max_retries = 5
    elif status_code == 429:
        if "queue is full" in detail.lower():
            cat = "queue_full"
            retry = True
            backoff = 2000
            max_retries = 3
        elif "guard is open" in detail.lower():
            cat = "circuit_breaker"
            retry = True
            backoff = 5000
            max_retries = 3
        else:
            cat = "rate_limit"
            retry = True
            backoff = 2000
            max_retries = 3
    elif 400 <= status_code < 500:
        cat = "invalid_request"
    elif status_code >= 500:
        cat = "internal_error"

    return {
        "error": cat,
        "message": detail,
        "retry_strategy": {
            "should_retry": retry,
            "backoff_ms": backoff,
            "max_retries": max_retries,
        }
    }


@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=_build_error_response(exc.status_code, detail),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_build_error_response(422, str(exc.errors()))
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
