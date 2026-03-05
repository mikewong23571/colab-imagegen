#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path


PROBE_CODE = textwrap.dedent(
    """
    import io
    import json
    import math
    import os
    import struct
    import time
    import wave

    from fastapi.testclient import TestClient
    from PIL import Image

    import app.main as main


    def build_png() -> bytes:
        buf = io.BytesIO()
        image = Image.new("RGB", (512, 768), color=(245, 245, 245))
        image.save(buf, format="PNG")
        return buf.getvalue()


    def build_wav() -> bytes:
        sample_rate = 16000
        duration_sec = 1.0
        freq = 440.0
        frames = bytearray()
        total_samples = int(sample_rate * duration_sec)
        for idx in range(total_samples):
            value = int(12000 * math.sin(2.0 * math.pi * freq * idx / sample_rate))
            frames.extend(struct.pack("<h", value))

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(bytes(frames))
        return buf.getvalue()


    token = os.environ["API_BEARER_TOKEN"]
    expected_ui_mode = os.environ["EXPECT_UI_ENGINE_MODE"]
    auth_header = {"Authorization": f"Bearer {token}"}

    with TestClient(main.app) as client:
        health_before = client.get("/healthz")
        print("health_before_status=", health_before.status_code)

        # image_gen basic regression
        gen_resp = client.post(
            "/generate",
            headers=auth_header,
            json={
                "prompt": "test prompt",
                "width": 512,
                "height": 512,
                "num_inference_steps": 4,
                "guidance_scale": 7.0,
            },
        )
        print("generate_status=", gen_resp.status_code)
        if gen_resp.status_code != 202:
            print("generate_error=", gen_resp.text)
            raise SystemExit(10)
        job_id = gen_resp.json().get("job_id")
        if not job_id:
            raise SystemExit("missing job_id in generate response")

        image_job = None
        for _ in range(100):
            job_resp = client.get(f"/jobs/{job_id}", headers=auth_header)
            if job_resp.status_code != 200:
                raise SystemExit(f"job query failed: {job_resp.status_code}")
            image_job = job_resp.json()
            if image_job.get("status") in {"succeeded", "failed"}:
                break
            time.sleep(0.05)

        if image_job is None:
            raise SystemExit("image job missing")
        print("generate_job_status=", image_job.get("status"))
        print("generate_job_task_type=", image_job.get("task_type"))
        if image_job.get("status") != "succeeded":
            raise SystemExit(f"image job not succeeded: {json.dumps(image_job, ensure_ascii=False)}")
        if image_job.get("task_type") != "image_gen":
            raise SystemExit(f"unexpected task_type: {image_job.get('task_type')}")

        image_resp = client.get(f"/jobs/{job_id}/image", headers=auth_header)
        print("generate_image_status=", image_resp.status_code)
        if image_resp.status_code != 200:
            raise SystemExit("image download failed")
        if len(image_resp.content) == 0:
            raise SystemExit("empty generated image")

        # asr basic regression
        asr_resp = client.post(
            "/asr/whisper/transcribe",
            headers=auth_header,
            files={"file": ("sample.wav", build_wav(), "audio/wav")},
        )
        print("asr_status=", asr_resp.status_code)
        if asr_resp.status_code != 200:
            print("asr_error=", asr_resp.text)
            raise SystemExit(20)
        asr_payload = asr_resp.json()
        print("asr_text_len=", len(asr_payload.get("text", "")))
        print("asr_segments_count=", len(asr_payload.get("segments", [])))
        if not asr_payload.get("text"):
            raise SystemExit("empty asr text")
        if not asr_payload.get("segments"):
            raise SystemExit("empty asr segments")

        # ui_parse basic regression
        ui_resp = client.post(
            "/ui/parse",
            headers=auth_header,
            files={"file": ("screen.png", build_png(), "image/png")},
        )
        print("ui_parse_status=", ui_resp.status_code)
        if ui_resp.status_code != 200:
            print("ui_parse_error=", ui_resp.text)
            raise SystemExit(30)
        ui_payload = ui_resp.json()
        print("ui_parse_engine_mode=", ui_payload.get("engine_mode"))
        print("ui_parse_elements_count=", len(ui_payload.get("elements", [])))
        if ui_payload.get("engine_mode") != expected_ui_mode:
            raise SystemExit(
                f"unexpected ui engine mode: expected={expected_ui_mode}, got={ui_payload.get('engine_mode')}"
            )
        if not ui_payload.get("elements"):
            raise SystemExit("empty ui parse elements")

        health_after = client.get("/healthz")
        print("health_after_status=", health_after.status_code)
        metrics = health_after.json().get("metrics", {})
        print("health_image_jobs=", json.dumps(metrics.get("image_jobs", {}), ensure_ascii=False))
        print("health_asr_jobs=", json.dumps(metrics.get("asr_jobs", {}), ensure_ascii=False))
        print("health_ui_parse_jobs=", json.dumps(metrics.get("ui_parse_jobs", {}), ensure_ascii=False))

    print("probe_passed=1")
    """
)


def _run_probe(name: str, env_overrides: dict[str, str]) -> int:
    env = os.environ.copy()
    env.update(env_overrides)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", PROBE_CODE],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    print(f"[regression] scenario={name} exit_code={proc.returncode}")
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.stderr:
        print(proc.stderr.rstrip(), file=sys.stderr)
    return proc.returncode


def _prepare_fake_omniparser(root: Path) -> tuple[Path, Path]:
    repo_dir = root / "fake_omniparser_repo"
    util_dir = repo_dir / "util"
    util_dir.mkdir(parents=True, exist_ok=True)
    (util_dir / "__init__.py").write_text("", encoding="utf-8")
    (util_dir / "omniparser.py").write_text(
        textwrap.dedent(
            """
            from __future__ import annotations


            class Omniparser:
                def __init__(self, config: dict) -> None:
                    self.config = config

                def parse(self, _: str):
                    return None, [
                        {
                            "type": "text",
                            "bbox": [0.10, 0.08, 0.62, 0.16],
                            "interactivity": False,
                            "content": "Settings",
                            "confidence": 0.95,
                        },
                        {
                            "type": "icon",
                            "bbox": [0.70, 0.08, 0.90, 0.16],
                            "interactivity": True,
                            "content": "Search",
                            "confidence": 0.88,
                        },
                    ]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    weights_dir = root / "fake_omniparser_weights"
    icon_detect_dir = weights_dir / "icon_detect"
    icon_caption_dir = weights_dir / "icon_caption_florence"
    icon_detect_dir.mkdir(parents=True, exist_ok=True)
    icon_caption_dir.mkdir(parents=True, exist_ok=True)
    (icon_detect_dir / "model.pt").write_bytes(b"fake-model")
    (icon_caption_dir / "model.safetensors").write_bytes(b"fake-caption-model")
    return repo_dir, weights_dir


def main() -> int:
    include_native_import_smoke = os.getenv("VERIFY_REGRESSION_NATIVE_IMPORT_SMOKE", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    with tempfile.TemporaryDirectory(prefix="verify-runtime-regression-") as tmp:
        root = Path(tmp)
        mock_output_dir = root / "mock_outputs"
        native_output_dir = root / "native_outputs"
        mock_output_dir.mkdir(parents=True, exist_ok=True)
        native_output_dir.mkdir(parents=True, exist_ok=True)

        base_env = {
            "API_BEARER_TOKEN": "dev-token",
            "MOCK_IMAGEGEN": "1",
            "MOCK_ASR": "1",
            "OMNIPARSER_ENABLED": "1",
        }

        mock_rc = _run_probe(
            name="mock_all",
            env_overrides={
                **base_env,
                "EXPECT_UI_ENGINE_MODE": "mock",
                "MOCK_UIPARSE": "1",
                "OUTPUT_DIR": str(mock_output_dir),
            },
        )
        if mock_rc != 0:
            print("verify_runtime_regression_failed=mock_all")
            return mock_rc

        if include_native_import_smoke:
            repo_dir, weights_dir = _prepare_fake_omniparser(root)
            native_rc = _run_probe(
                name="native_import_smoke",
                env_overrides={
                    **base_env,
                    "EXPECT_UI_ENGINE_MODE": "native",
                    "MOCK_UIPARSE": "0",
                    "OMNIPARSER_DIR": str(repo_dir),
                    "OMNIPARSER_WEIGHTS_DIR": str(weights_dir),
                    "OUTPUT_DIR": str(native_output_dir),
                },
            )
            if native_rc != 0:
                print("verify_runtime_regression_failed=native_import_smoke")
                return native_rc

    print("verify_runtime_regression_passed=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
