"""Non-secret OCR runtime identity shared by health and release checks."""
from __future__ import annotations

import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import subprocess
from functools import lru_cache

import psutil

from .storage import file_sha256
from .numeric_recheck import MODEL_DIR, model_identity

MODELS = ("Arabic.traineddata", "eng.traineddata", "fas.traineddata")
PROFILE_KEYS = {
    "OCR_PIPELINE_MODE": "legacy",
    "OCR_PERSISTENT_ENGINE": "1",
    "OCR_WORKERS": "2",
    "OCR_RAM_BUDGET_MIB": "4096",
    "OCR_DEFAULT_DPI": "2000",
    MODEL_DIR: "",
}


def profile(settings):
    return {key: str(settings.get(key, default)) for key, default in PROFILE_KEYS.items()}


def resource_limits(settings):
    config = profile(settings)
    cpu = os.cpu_count() or 1
    workers = max(1, min(2, int(config["OCR_WORKERS"]), cpu))
    ram_mib = min(int(config["OCR_RAM_BUDGET_MIB"]),
                  psutil.virtual_memory().total // (2 * 2**20))
    if ram_mib < 768 * workers:
        workers = 1
    if ram_mib < 768:
        raise ValueError("Insufficient configured RAM for isolated OCR")
    return {"workers": workers, "ram_mib": ram_mib,
            "native_workers": min(4, max(1, cpu // workers)), "omp_threads": 1}


def digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@lru_cache(maxsize=4)
def engine_identity(executable: Path, tessdata: Path):
    if not executable.is_file():
        raise ValueError("Native OCR executable is missing")
    data = {
        "sha256": file_sha256(executable),
        "version": subprocess.run([str(executable), "--version"], check=True, capture_output=True,
                                  text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                  ).stdout.splitlines()[0].strip(),
        "libraries": {path.name: file_sha256(path) for path in sorted(executable.parent.glob("*.dll"))},
        "models": {name: file_sha256(tessdata / name) for name in MODELS},
    }
    return data


def runtime_identity(settings, executable, tessdata):
    vars = profile(settings)
    packages = ["Flask", "numpy", "opencv-python-headless", "psutil", "pymupdf", "waitress"]
    if vars["OCR_PIPELINE_MODE"] != "legacy":
        packages.append("Pillow")
    identity = {
        "settings": vars,
        "limits": resource_limits(settings),
        "os": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "packages": {name: version(name) for name in packages},
        "engine": engine_identity(executable, tessdata),
    }
    if vars[MODEL_DIR]:
        identity["numeric_recheck"] = model_identity(vars[MODEL_DIR])
    return identity


def current_runtime(engine):
    return runtime_identity(os.environ, Path(engine.executable), engine.tessdata_dir)
