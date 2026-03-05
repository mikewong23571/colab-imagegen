#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error, request

from PIL import Image, ImageDraw


def _http_get_json(url: str, timeout_sec: int) -> dict[str, Any]:
    req = request.Request(url=url, method="GET")
    with request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def _http_post_multipart_json(
    url: str,
    token: str,
    file_field: str,
    file_path: Path,
    timeout_sec: int,
) -> dict[str, Any]:
    boundary = f"----colab-imagegen-{uuid.uuid4().hex}"
    filename = file_path.name
    file_bytes = file_path.read_bytes()
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    body = bytearray()
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode("utf-8")
    )
    body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
    body.extend(file_bytes)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    req = request.Request(url=url, method="POST", data=bytes(body))
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Accept", "application/json")
    with request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def _create_default_image(path: Path) -> None:
    image = Image.new("RGB", (1080, 1920), color=(240, 240, 240))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1080, 180), fill=(33, 37, 41))
    draw.text((36, 70), "Settings", fill=(255, 255, 255))
    draw.rectangle((40, 260, 1040, 420), fill=(255, 255, 255))
    draw.text((72, 318), "Wi-Fi", fill=(30, 30, 30))
    draw.rectangle((40, 460, 1040, 620), fill=(255, 255, 255))
    draw.text((72, 518), "Bluetooth", fill=(30, 30, 30))
    draw.rectangle((40, 660, 1040, 820), fill=(255, 255, 255))
    draw.text((72, 718), "Notifications", fill=(30, 30, 30))
    image.save(path, format="PNG")


def _wait_healthz(healthz_url: str, timeout_sec: int) -> None:
    deadline = time.time() + timeout_sec
    while True:
        try:
            payload = _http_get_json(healthz_url, timeout_sec=min(timeout_sec, 20))
            if isinstance(payload, dict) and payload.get("status") == "ok":
                return
        except Exception:
            pass
        if time.time() >= deadline:
            raise SystemExit(f"healthz not ready within {timeout_sec}s: {healthz_url}")
        time.sleep(2)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure /ui/parse cold-start behavior across multiple samples."
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Service base URL, e.g. http://127.0.0.1:8000",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("API_BEARER_TOKEN", ""),
        help="Bearer token (default: API_BEARER_TOKEN env)",
    )
    parser.add_argument(
        "--image",
        default="",
        help="Optional path to an image file. If omitted, a synthetic UI image is generated.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="Number of measurement samples (default: 3).",
    )
    parser.add_argument(
        "--expect-engine-mode",
        default="native",
        help="Expected engine_mode in /ui/parse response (default: native).",
    )
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=180,
        help="HTTP timeout seconds for each request (default: 180).",
    )
    parser.add_argument(
        "--restart-cmd",
        default="",
        help="Optional command to restart service before each sample, e.g. 'bash scripts/ops.sh restart'.",
    )
    parser.add_argument(
        "--restart-wait-timeout-sec",
        type=int,
        default=300,
        help="Wait timeout after restart command (default: 300).",
    )
    parser.add_argument(
        "--pause-sec",
        type=float,
        default=0.0,
        help="Pause seconds between samples (default: 0).",
    )
    return parser.parse_args()


def _run_restart_command(command: str) -> None:
    print(f"restart.cmd={command}")
    result = subprocess.run(command, shell=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"restart command failed with exit code {result.returncode}")


def main() -> int:
    args = _parse_args()
    if not args.token:
        raise SystemExit("missing token: pass --token or set API_BEARER_TOKEN")
    if args.runs < 1:
        raise SystemExit("--runs must be >= 1")

    base = args.base_url.rstrip("/")
    healthz_url = f"{base}/healthz"
    parse_url = f"{base}/ui/parse"

    image_path: Path | None = None
    tmp_file: tempfile.NamedTemporaryFile[str] | None = None
    if args.image:
        image_path = Path(args.image).expanduser().resolve()
        if not image_path.exists():
            raise SystemExit(f"image file not found: {image_path}")
    else:
        tmp_file = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_file.close()
        image_path = Path(tmp_file.name)
        _create_default_image(image_path)

    wall_elapsed_values: list[int] = []
    server_elapsed_values: list[int] = []
    try:
        for index in range(args.runs):
            sample_no = index + 1
            if args.restart_cmd:
                _run_restart_command(args.restart_cmd)
                _wait_healthz(healthz_url, timeout_sec=args.restart_wait_timeout_sec)

            health_before = _http_get_json(healthz_url, timeout_sec=args.timeout_sec)
            omniparser_before = health_before.get("omniparser", {})

            start = time.perf_counter()
            try:
                ui_result = _http_post_multipart_json(
                    url=parse_url,
                    token=args.token,
                    file_field="file",
                    file_path=image_path,
                    timeout_sec=args.timeout_sec,
                )
            except error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                print(f"sample.{sample_no}.http_error={exc.code}")
                print(f"sample.{sample_no}.http_body={detail}")
                return 2
            wall_elapsed_ms = int((time.perf_counter() - start) * 1000)

            engine_mode = str(ui_result.get("engine_mode"))
            elements = ui_result.get("elements") if isinstance(ui_result.get("elements"), list) else []
            server_elapsed_ms = int(ui_result.get("elapsed_ms", 0))
            health_after = _http_get_json(healthz_url, timeout_sec=args.timeout_sec)
            omniparser_after = health_after.get("omniparser", {})

            print(f"sample.{sample_no}.engine_mode={engine_mode}")
            print(f"sample.{sample_no}.elements_count={len(elements)}")
            print(f"sample.{sample_no}.server_elapsed_ms={server_elapsed_ms}")
            print(f"sample.{sample_no}.wall_elapsed_ms={wall_elapsed_ms}")
            print(
                f"sample.{sample_no}.load_attempted_before={omniparser_before.get('load_attempted')}"
            )
            print(
                f"sample.{sample_no}.load_attempted_after={omniparser_after.get('load_attempted')}"
            )
            print(
                f"sample.{sample_no}.last_load_duration_ms_after={omniparser_after.get('last_load_duration_ms')}"
            )

            if engine_mode != args.expect_engine_mode:
                print(
                    f"measure_failed: expected engine_mode={args.expect_engine_mode}, got={engine_mode}",
                )
                return 3
            if len(elements) == 0:
                print("measure_failed: no elements returned")
                return 4

            wall_elapsed_values.append(wall_elapsed_ms)
            server_elapsed_values.append(server_elapsed_ms)

            if args.pause_sec > 0 and sample_no < args.runs:
                time.sleep(args.pause_sec)
    finally:
        if tmp_file is not None and image_path is not None:
            image_path.unlink(missing_ok=True)

    wall_avg = int(sum(wall_elapsed_values) / len(wall_elapsed_values))
    server_avg = int(sum(server_elapsed_values) / len(server_elapsed_values))
    print(f"summary.samples={args.runs}")
    print(f"summary.wall_elapsed_ms.min={min(wall_elapsed_values)}")
    print(f"summary.wall_elapsed_ms.max={max(wall_elapsed_values)}")
    print(f"summary.wall_elapsed_ms.avg={wall_avg}")
    print(f"summary.server_elapsed_ms.min={min(server_elapsed_values)}")
    print(f"summary.server_elapsed_ms.max={max(server_elapsed_values)}")
    print(f"summary.server_elapsed_ms.avg={server_avg}")
    if len(server_elapsed_values) >= 2:
        rest_avg = int(sum(server_elapsed_values[1:]) / len(server_elapsed_values[1:]))
        print(f"summary.server_elapsed_ms.first={server_elapsed_values[0]}")
        print(f"summary.server_elapsed_ms.rest_avg={rest_avg}")
        print(f"summary.server_elapsed_ms.first_minus_rest_avg={server_elapsed_values[0] - rest_avg}")

    print("measure_passed=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
