from __future__ import annotations

import asyncio
import contextlib
import io
import os
import time
import uuid
from dataclasses import dataclass
from typing import Literal
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field
from PIL import Image, ImageDraw
import torch


JobStatus = Literal["queued", "running", "succeeded", "failed"]


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


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    created_at: float
    started_at: float | None
    completed_at: float | None
    error: str | None
    width: int
    height: int
    num_inference_steps: int


@dataclass
class QueueItem:
    job_id: str
    request: GenerateRequest


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


class ServiceState:
    def __init__(self) -> None:
        self.max_queue_size = int(os.getenv("MAX_QUEUE_SIZE", "16"))
        self.max_steps = int(os.getenv("MAX_STEPS", "30"))
        self.max_width = int(os.getenv("MAX_WIDTH", "768"))
        self.max_height = int(os.getenv("MAX_HEIGHT", "768"))
        self.max_jobs = int(os.getenv("MAX_JOBS", "256"))

        self.generator = ImageGenerator()
        self.queue: asyncio.Queue[QueueItem] = asyncio.Queue(maxsize=self.max_queue_size)
        self.jobs: dict[str, JobResponse] = {}
        self.images: dict[str, bytes] = {}
        self.worker: asyncio.Task[None] | None = None

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

        job_id = str(uuid.uuid4())
        now = time.time()
        self.jobs[job_id] = JobResponse(
            job_id=job_id,
            status="queued",
            created_at=now,
            started_at=None,
            completed_at=None,
            error=None,
            width=req.width,
            height=req.height,
            num_inference_steps=req.num_inference_steps,
        )

        try:
            self.queue.put_nowait(QueueItem(job_id=job_id, request=req))
        except asyncio.QueueFull:
            self.jobs.pop(job_id, None)
            raise HTTPException(status_code=429, detail="Queue is full")

        self._evict_old_jobs()
        return GenerateAccepted(job_id=job_id, status="queued")

    def get_job(self, job_id: str) -> JobResponse:
        job = self.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    def get_image(self, job_id: str) -> bytes:
        job = self.get_job(job_id)
        if job.status == "failed":
            raise HTTPException(status_code=409, detail=f"Job failed: {job.error}")
        if job.status != "succeeded":
            raise HTTPException(status_code=409, detail="Job is not complete")

        image = self.images.get(job_id)
        if image is None:
            raise HTTPException(status_code=404, detail="Image payload is unavailable")
        return image

    def health(self) -> dict[str, object]:
        return {
            "status": "ok",
            "model_id": self.generator.model_id,
            "device": self.generator.device,
            "queue_size": self.queue.qsize(),
            "queue_capacity": self.max_queue_size,
            "max_width": self.max_width,
            "max_height": self.max_height,
            "max_steps": self.max_steps,
        }

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

    def _evict_old_jobs(self) -> None:
        if len(self.jobs) <= self.max_jobs:
            return

        removable = [
            job
            for job in self.jobs.values()
            if job.status in {"succeeded", "failed"}
        ]
        removable.sort(key=lambda item: item.completed_at or item.created_at)

        while len(self.jobs) > self.max_jobs and removable:
            victim = removable.pop(0)
            self.jobs.pop(victim.job_id, None)
            self.images.pop(victim.job_id, None)

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
                image = await asyncio.to_thread(self.generator.generate, item.request)
                self.images[item.job_id] = image
                job.status = "succeeded"
                job.completed_at = time.time()
                job.error = None
            except Exception as exc:
                job.status = "failed"
                job.completed_at = time.time()
                job.error = str(exc)
            finally:
                self.queue.task_done()


state = ServiceState()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await state.start()
    try:
        yield
    finally:
        await state.stop()


app = FastAPI(title="colab-imagegen", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict[str, object]:
    return state.health()


@app.post("/generate", response_model=GenerateAccepted, status_code=202)
async def generate(req: GenerateRequest) -> GenerateAccepted:
    return await state.submit(req)


@app.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str) -> JobResponse:
    return state.get_job(job_id)


@app.get("/jobs/{job_id}/image")
def get_image(job_id: str) -> Response:
    image = state.get_image(job_id)
    return Response(content=image, media_type="image/png")
