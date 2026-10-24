"""Document-scoped, isolated Tesseract workers using the installed C API.

Models are reused, not recognition state. The CLI remains the compatibility
fallback. No image or customer text is retained between documents.
"""
from __future__ import annotations

import ctypes as C
import multiprocessing as MP
import os
import re
import struct
import threading
import time
from pathlib import Path


class NativeAPI:
    """One worker's API handles; never shared by threads or processes."""

    def __init__(self, directory: str, tessdata: str):
        self.directory_handle = os.add_dll_directory(directory)
        self.tess = C.CDLL(str(Path(directory) / 'libtesseract-5.dll'))
        self.lept = C.CDLL(str(Path(directory) / 'libleptonica-6.dll'))
        self.tessdata = tessdata
        self.handles: dict[str, int] = {}
        signatures = {
            'TessBaseAPICreate': (C.c_void_p, []),
            'TessBaseAPIInit3': (C.c_int, [C.c_void_p, C.c_char_p, C.c_char_p]),
            'TessBaseAPISetVariable': (C.c_int, [C.c_void_p, C.c_char_p, C.c_char_p]),
            'TessBaseAPISetPageSegMode': (None, [C.c_void_p, C.c_int]),
            'TessBaseAPIClearAdaptiveClassifier': (None, [C.c_void_p]),
            'TessBaseAPISetImage2': (None, [C.c_void_p, C.c_void_p]),
            'TessBaseAPIRecognize': (C.c_int, [C.c_void_p, C.c_void_p]),
            'TessBaseAPIGetUTF8Text': (C.c_void_p, [C.c_void_p]),
            'TessBaseAPIGetHOCRText': (C.c_void_p, [C.c_void_p, C.c_int]),
            'TessBaseAPIClear': (None, [C.c_void_p]),
            'TessBaseAPIDelete': (None, [C.c_void_p]),
            'TessDeleteText': (None, [C.c_void_p]),
            'TessMonitorCreate': (C.c_void_p, []),
            'TessMonitorDelete': (None, [C.c_void_p]),
            'TessMonitorSetDeadlineMSecs': (None, [C.c_void_p, C.c_int]),
        }
        for name, (result, arguments) in signatures.items():
            function = getattr(self.tess, name)
            function.restype, function.argtypes = result, arguments
        self.lept.pixReadMem.argtypes = [C.c_void_p, C.c_size_t]
        self.lept.pixReadMem.restype = C.c_void_p
        self.lept.pixDestroy.argtypes = [C.POINTER(C.c_void_p)]
        self.lept.pixDestroy.restype = None

    def _handle(self, language: str):
        if language not in self.handles:
            if len(self.handles) >= 3:
                raise ValueError('Native OCR language capacity reached')
            handle = self.tess.TessBaseAPICreate()
            if not handle:
                raise RuntimeError('Native OCR allocation failed')
            if self.tess.TessBaseAPIInit3(handle, self.tessdata.encode('utf-8'), language.encode('ascii')):
                self.tess.TessBaseAPIDelete(handle)
                raise RuntimeError('Native OCR model initialization failed')
            self.handles[language] = handle
            for key, value in ((b'tessedit_do_invert', b'0'), (b'debug_file', b'NUL')):
                if not self.tess.TessBaseAPISetVariable(handle, key, value):
                    raise RuntimeError('Native OCR configuration failed')
        return self.handles[language]

    def _text(self, pointer) -> str:
        if not pointer:
            raise RuntimeError('Native OCR returned no result')
        try:
            return C.string_at(pointer).decode('utf-8')
        finally:
            self.tess.TessDeleteText(pointer)

    def recognize(self, png: bytes, psm: int, whitelist: str | None, language: str) -> tuple[str, str]:
        handle = self._handle(language)
        # Matching a new CLI process requires resetting both learned state and
        # per-call variables, including a previous digit-only whitelist.
        self.tess.TessBaseAPIClearAdaptiveClassifier(handle)
        self.tess.TessBaseAPISetPageSegMode(handle, psm)
        if not self.tess.TessBaseAPISetVariable(handle, b'tessedit_char_whitelist',
                                               (whitelist or '').encode('utf-8')):
            raise RuntimeError('Native OCR whitelist configuration failed')
        data = C.create_string_buffer(png)
        pix = C.c_void_p(self.lept.pixReadMem(data, len(png)))
        if not pix:
            raise ValueError('Native OCR could not decode image')
        monitor = self.tess.TessMonitorCreate()
        try:
            if not monitor:
                raise RuntimeError('Native OCR monitor allocation failed')
            self.tess.TessMonitorSetDeadlineMSecs(monitor, 60000)
            self.tess.TessBaseAPISetImage2(handle, pix)
            if self.tess.TessBaseAPIRecognize(handle, monitor):
                raise RuntimeError('Native OCR recognition failed or timed out')
            return (self._text(self.tess.TessBaseAPIGetUTF8Text(handle)),
                    self._text(self.tess.TessBaseAPIGetHOCRText(handle, 0)))
        finally:
            self.tess.TessBaseAPIClear(handle)
            if monitor:
                self.tess.TessMonitorDelete(monitor)
            self.lept.pixDestroy(C.byref(pix))

    def close(self):
        for handle in self.handles.values():
            self.tess.TessBaseAPIDelete(handle)
        self.handles.clear()
        self.directory_handle.close()


_worker_api: NativeAPI | None = None


def _recognize(directory: str, tessdata: str, png: bytes, psm: int,
               whitelist: str | None, language: str) -> tuple[str, str]:
    global _worker_api
    if _worker_api is None:
        _worker_api = NativeAPI(directory, tessdata)
    return _worker_api.recognize(png, psm, whitelist, language)


class NativePool:
    """At most four child processes, owned and destroyed by one document."""

    def __init__(self, directory: Path, tessdata: Path):
        self.directory, self.tessdata = str(directory), str(tessdata)
        self.workers = min(4, max(1, (os.cpu_count() or 1) // 2))
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(self.workers)
        self.pool = MP.get_context('spawn').Pool(processes=self.workers)

    @staticmethod
    def supported(executable: str | None) -> bool:
        if os.name != 'nt' or not executable:
            return False
        directory = Path(executable).resolve().parent
        return all((directory / name).is_file() for name in
                   ('libtesseract-5.dll', 'libleptonica-6.dll'))

    def recognize(self, png: bytes, psm: int, whitelist: str | None, language: str, *, seconds=90) -> tuple[str, str]:
        if (len(png) < 24 or len(png) > 64 * 1024 * 1024
                or png[:8] != b'\x89PNG\r\n\x1a\n' or png[12:16] != b'IHDR'):
            raise ValueError('Invalid native OCR image')
        width, height = struct.unpack('>II', png[16:24])
        if not width or not height or width * height > 20_000_000:
            raise ValueError('Native OCR image dimensions exceed limit')
        if (not 0 <= psm <= 13 or not re.fullmatch(r'[a-zA-Z0-9_+]{1,64}', language)
                or (whitelist is not None and (len(whitelist) > 64 or '\n' in whitelist or '\r' in whitelist))):
            raise ValueError('Invalid native OCR configuration')
        with self.slots:
            try:
                with self.lock:
                    if self.pool is None:
                        raise RuntimeError('Native OCR pool is closed')
                    result = self.pool.apply_async(_recognize,
                        (self.directory, self.tessdata, png, psm, whitelist, language))
                deadline = time.monotonic() + min(90, max(0, seconds))
                while True:
                    with self.lock:
                        if self.pool is None:
                            raise RuntimeError('Native OCR pool is closed')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('Native OCR worker deadline exceeded')
                    try:
                        return result.get(timeout=min(1.0, remaining))
                    except MP.TimeoutError:
                        continue
            except Exception as exc:
                # A native crash or hung decoder cannot hang the web process.
                # The parent engine disables this backend for this document.
                self.close()
                raise RuntimeError('Native OCR worker unavailable') from exc

    def close(self):
        with self.lock:
            pool, self.pool = self.pool, None
        if pool is not None:
            pool.terminate()
            pool.join()
