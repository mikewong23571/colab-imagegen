#!/usr/bin/env python3
"""
Bootstrap executor for colab-run.yaml.

Usage:
  python skills/colab-cli/scripts/colab_bootstrap.py --config colab-run.yaml
  python skills/colab-cli/scripts/colab_bootstrap.py --config colab-run.yaml --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


class ConfigError(Exception):
    """Raised when colab-run.yaml is invalid."""


def _require_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"Field '{field}' must be a mapping")
    return value


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Field '{field}' must be a non-empty string")
    return value


def _optional_str(value: Any, field: str, default: str) -> str:
    if value is None:
        return default
    return _require_str(value, field)


def _optional_cmd_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"Field '{field}' must be a list of strings")
    commands: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"Field '{field}[{idx}]' must be a non-empty string")
        commands.append(item)
    return commands


def _optional_env_map(value: Any, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"Field '{field}' must be a mapping of string to string")
    out: dict[str, str] = {}
    for key, val in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigError(f"Field '{field}' has an invalid env key")
        if not isinstance(val, (str, int, float, bool)):
            raise ConfigError(
                f"Field '{field}.{key}' must be string/number/bool (convertible to string)"
            )
        out[key] = str(val)
    return out


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise ConfigError(
            "PyYAML is required. Install with: pip install pyyaml"
        ) from exc

    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # broad by design: YAML parser errors vary by version
        raise ConfigError(f"Failed to parse YAML: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ConfigError("Top-level YAML must be a mapping")
    return parsed


def load_config(path: Path) -> dict[str, Any]:
    raw = load_yaml(path)

    version = raw.get("schema_version")
    if version != 1:
        raise ConfigError("Field 'schema_version' must be 1")

    repo = _require_dict(raw.get("repo"), "repo")
    runtime = _require_dict(raw.get("runtime", {}), "runtime")
    steps = _require_dict(raw.get("steps"), "steps")

    repo_url = _require_str(repo.get("url"), "repo.url")
    repo_ref = _optional_str(repo.get("ref"), "repo.ref", "main")
    repo_local_dir = _optional_str(
        repo.get("local_dir"), "repo.local_dir", "/content/workspace/repo"
    )

    workdir = _optional_str(runtime.get("workdir"), "runtime.workdir", ".")
    env_vars = _optional_env_map(runtime.get("env"), "runtime.env")

    install_steps = _optional_cmd_list(steps.get("install"), "steps.install")
    run_steps = _optional_cmd_list(steps.get("run"), "steps.run")
    if not run_steps:
        raise ConfigError("Field 'steps.run' must contain at least one command")

    return {
        "schema_version": 1,
        "repo": {
            "url": repo_url,
            "ref": repo_ref,
            "local_dir": repo_local_dir,
        },
        "runtime": {
            "workdir": workdir,
            "env": env_vars,
        },
        "steps": {
            "install": install_steps,
            "run": run_steps,
        },
    }


def run_shell(command: str, cwd: Path, env: dict[str, str], dry_run: bool) -> None:
    print(f"$ {command}")
    print(f"  cwd: {cwd}")
    if dry_run:
        return
    subprocess.run(command, shell=True, check=True, cwd=str(cwd), env=env)


def prepare_repo(repo_url: str, repo_ref: str, repo_dir: Path, dry_run: bool) -> None:
    git_dir = repo_dir / ".git"
    if git_dir.exists():
        run_shell("git fetch --all --tags", cwd=repo_dir, env=os.environ.copy(), dry_run=dry_run)
    else:
        if not dry_run:
            repo_dir.parent.mkdir(parents=True, exist_ok=True)
        clone_cmd = f"git clone {repo_url} {repo_dir}"
        run_shell(clone_cmd, cwd=repo_dir.parent, env=os.environ.copy(), dry_run=dry_run)

    run_shell(f"git checkout {repo_ref}", cwd=repo_dir, env=os.environ.copy(), dry_run=dry_run)


def get_head_sha(repo_dir: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute colab-run.yaml bootstrap flow")
    parser.add_argument("--config", required=True, help="Path to colab-run.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print normalized config as JSON before execution",
    )
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        return 1

    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(f"Invalid config: {exc}", file=sys.stderr)
        return 2

    repo_cfg = cfg["repo"]
    runtime_cfg = cfg["runtime"]
    steps_cfg = cfg["steps"]

    repo_dir = Path(repo_cfg["local_dir"]).expanduser()
    if not repo_dir.is_absolute():
        repo_dir = (Path.cwd() / repo_dir).resolve()

    workdir = (repo_dir / runtime_cfg["workdir"]).resolve()

    exec_env = os.environ.copy()
    exec_env.update(runtime_cfg["env"])

    if args.print_config:
        print(json.dumps(cfg, indent=2, ensure_ascii=True))

    print("==> Preparing repository")
    prepare_repo(repo_cfg["url"], repo_cfg["ref"], repo_dir, args.dry_run)

    if not args.dry_run and not workdir.exists():
        print(f"Workdir does not exist: {workdir}", file=sys.stderr)
        return 3

    print("==> Running install steps")
    for command in steps_cfg["install"]:
        run_shell(command, cwd=workdir, env=exec_env, dry_run=args.dry_run)

    print("==> Running job steps")
    for command in steps_cfg["run"]:
        run_shell(command, cwd=workdir, env=exec_env, dry_run=args.dry_run)

    if not args.dry_run:
        head_sha = get_head_sha(repo_dir)
        print(f"HEAD: {head_sha}")
    print("Done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
