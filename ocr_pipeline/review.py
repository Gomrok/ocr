"""Owned, durable review snapshots and replace-all search handoff; no search engine or DB."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import threading
import uuid

from .precision.extraction import normalize_search


class ReviewConflict(Exception):
    pass


_PROCESS_LOCK = threading.RLock()


def _identifier(value, pattern):
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError("Invalid review identity")
    return value


def _write_atomic(path, data):
    serialized = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    if len(serialized) > 16 * 1024 * 1024:
        raise ValueError("Review state exceeds document budget")
    if shutil.disk_usage(path.parent).free < 1536 * 1024 * 1024 + 2*len(serialized):
        raise OSError("Review storage reserve reached")
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ReviewStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def _directory(self, owner, document_id):
        owner = _identifier(owner, r"[a-f0-9]{32}")
        document_id = _identifier(document_id, r"[a-f0-9]{64}")
        directory = (self.root / owner / document_id).resolve()
        if self.root not in directory.parents:
            raise ValueError("Review path escapes storage")
        return directory

    @contextmanager
    def _locked(self, directory):
        # A process lock plus an OS byte lock protects optimistic updates across workers.
        with _PROCESS_LOCK:
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / ".lock").open("a+b") as handle:
                if handle.tell() == 0:
                    handle.write(b"0"); handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    handle.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def register(self, owner, payload, snapshot_directory, *, display_name=None):
        document_id = payload["document"]["source_sha256"]
        revision = _identifier(payload["extraction_revision"], r"[a-f0-9]{24}")
        directory = self._directory(owner, document_id)
        snapshot_directory = Path(snapshot_directory).resolve(strict=True)
        with self._locked(directory):
            path = directory / "state.json"
            state = self._load(owner, document_id)[1] if path.exists() else {
                "document_id": document_id, "version": 0, "active_extraction_revision": revision,
                "revisions": {}, "history": [], "name": display_name or payload["document"]["filename"]}
            if revision not in state["revisions"]:
                state["revisions"][revision] = {"snapshot_directory": str(snapshot_directory), "edits": {},
                    "pages": [{key: page.get(key) for key in ("page", "reference_image")} for page in payload["pages"]]}
                state["version"] += 1
                # Reprocessing must not silently replace the approved search revision.
                self._save_state(directory, state)
        return document_id

    def _save_state(self, directory, state):
        _write_atomic(directory / "state.json", state)
        summary = {key: state[key] for key in ("document_id", "version", "name", "active_extraction_revision")}
        try:
            _write_atomic(directory / "summary.json", summary)
        except OSError:
            # The authoritative review is committed; the optional list summary can be rebuilt.
            logging.getLogger(__name__).warning("Review list summary could not be refreshed")

    def _load(self, owner, document_id):
        directory = self._directory(owner, document_id)
        path = directory / "state.json"
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Review state exceeds document budget")
        state = json.loads(path.read_text("utf-8"))
        return directory, state

    def _snapshot(self, state, revision=None):
        revision = revision or state["active_extraction_revision"]
        entry = state["revisions"][revision]
        path = Path(entry["snapshot_directory"]) / "extraction.json"
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("Extraction snapshot exceeds review limit")
        payload = json.loads(path.read_text("utf-8"))
        if payload["document"]["source_sha256"] != state["document_id"] or payload["extraction_revision"] != revision:
            raise ValueError("Extraction snapshot identity mismatch")
        return entry, payload

    def list_documents(self, owner):
        owner = _identifier(owner, r"[a-f0-9]{32}")
        directory = (self.root / owner).resolve()
        if self.root not in directory.parents:
            raise ValueError("Review path escapes storage")
        result = []
        for path in sorted(directory.glob("*/state.json"))[:300]:
            if re.fullmatch(r"[a-f0-9]{64}", path.parent.name):
                summary = path.with_name("summary.json")
                if summary.is_file() and summary.stat().st_size <= 4096:
                    result.append(json.loads(summary.read_text("utf-8")))
                else:
                    result.append({"document_id":path.parent.name, "name":"سند آمادهٔ بازبینی", "version":None, "active_extraction_revision":None})
        return result

    def get(self, owner, document_id):
        _, state = self._load(owner, document_id)
        entry, payload = self._snapshot(state)
        units = []
        for page in payload["pages"]:
            for unit in page.get("regions", []):
                edit = entry["edits"].get(unit["span_id"], {})
                units.append({key: unit.get(key) for key in (
                    "span_id", "page", "bbox", "kind", "content_text", "engine_text_raw", "reasons", "row", "column", "table_id")}
                    | {"alternatives": list(dict.fromkeys(a.get("engine_text_raw", "") for a in unit["attempts"] if not a.get("error"))),
                       "review_status": edit.get("status", "not_reviewed"), "verified_text": edit.get("text"),
                       "reviewer": edit.get("reviewer"), "reviewed_at": edit.get("reviewed_at"),
                       "handwriting": edit.get("handwriting", page.get("handwriting_declared", False))})
        return {"document_id": document_id, "version": state["version"], "name": state["name"],
                "extraction_revision": state["active_extraction_revision"], "available_revisions": list(state["revisions"]),
                "pages": [{key: p.get(key) for key in ("page", "width", "height", "status", "error")} for p in payload["pages"]],
                "units": units, "search_entries": len(self._search_entries(state, payload)),
                "search_integration": "file_handoff_only_no_index_connected"}

    def image_path(self, owner, document_id, page):
        _, state = self._load(owner, document_id)
        entry = state["revisions"][state["active_extraction_revision"]]
        found = next((p for p in entry["pages"] if p["page"] == page and p.get("reference_image")), None)
        if not found:
            raise FileNotFoundError("Reference page not available")
        root = Path(entry["snapshot_directory"]).resolve()
        path = (root / found["reference_image"]).resolve()
        if root not in path.parents:
            raise ValueError("Reference path escapes snapshot")
        return path

    def save(self, owner, document_id, expected_version, changes, *, reviewer, activate_revision=None):
        if (type(expected_version) is not int or not isinstance(changes, list) or len(changes) > 100
                or not isinstance(reviewer, str) or not 1 <= len(reviewer.strip()) <= 120):
            raise ValueError("Version, bounded changes and human reviewer name are required")
        directory = self._directory(owner, document_id)
        with self._locked(directory):
            _, state = self._load(owner, document_id)
            if expected_version != state["version"]:
                raise ReviewConflict("Review version changed")
            if activate_revision is not None:
                _identifier(activate_revision, r"[a-f0-9]{24}")
                if activate_revision not in state["revisions"] or changes:
                    raise ValueError("Switch revision separately from editing")
                state["active_extraction_revision"] = activate_revision
            entry, payload = self._snapshot(state)
            units = {u["span_id"]:u for p in payload["pages"] for u in p.get("regions", [])}
            declared_handwriting = {u["span_id"]: p.get("handwriting_declared", False)
                                    for p in payload["pages"] for u in p.get("regions", [])}
            identifiers = []
            for change in changes:
                if not isinstance(change, dict) or set(change) - {"span_id", "text", "status", "handwriting"}:
                    raise ValueError("Invalid review change")
                identifier, status = change.get("span_id"), change.get("status")
                value = change.get("text")
                if (not isinstance(identifier, str) or identifier not in units or identifier in identifiers
                        or status not in ("approved", "unreadable", "not_reviewed")):
                    raise ValueError("Unknown or duplicate unit/status")
                if status == "approved" and (not isinstance(value, str) or not value.strip()):
                    raise ValueError("Approval requires an explicit nonempty transcription")
                if value is not None and (not isinstance(value, str) or len(value) > 10_000 or any(ord(c)<32 and c not in "\n\t\r" for c in value)):
                    raise ValueError("Invalid or oversized review text")
                if "handwriting" in change and type(change["handwriting"]) is not bool:
                    raise ValueError("Handwriting flag must be boolean")
                identifiers.append(identifier)
                previous = entry["edits"].get(identifier)
                edit = {"text": value if status == "approved" else None, "status": status,
                        "reviewer": reviewer.strip(), "reviewed_at": datetime.now(timezone.utc).isoformat(),
                        "handwriting": change.get("handwriting", (previous or {}).get("handwriting", declared_handwriting[identifier]))}
                entry["edits"][identifier] = edit
                state["history"].append({"span_id": identifier, "revision": state["active_extraction_revision"],
                    "before": previous or {"text": units[identifier]["content_text"], "status": "not_reviewed"}, "after": edit})
            if len(state["history"]) > 20_000:
                raise ValueError("Review history limit reached; archive through operator workflow")
            state["version"] += 1
            state["history"].append({"version": state["version"], "active_revision": state["active_extraction_revision"],
                                     "reviewer": reviewer.strip(), "time": datetime.now(timezone.utc).isoformat()})
            # One atomic state includes edits and active extraction. Search is derived from this same state.
            self._save_state(directory, state)
        return self.get(owner, document_id)

    def _search_entries(self, state, payload):
        revision = state["active_extraction_revision"]
        edits = state["revisions"][revision]["edits"]
        entries = []
        for page in payload["pages"]:
            for region in page.get("regions", []):
                edit = edits.get(region["span_id"], {})
                if edit.get("status") != "approved":
                    continue
                # A replace-document consumer must remove older entries before applying this set.
                entries.append({"id": f"{state['document_id']}:{region['span_id']}", "span_id": region["span_id"],
                                "page": page["page"], "bbox": region["bbox"], "source_sha256": state["document_id"],
                                "text": normalize_search(edit["text"]), "human_text": edit["text"],
                                "reviewer": edit["reviewer"], "reviewed_at": edit["reviewed_at"]})
        return entries

    def search_snapshot(self, owner, document_id):
        _, state = self._load(owner, document_id)
        _, payload = self._snapshot(state)
        entries = self._search_entries(state, payload)
        return {"operation": "replace_document", "document_id": document_id, "version": state["version"],
                "extraction_revision": state["active_extraction_revision"], "entries": entries,
                "content_hash": hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                "integration_status": "NO_CONNECTED_INDEX"}
