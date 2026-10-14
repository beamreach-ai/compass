import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _default_state() -> Dict[str, Any]:
    return {"ropes": {}, "findings": {}, "jobs": {}}


class LocalStateStore:
    def __init__(self, path: Optional[str] = None) -> None:
        legacy = Path.home() / ".beamreach"  # where older installs kept it
        default_dir = Path(os.getenv("COMPASS_STATE_DIR") or os.getenv("BEAMREACH_STATE_DIR") or (legacy if legacy.exists() else Path.home() / ".compass"))
        default_dir.mkdir(parents=True, exist_ok=True)
        self._path = Path(path or (default_dir / "state.json"))
        if not self._path.exists():
            self._path.write_text(json.dumps(_default_state(), indent=2))
        self._lock = threading.RLock()

    def _load(self) -> Dict[str, Any]:
        try:
            with self._path.open("r", encoding="utf-8") as fd:
                return json.load(fd)
        except Exception:
            return _default_state()

    def _save(self, state: Dict[str, Any]) -> None:
        tmp_path = self._path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as fd:
            json.dump(state, fd, indent=2, sort_keys=True)
        tmp_path.replace(self._path)

    def _ensure_rope(self, state: Dict[str, Any], rope_id: str) -> Dict[str, Any]:
        ropes = state.setdefault("ropes", {})
        rope = ropes.get(rope_id)
        now_ms = int(time.time() * 1000)
        if not rope:
            rope = {
                "rope_id": rope_id,
                "status": "in_progress",
                "created_at": now_ms,
                "last_updated": now_ms,
                "progress": [],
                "logs": [],
            }
            ropes[rope_id] = rope
        rope.setdefault("progress", [])
        rope.setdefault("logs", [])
        rope.setdefault("metadata", {})
        return rope

    def append_progress(
        self,
        session_id: str,
        *,
        message: str,
        phase: str,
        status: str,
        timestamp_ms: int,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not session_id:
            return
        with self._lock:
            state = self._load()
            rope = self._ensure_rope(state, session_id)
            entry = {
                "ts": timestamp_ms,
                "message": message,
                "phase": phase,
                "status": status,
            }
            if extra:
                entry["extra"] = extra
            rope["progress"].append(entry)
            rope["last_updated"] = max(rope.get("last_updated", timestamp_ms), timestamp_ms)
            self._save(state)

    def update_status(
        self,
        session_id: str,
        *,
        status: str,
        timestamp_ms: int,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not session_id:
            return
        with self._lock:
            state = self._load()
            rope = self._ensure_rope(state, session_id)
            rope["status"] = status
            rope["last_updated"] = max(rope.get("last_updated", timestamp_ms), timestamp_ms)
            if extra:
                for key, value in extra.items():
                    if value is None:
                        continue
                    if key in ("pr_urls", "pr_actions", "answer", "triage", "repo", "triage_updated_at"):
                        rope[key] = value
                    else:
                        rope.setdefault("metadata", {})[key] = value
            self._save(state)

    def upsert_rope(self, rope_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not rope_id:
            raise ValueError("rope_id is required")
        with self._lock:
            state = self._load()
            ropes = state.setdefault("ropes", {})
            existing = ropes.get(rope_id, {})
            now_ms = int(time.time() * 1000)
            merged = {
                "rope_id": rope_id,
                "created_at": existing.get("created_at", payload.get("created_at", now_ms)),
                "last_updated": payload.get("last_updated", now_ms),
                "status": payload.get("status", existing.get("status", "in_progress")),
                "message": payload.get("message", existing.get("message")),
                "target": payload.get("target", existing.get("target")),
                "console": payload.get("console", existing.get("console", [])),
                "logs": payload.get("logs", existing.get("logs", [])),
                "metadata": payload.get("metadata", existing.get("metadata", {})),
            }
            merged["progress"] = existing.get("progress", [])
            merged["triage"] = payload.get("triage", existing.get("triage"))
            merged["pr_urls"] = payload.get("pr_urls", existing.get("pr_urls"))
            merged["pr_actions"] = payload.get("pr_actions", existing.get("pr_actions"))
            ropes[rope_id] = merged
            self._save(state)
            return merged

    def append_log(
        self,
        rope_id: str,
        *,
        message: str,
        phase: Optional[str],
        status: Optional[str],
        timestamp_ms: int,
    ) -> Dict[str, Any]:
        if not rope_id:
            raise ValueError("rope_id is required")
        with self._lock:
            state = self._load()
            rope = self._ensure_rope(state, rope_id)
            entry = {"ts": timestamp_ms, "message": message}
            if phase:
                entry["phase"] = phase
            rope_logs: List[Dict[str, Any]] = rope.setdefault("logs", [])
            rope_logs.append(entry)
            if status:
                rope["status"] = status
            rope["last_updated"] = max(rope.get("last_updated", timestamp_ms), timestamp_ms)
            self._save(state)
            return rope

    def upsert_triage_for_rope(self, rope_id: str, payload: Dict[str, Any]) -> None:
        if not rope_id:
            return
        with self._lock:
            state = self._load()
            rope = self._ensure_rope(state, rope_id)
            rope["triage"] = payload
            rope["triage_updated_at"] = int(time.time() * 1000)
            rope["last_updated"] = max(rope.get("last_updated", 0), rope["triage_updated_at"])
            self._save(state)

    def upsert_triage_for_finding(self, finding_id: str, payload: Dict[str, Any]) -> None:
        if not finding_id:
            return
        with self._lock:
            state = self._load()
            findings = state.setdefault("findings", {})
            now_ms = int(time.time() * 1000)
            findings[finding_id] = {
                "triage": payload,
                "triage_updated_at": now_ms,
            }
            rope = state.get("ropes", {}).get(finding_id)
            if rope:
                rope["triage"] = payload
                rope["triage_updated_at"] = now_ms
                rope["last_updated"] = max(rope.get("last_updated", now_ms), now_ms)
            self._save(state)

    def get_rope(self, rope_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            state = self._load()
            rope = state.get("ropes", {}).get(rope_id)
            return json.loads(json.dumps(rope)) if rope else None

    def list_ropes(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            state = self._load()
            ropes = list(state.get("ropes", {}).values())
            ropes.sort(key=lambda entry: entry.get("last_updated", 0), reverse=True)
            trimmed = ropes[:limit]
            return json.loads(json.dumps(trimmed))

    def create_job(self, job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not job_id:
            raise ValueError("job_id is required")
        with self._lock:
            state = self._load()
            jobs = state.setdefault("jobs", {})
            payload = dict(payload)
            payload["job_id"] = job_id
            jobs[job_id] = payload
            self._save(state)
            return json.loads(json.dumps(payload))

    def update_job(self, job_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not job_id:
            raise ValueError("job_id is required")
        with self._lock:
            state = self._load()
            jobs = state.setdefault("jobs", {})
            existing = jobs.get(job_id)
            if not existing:
                return None
            for key, value in updates.items():
                existing[key] = value
            self._save(state)
            return json.loads(json.dumps(existing))

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        if not job_id:
            return None
        with self._lock:
            state = self._load()
            job = state.get("jobs", {}).get(job_id)
            return json.loads(json.dumps(job)) if job else None

    def list_jobs(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            state = self._load()
            jobs = list((state.get("jobs") or {}).values())
            jobs.sort(key=lambda entry: entry.get("updated_at", entry.get("created_at", 0)), reverse=True)
            trimmed = jobs[:limit]
            return json.loads(json.dumps(trimmed))

    def get_finding(self, finding_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            state = self._load()
            finding = state.get("findings", {}).get(finding_id)
            return json.loads(json.dumps(finding)) if finding else None


local_state_store = LocalStateStore()
