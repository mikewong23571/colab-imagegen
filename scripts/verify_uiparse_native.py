#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import tempfile
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify /ui/parse in native mode and print evidence for progress tracking."
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
        "--expect-engine-mode",
        default="native",
        help="Expected engine_mode in /ui/parse response (default: native).",
    )
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=180,
        help="HTTP timeout seconds (default: 180).",
    )
    parser.add_argument(
        "--dump-json",
        action="store_true",
        help="Print full JSON responses for healthz and ui/parse.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.token:
        raise SystemExit("missing token: pass --token or set API_BEARER_TOKEN")

    base = args.base_url.rstrip("/")
    healthz_url = f"{base}/healthz"
    parse_url = f"{base}/ui/parse"

    health = _http_get_json(healthz_url, timeout_sec=args.timeout_sec)
    omniparser = health.get("omniparser", {})
    print("health.omniparser.enabled=", omniparser.get("enabled"))
    print("health.omniparser.ready=", omniparser.get("ready"))
    print("health.omniparser.engine_mode=", omniparser.get("engine_mode"))
    print("health.omniparser.reason=", omniparser.get("reason"))

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
        print("ui_parse_http_error=", exc.code)
        print("ui_parse_http_body=", detail)
        return 2
    finally:
        if tmp_file is not None and image_path is not None:
            image_path.unlink(missing_ok=True)

    engine_mode = str(ui_result.get("engine_mode"))
    elements = ui_result.get("elements") if isinstance(ui_result.get("elements"), list) else []
    elapsed_ms = ui_result.get("elapsed_ms")

    print("ui_parse.engine_mode=", engine_mode)
    print("ui_parse.elements_count=", len(elements))
    print("ui_parse.elapsed_ms=", elapsed_ms)
    print("ui_parse.parse_id=", ui_result.get("parse_id"))

    health_after = _http_get_json(healthz_url, timeout_sec=args.timeout_sec)
    ui_metrics = health_after.get("metrics", {}).get("ui_parse_jobs", {})
    if isinstance(ui_metrics, dict) and ui_metrics:
        print("health.metrics.ui_parse_jobs=", json.dumps(ui_metrics, ensure_ascii=False))

    if args.dump_json:
        print("health_before.json=", json.dumps(health, ensure_ascii=False))
        print("health_after.json=", json.dumps(health_after, ensure_ascii=False))
        print("ui_parse.json=", json.dumps(ui_result, ensure_ascii=False))

    if engine_mode != args.expect_engine_mode:
        print(
            f"verify_failed: expected engine_mode={args.expect_engine_mode}, got={engine_mode}",
        )
        return 3

    if len(elements) == 0:
        print("verify_failed: no elements returned")
        return 4

    print("verify_passed=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
