"""Post-deployment HTTPS smoke using a freshly generated, non-private PDF."""
import argparse
from http.cookiejar import CookieJar
import json
import re
import secrets
import sys
import time
from urllib.request import HTTPCookieProcessor, Request, build_opener

import pymupdf

URL = "https://ocr.gomrok.org"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--mode", required=True)
    args = parser.parse_args()
    opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def get(path, data=None, headers=None):
        with opener.open(Request(URL + path, data=data, headers=headers or {}), timeout=15) as response:
            if response.url != URL + path:
                raise RuntimeError("Unexpected production redirect")
            return response.read()

    health = json.loads(get("/healthz"))
    if (health["status"], health["release"], health["pipeline_mode"], health["runtime_fingerprint"]) != (
            "ok", args.commit, args.mode, args.fingerprint):
        raise RuntimeError("Production identity does not match the tested release")
    csrf = re.search(rb'name="csrf-token" content="([^"]+)"', get("/"))[1].decode("ascii")
    digits = "0046" + str(secrets.randbelow(9000) + 1000)
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=300, height=110)
        page.insert_text((25, 55), "OCR TEST " + digits, fontsize=18)
        source = pdf.tobytes()
    boundary = secrets.token_hex(16)
    data = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"pdfs\"; filename=\"release-smoke.pdf\"\r\n"
        "Content-Type: application/pdf\r\n\r\n"
    ).encode("ascii") + source + f"\r\n--{boundary}--\r\n".encode("ascii")
    job = json.loads(get("/upload", data, {"X-CSRF-Token": csrf,
                                         "Content-Type": f"multipart/form-data; boundary={boundary}"}))
    job_id = job["job_id"]
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        status = json.loads(get(f"/status/{job_id}"))
        if status["state"] not in ("queued", "running"):
            break
        time.sleep(1.0)
    else:
        raise TimeoutError("Generated document did not finish")
    if status["state"] != "done" or status["pages_done"] != 1 or status["failed_pages"]:
        raise RuntimeError("Generated document was not fully processed")
    name = next(name for name in status["files"] if name.endswith(".txt"))
    content = get(f"/download/{job_id}/{name}").decode("utf-8").translate(str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"
    ))
    if not re.search(rf"\bOCR\s+TEST\s+{digits}\b", content) or len(re.findall(r"^={20} PAGE ", content, re.M)) != 1:
        raise RuntimeError("Generated text or number is not exact")
    print("Public HTTPS identity, upload, processing, and download passed")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, StopIteration, TypeError) as exc:
        print(f"Public smoke failed: {exc}", file=sys.stderr)
        sys.exit(1)
