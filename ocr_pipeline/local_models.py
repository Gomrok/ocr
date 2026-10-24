"""Pinned installed Tesseract artifacts. No network or dynamic trust enrollment."""

import hashlib
import re
from pathlib import Path

MODEL_SHA256 = {
    "fas": "99e420969b5ddd2cb135b416316a7ed417c59c4faf9e0d28941348f6448114df",
    "eng": "8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba",
    "Arabic": "a80454f72df35c6f52550551dd69b8521ea6a62d834396d1c7dce4c35493902e",
}


def verify_models(directory: Path, languages: str) -> dict:
    names = languages.split("+")
    if not names or any(
        not re.fullmatch("[A-Za-z_]+", name) or name not in MODEL_SHA256
        for name in names
    ):
        raise RuntimeError("UNAPPROVED_LOCAL_MODEL")
    verified = {}
    for name in names:
        path = directory / (name + ".traineddata")
        if not path.is_file():
            raise RuntimeError("LOCAL_MODEL_MISSING")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != MODEL_SHA256[name]:
            raise RuntimeError("LOCAL_MODEL_HASH_MISMATCH")
        verified[name] = digest
    return verified
