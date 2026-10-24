import os
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

from ocr_pipeline import preview
from ocr_pipeline.process_guard import ProcessGuard, run_worker, worker_environment

PREFIX = "from ocr_pipeline.process_guard import enter_worker_job; enter_worker_job(); "


class ProcessGuardTests(unittest.TestCase):
    def run_code(self, code, **limits):
        options = {
            "memory_bytes": 256 * 1024 * 1024,
            "seconds": 5,
            "output_limit": 4096,
        }
        options.update(limits)
        return run_worker([sys.executable, "-u", "-c", PREFIX + code], **options)

    def test_normal_output_and_secret_free_environment(self):
        with patch.dict(
            os.environ, {"OCR_SECRET_KEY": "test-only", "GOOGLE_API_KEY": "test-only"}
        ):
            self.assertNotIn("OCR_SECRET_KEY", worker_environment())
            code, output = self.run_code(
                "import os; print('OCR_SECRET_KEY' in os.environ, 'GOOGLE_API_KEY' in os.environ)"
            )
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), b"False False")

    def test_timeout_and_output_limit_are_bounded(self):
        started = time.monotonic()
        with self.assertRaises(RuntimeError):
            self.run_code("import time; time.sleep(60)", seconds=0.5)
        self.assertLess(time.monotonic() - started, 7)
        with self.assertRaises(RuntimeError):
            self.run_code("print('x'*10000)", output_limit=100)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object regression")
    def test_windows_memory_limit_covers_launcher_and_child(self):
        child = PREFIX + "blob=bytearray(128*1024*1024); print('allocated')"
        code = (
            "import subprocess,sys; "
            f"p=subprocess.run([sys.executable,'-u','-c',{child!r}]); "
            "raise SystemExit(p.returncode)"
        )
        try:
            status, output = self.run_code(code, memory_bytes=96 * 1024 * 1024)
        except RuntimeError:
            return  # Aggregate RSS monitoring also fails closed.
        self.assertNotEqual(status, 0)
        self.assertNotIn(b"allocated", output)

    def test_close_kills_descendants_even_after_launcher_exits(self):
        child = PREFIX + "import time; time.sleep(60)"
        program = (
            PREFIX + "import subprocess,sys; "
            f"p=subprocess.Popen([sys.executable,'-u','-c',{child!r}]); "
            "print(p.pid,flush=True)"
        )
        guard = ProcessGuard(256 * 1024 * 1024)
        process = guard.start([sys.executable, "-u", "-c", program])
        messages = queue.Queue()
        reader = threading.Thread(
            target=lambda: messages.put(process.stdout.readline()), daemon=True
        )
        reader.start()
        try:
            child_process = psutil.Process(int(messages.get(timeout=5)))
            process.wait(timeout=5)
            guard.close()
            guard.close()
            _, alive = psutil.wait_procs([child_process], timeout=5)
            self.assertFalse(alive)
        finally:
            guard.close()
            reader.join(timeout=1)
            process.stdout.close()

    def test_preview_rejects_oversized_and_malformed_worker_result(self):
        with (
            patch.object(preview, "run_worker", return_value=(0, b"not a PNG")),
            self.assertRaises(RuntimeError),
        ):
            preview.render_preview(Path("test.pdf"), 1)
        with (
            patch.object(preview, "run_worker", return_value=(4, b"")),
            self.assertRaises(IndexError),
        ):
            preview.render_preview(Path("test.pdf"), 99)


if __name__ == "__main__":
    unittest.main()
