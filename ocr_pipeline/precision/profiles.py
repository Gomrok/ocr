"""Versioned experimental profiles; confidence is not measured accuracy."""
from dataclasses import asdict, dataclass
import hashlib
import json

PROFILE_VERSION = "tesseract-regions-v1"
MODEL_COMMIT = "e12c65a915945e4c28e237a9b52bc4a8f39a0cec"
MODEL_HASHES = {
    "fas": "99e420969b5ddd2cb135b416316a7ed417c59c4faf9e0d28941348f6448114df",
    "eng": "8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba",
}
ENGINE_VERSION = "tesseract v5.5.3.20260724"
ENGINE_SHA256 = "c66f0f12ed76f6aa455dac97684bbc86756d6a732380bee09122454cfda3f420"
LANGUAGES = {"persian": "fas", "mixed": "fas+eng", "english": "eng"}


@dataclass(frozen=True)
class Options:
    profile: str = "persian"
    dpi: int = 300
    max_attempts: int = 6  # Includes the baseline observation of this region.
    max_regions: int = 32
    max_extra_calls: int = 80
    max_pixels: int = 25_000_000
    timeout_seconds: int = 90
    min_free_bytes: int = 1536 * 1024 * 1024
    max_document_bytes: int = 512 * 1024 * 1024
    max_artifact_bytes: int = 1024 * 1024 * 1024
    max_snapshot_bytes: int = 64 * 1024 * 1024
    max_document_seconds: int = 3600
    handwriting: bool = False  # Declaration only; there is no handwriting detector.
    deskew: bool = True
    contrast: bool = True
    adaptive: bool = True
    denoise: bool = False
    remove_shadow: bool = False
    trim_black_border: bool = False
    orientation_degrees: int = 0  # Explicit input; OSD is not installed/assumed.
    perspective_corners: tuple = ()  # Explicit reference-image corners, TL/TR/BR/BL.

    def __post_init__(self):
        if self.profile not in LANGUAGES or self.dpi not in (300, 400):
            raise ValueError("Unknown profile or unsupported experimental DPI")
        if not (1 <= self.max_attempts <= 6 and 1 <= self.max_regions <= 64
                and 0 <= self.max_extra_calls <= 160 and 1 <= self.timeout_seconds <= 180):
            raise ValueError("Recognition budget outside supported limits")
        if not (1_000_000 <= self.max_pixels <= 32_000_000
                and self.min_free_bytes >= 1536 * 1024 * 1024
                and 1 <= self.max_document_bytes <= 512 * 1024 * 1024
                and 1 <= self.max_artifact_bytes <= 2 * 1024 * 1024 * 1024):
            raise ValueError("Unsafe resource limits")
        if not (1024 * 1024 <= self.max_snapshot_bytes <= 64 * 1024 * 1024 and 1 <= self.max_document_seconds <= 7200):
            raise ValueError("Unsafe snapshot or document time budget")
        if self.orientation_degrees not in (0, 90, 180, 270):
            raise ValueError("Orientation must be explicitly 0/90/180/270")
        if (self.orientation_degrees or self.perspective_corners) and (self.max_attempts < 2 or self.max_extra_calls < 1):
            raise ValueError("Declared geometry requires room for the original comparison")
        if self.perspective_corners and (len(self.perspective_corners) != 4 or
                any(len(point) != 2 for point in self.perspective_corners)):
            raise ValueError("Four reference-image corners required")

    @property
    def languages(self):
        return LANGUAGES[self.profile]

    def manifest(self):
        return {"version": PROFILE_VERSION, "validation": "experimental",
                "oem": 1, "languages": self.languages, **asdict(self),
                "perspective_corners": [list(point) for point in self.perspective_corners]}

    @property
    def revision(self):
        return hashlib.sha256(json.dumps(self.manifest(), sort_keys=True).encode()).hexdigest()[:16]
