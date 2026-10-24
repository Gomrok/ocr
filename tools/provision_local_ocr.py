"""Provision public OCR artifacts only; never reads or uploads user documents.

An optional pip --dry-run --report supplies resolved binary wheels. Every wheel
is checked against the authoritative PyPI digest before offline installation.
Models are pinned to official PaddlePaddle repository commits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ocr_pipeline.model_artifacts import CONFIG_HASHES, MODELS

RESERVE = 1536 * 1024 * 1024


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url, destination, expected=None):
    if destination.is_file() and (not expected or digest(destination) == expected):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    subprocess.run(
        [
            "curl.exe",
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--retry",
            "1",
            "--connect-timeout",
            "20",
            "--max-time",
            "600",
            "--output",
            str(partial),
            url,
        ],
        check=True,
    )
    if expected and digest(partial) != expected:
        raise ValueError("Downloaded artifact does not match authoritative SHA-256")
    partial.replace(destination)


def provision_models(root):
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < RESERVE + 120 * 1024 * 1024:
        raise OSError("Insufficient space; existing documents must not be removed")
    files = {}
    for name, (revision, weights_sha) in MODELS.items():
        for filename in ("inference.json", "inference.pdiparams", "inference.yml"):
            path = root / name / filename
            expected = (
                weights_sha
                if filename.endswith("pdiparams")
                else CONFIG_HASHES[f"{name}/{filename}"]
            )
            download(
                f"https://huggingface.co/PaddlePaddle/{name}/resolve/{revision}/{filename}",
                path,
                expected,
            )
            files[path.relative_to(root).as_posix()] = digest(path)
            print(f"Verified model artifact: {name}/{filename}", flush=True)
    manifest = {
        "models": list(MODELS),
        "files": files,
        "revisions": {name: values[0] for name, values in MODELS.items()},
        "license": "Apache-2.0",
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def provision_wheels(report, wheelhouse, python):
    plan = json.loads(report.read_text("utf-8"))
    wheelhouse.mkdir(parents=True, exist_ok=True)
    packages, total_unpacked = [], 0
    for item in plan["install"]:
        name, version = item["metadata"]["name"], item["metadata"]["version"]
        url = item["download_info"]["url"]
        parsed = urllib.parse.urlparse(url)
        filename = Path(urllib.parse.unquote(parsed.path)).name
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"files.pythonhosted.org", "mirrors.aliyun.com"}
            or not filename.endswith(".whl")
        ):
            raise ValueError("Only approved public binary wheel sources are allowed")
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/{urllib.parse.quote(name)}/{version}/json",
            timeout=30,
        ) as stream:
            official = json.load(stream)
        record = next(f for f in official["urls"] if f["filename"] == filename)
        expected = record["digests"]["sha256"]
        if shutil.disk_usage(wheelhouse).free < RESERVE + record["size"]:
            raise OSError("Insufficient space for verified wheel staging")
        path = wheelhouse / filename
        download(url, path, expected)
        with zipfile.ZipFile(path) as wheel:
            total_unpacked += sum(f.file_size for f in wheel.infolist())
        packages.append(f"{name}=={version} --hash=sha256:{expected}")
        print(f"Verified public wheel: {name}=={version}", flush=True)
    if shutil.disk_usage(wheelhouse).free < RESERVE + total_unpacked * 1.15:
        raise OSError(
            f"Insufficient installation space ({total_unpacked} bytes plus reserve required)"
        )
    lock = wheelhouse / "requirements.lock"
    lock.write_text("\n".join(packages) + "\n", encoding="utf-8")
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-cache-dir",
            "--require-hashes",
            "--find-links",
            str(wheelhouse),
            "-r",
            str(lock),
        ],
        check=True,
    )


def install_offline(lock, wheelhouse, python):
    """Install the committed Windows lock only from already verified local wheels."""
    normalize = lambda value: re.sub(r"[-_.]+", "_", value).lower()
    unpacked = 0
    for line in lock.read_text("utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = re.fullmatch(
            r"([\w.-]+)==([\w.+-]+) --hash=sha256:([a-f0-9]{64})", line
        )
        if not match:
            raise ValueError("Invalid offline dependency lock")
        name, version, expected = match.groups()
        matches = [
            p
            for p in wheelhouse.glob("*.whl")
            if normalize(p.name.split("-")[0]) == normalize(name)
            and p.name.split("-")[1] == version
            and digest(p) == expected
        ]
        if len(matches) != 1:
            raise ValueError(f"Missing or unverified offline wheel: {name}=={version}")
        with zipfile.ZipFile(matches[0]) as wheel:
            unpacked += sum(member.file_size for member in wheel.infolist())
    if shutil.disk_usage(python.parent).free < RESERVE + 1.15 * unpacked:
        raise OSError("Insufficient space for isolated installation and safety reserve")
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-cache-dir",
            "--require-hashes",
            "--find-links",
            str(wheelhouse),
            "-r",
            str(lock),
        ],
        check=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--lock", type=Path)
    args = parser.parse_args()
    if args.models:
        provision_models(args.models)
    if args.report:
        if not args.wheelhouse or not args.python:
            parser.error("--report requires --wheelhouse and --python")
        provision_wheels(args.report, args.wheelhouse, args.python)
    if args.lock:
        if not args.wheelhouse or not args.python:
            parser.error("--lock requires --wheelhouse and --python")
        install_offline(args.lock, args.wheelhouse, args.python)
