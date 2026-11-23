"""Declarative per-project private machine image specifications."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess

IMAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.:-]{0,127}$")
SPEC_DIRS = {".coder-images", "profiles", "images"}


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not cleaned:
        raise ValueError(f"Cannot derive valid image slug from: {value!r}")
    return cleaned


def infer_project_name(spec_path: Path) -> str:
    """Derive a stable project slug from git remote origin, git root, or parent directory."""
    parent = spec_path.resolve().parent
    try:
        origin = subprocess.run(
            ["git", "-C", str(parent), "remote", "get-url", "origin"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        if origin:
            tail = origin.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
            tail = tail.removesuffix(".git")
            if tail:
                return _slug(tail)
    except (subprocess.CalledProcessError, OSError):
        pass
    try:
        top = subprocess.run(
            ["git", "-C", str(parent), "rev-parse", "--show-toplevel"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        if top:
            return _slug(Path(top).name)
    except (subprocess.CalledProcessError, OSError):
        pass
    candidate = parent.parent.name if parent.name in SPEC_DIRS else parent.name
    return _slug(candidate)


def load_spec(path: str | Path, *, default_disk_gb: int = 50, base_image_id: str = "") -> dict:
    """Load, validate, auto-scope, and hash an image specification file."""
    spec_path = Path(path).resolve()
    raw = json.loads(spec_path.read_text())
    if raw.get("version", 1) != 1:
        raise ValueError("Image spec requires version: 1")
    allowed_keys = {"version", "project", "name", "arch", "disk_gb", "packages", "setup_script", "commands"}
    unknown = set(raw) - allowed_keys
    if unknown:
        raise ValueError("Unknown image spec keys: " + ", ".join(sorted(unknown)))

    project = _slug(raw["project"]) if raw.get("project") else infer_project_name(spec_path)
    base_name = _slug(raw["name"]) if raw.get("name") else _slug(spec_path.stem)
    scoped_name = base_name if base_name.startswith(project + "-") else f"{project}-{base_name}"
    if not IMAGE_NAME_RE.fullmatch(scoped_name):
        raise ValueError(f"Scoped image name must be 1-63 lowercase letters/digits/hyphens: {scoped_name}")

    arch = raw.get("arch", "amd64")
    if arch not in ("amd64", "arm64"):
        raise ValueError("Image arch must be 'amd64' or 'arm64'")

    disk_gb = int(raw.get("disk_gb", default_disk_gb))
    if not (30 <= disk_gb <= 100):
        raise ValueError("Image disk_gb must be between 30 and 100")

    packages = list(raw.get("packages", []))
    for pkg in packages:
        if not isinstance(pkg, str) or not PACKAGE_RE.fullmatch(pkg):
            raise ValueError(f"Invalid apt package name in image spec: {pkg!r}")

    commands = list(raw.get("commands", []))
    if any(not isinstance(cmd, str) or not cmd.strip() for cmd in commands):
        raise ValueError("Image spec commands must be non-empty strings")

    script_text = ""
    script_rel = raw.get("setup_script")
    if script_rel:
        if not isinstance(script_rel, str) or Path(script_rel).is_absolute():
            raise ValueError("setup_script must be a relative path")
        # Check every path component before resolve() so symlinks inside the tree are rejected too.
        probe = spec_path.parent
        for part in Path(script_rel).parts:
            probe = probe / part
            if probe.is_symlink():
                raise ValueError(f"setup_script must not traverse or be a symlink: {script_rel}")
        script_path = (spec_path.parent / script_rel).resolve()
        base_root = spec_path.parent.parent if spec_path.parent.name in SPEC_DIRS else spec_path.parent
        if not script_path.is_file() or not script_path.is_relative_to(base_root.resolve()):
            raise ValueError(f"setup_script must be a regular file inside {base_root}: {script_rel}")
        script_text = script_path.read_text()

    if not packages and not commands and not script_text:
        raise ValueError("Image spec must define at least one of packages, setup_script, or commands")

    canonical = {
        "version": 1,
        "name": scoped_name,
        "arch": arch,
        "disk_gb": disk_gb,
        "packages": packages,
        "commands": commands,
        "setup_script_sha256": hashlib.sha256(script_text.encode()).hexdigest() if script_text else "",
        **({"base_image_id": base_image_id} if base_image_id else {}),
    }
    image_hash = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()[:16]
    return {
        **canonical,
        "project": project,
        "spec_path": str(spec_path),
        "setup_script_text": script_text,
        "hash": image_hash,
    }
