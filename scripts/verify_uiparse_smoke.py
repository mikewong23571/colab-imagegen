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
    import os

    from fastapi.testclient import TestClient
    from PIL import Image

    import app.main as main


    def build_png() -> bytes:
        buf = io.BytesIO()
        image = Image.new("RGB", (320, 640), color=(246, 246, 246))
        image.save(buf, format="PNG")
        return buf.getvalue()


    token = os.environ["API_BEARER_TOKEN"]
    expected_mode = os.environ["EXPECTED_ENGINE_MODE"]

    with TestClient(main.app) as client:
        health_before = client.get("/healthz")
        print("health_before_status=", health_before.status_code)

        response = client.post(
            "/ui/parse",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("screen.png", build_png(), "image/png")},
        )
        print("ui_parse_status=", response.status_code)

        payload = response.json()
        if response.status_code != 200:
            print("ui_parse_error=", json.dumps(payload, ensure_ascii=False))
            raise SystemExit(10)

        engine_mode = str(payload.get("engine_mode"))
        elements = payload.get("elements") if isinstance(payload.get("elements"), list) else []
        print("ui_parse_engine_mode=", engine_mode)
        print("ui_parse_elements_count=", len(elements))

        health_after = client.get("/healthz")
        print("health_after_status=", health_after.status_code)
        health_payload = health_after.json()
        print("health_omniparser=", json.dumps(health_payload.get("omniparser", {}), ensure_ascii=False))
        print(
            "health_ui_metrics=",
            json.dumps(health_payload.get("metrics", {}).get("ui_parse_jobs", {}), ensure_ascii=False),
        )

        if engine_mode != expected_mode:
            raise SystemExit(f"expected engine_mode={expected_mode}, got={engine_mode}")
        if len(elements) == 0:
            raise SystemExit("expected non-empty ui elements")
        if expected_mode == "native" and not bool(health_payload.get("omniparser", {}).get("loaded")):
            raise SystemExit("expected omniparser loaded=true after native parse")

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
    print(f"[smoke] scenario={name} exit_code={proc.returncode}")
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
    with tempfile.TemporaryDirectory(prefix="verify-uiparse-smoke-") as tmp:
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
            name="mock",
            env_overrides={
                **base_env,
                "EXPECTED_ENGINE_MODE": "mock",
                "MOCK_UIPARSE": "1",
                "OUTPUT_DIR": str(mock_output_dir),
            },
        )
        if mock_rc != 0:
            print("verify_uiparse_smoke_failed=mock")
            return mock_rc

        repo_dir, weights_dir = _prepare_fake_omniparser(root)
        native_rc = _run_probe(
            name="native_import_smoke",
            env_overrides={
                **base_env,
                "EXPECTED_ENGINE_MODE": "native",
                "MOCK_UIPARSE": "0",
                "OMNIPARSER_DIR": str(repo_dir),
                "OMNIPARSER_WEIGHTS_DIR": str(weights_dir),
                "OUTPUT_DIR": str(native_output_dir),
            },
        )
        if native_rc != 0:
            print("verify_uiparse_smoke_failed=native_import_smoke")
            return native_rc

    print("verify_uiparse_smoke_passed=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
