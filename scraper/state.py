"""
Run state: single-instance lock + resumable checkpoint.

* RunLock prevents overlapping executions (scheduled + manual, or two schedulers).
  A lock left behind by a crashed process is detected (dead PID or too old) and replaced.
* Checkpoint records which category pages were fully discovered, which products
  were processed, and appends every processed record to a JSONL file. If a run is
  interrupted (crash, reboot, Ctrl+C), the next run resumes from there instead of
  starting over, as long as the checkpoint is younger than ``resume_max_age_hours``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterator, List, Optional

log = logging.getLogger("scraper.state")


# --------------------------------------------------------------------------- #
# Lock
# --------------------------------------------------------------------------- #
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class AlreadyRunning(RuntimeError):
    pass


class RunLock:
    def __init__(self, path: Path, stale_after_hours: float = 12):
        self.path = Path(path)
        self.stale_after = stale_after_hours * 3600
        self._held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._is_stale():
                    log.warning("removing stale lock %s", self.path)
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                raise AlreadyRunning(f"another scraper run is active (lock: {self.path})")
            with os.fdopen(fd, "w") as handle:
                json.dump({"pid": os.getpid(), "started": time.time()}, handle)
            self._held = True
            return
        raise AlreadyRunning(f"could not acquire lock {self.path}")

    def _is_stale(self) -> bool:
        try:
            info = json.loads(self.path.read_text())
            pid, started = int(info.get("pid", 0)), float(info.get("started", 0))
        except (OSError, ValueError):
            return True
        if time.time() - started > self.stale_after:
            return True
        return not _pid_alive(pid)

    def release(self) -> None:
        if self._held:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self._held = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


class WaitingFileLock:
    """
    Short-lived mutex around the shared output files.

    Sites run independently (possibly at the same moment, or next to a manual run), but they
    all merge into one products.csv. The holder keeps this lock only while files are written,
    so waiting a little is always the right answer: unlike RunLock this one blocks.
    """

    def __init__(self, path: Path, timeout_seconds: float = 300, stale_after_seconds: float = 600):
        self._lock = RunLock(path, stale_after_seconds / 3600)
        self.timeout = timeout_seconds

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._lock.acquire()
                return self
            except AlreadyRunning:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)

    def __exit__(self, *exc):
        self._lock.release()


# --------------------------------------------------------------------------- #
# Checkpoint
# --------------------------------------------------------------------------- #
class Checkpoint:
    def __init__(self, state_dir: Path, resume_max_age_hours: float = 20):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "checkpoint.json"
        self.max_age = timedelta(hours=resume_max_age_hours)
        self.data: Dict = {}

    # ------------------------------------------------------------ lifecycle
    def start_or_resume(self, started_at: datetime, force_new: bool = False) -> bool:
        """Returns True when an interrupted run is being resumed."""
        previous = self._load()
        if (not force_new and previous and previous.get("status") == "running"
                and started_at - datetime.fromisoformat(previous["started_at"]) <= self.max_age):
            self.data = previous
            self.data["resumed_count"] = int(self.data.get("resumed_count", 0)) + 1
            self._save()
            log.info("resuming interrupted run %s (%d products already done)",
                     self.run_id, len(self.data.get("done", {})))
            return True
        if previous and previous.get("status") == "running":
            log.warning("discarding old unfinished run %s (too old or fresh run forced)", previous.get("run_id"))
        self.data = {
            "run_id": started_at.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "started_at": started_at.isoformat(timespec="seconds"),
            "status": "running",
            "discovered": {},        # target_id -> {"done": bool, "items": [...]}
            "done": {},              # item key -> "ok" | "skipped" | "failed:<n>"
            "denied_sites": [],
            "resumed_count": 0,
        }
        self.records_path.unlink(missing_ok=True)
        for old in self.dir.glob("discovered_*.json"):
            old.unlink(missing_ok=True)
        self._save()
        return False

    def finish(self, status: str = "completed", summary: Optional[Dict] = None) -> None:
        self.data["status"] = status
        self.data["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        if summary:
            self.data["summary"] = summary
        self._save()

    @property
    def run_id(self) -> str:
        return self.data["run_id"]

    @property
    def started_at(self) -> str:
        return self.data["started_at"]

    @property
    def records_path(self) -> Path:
        return self.dir / "current_run_records.jsonl"

    # ------------------------------------------------------------ discovery
    def discovery_done(self, target_id: str) -> bool:
        return bool(self.data["discovered"].get(target_id, {}).get("done"))

    def _discovery_file(self, target_id: str) -> Path:
        digest = hashlib.sha1(target_id.encode("utf-8")).hexdigest()[:16]
        return self.dir / f"discovered_{digest}.json"

    def save_discovery(self, target_id: str, items: List[Dict]) -> None:
        # items live in their own file so the small checkpoint can be rewritten cheaply
        path = self._discovery_file(target_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        self.data["discovered"][target_id] = {"done": True, "file": path.name, "count": len(items)}
        self._save()

    def discovered_items(self, target_id: str) -> List[Dict]:
        path = self._discovery_file(target_id)
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------ products
    def status_of(self, key: str) -> str:
        return self.data["done"].get(key, "")

    def failures_of(self, key: str) -> int:
        status = self.status_of(key)
        return int(status.split(":")[1]) if status.startswith("failed:") else 0

    def mark(self, key: str, status: str) -> None:
        self.data["done"][key] = status
        self._save()

    def mark_failed(self, key: str) -> int:
        count = self.failures_of(key) + 1
        self.mark(key, f"failed:{count}")
        return count

    def append_record(self, payload: Dict) -> None:
        with self.records_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def iter_records(self) -> Iterator[Dict]:
        if not self.records_path.exists():
            return
        with self.records_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:      # last line may be cut by a crash
                        log.warning("ignoring corrupt checkpoint line")

    def deny_site(self, site: str) -> None:
        if site not in self.data["denied_sites"]:
            self.data["denied_sites"].append(site)
            self._save()

    def site_denied(self, site: str) -> bool:
        return site in self.data.get("denied_sites", [])

    # ------------------------------------------------------------ io
    def _load(self) -> Dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("checkpoint file unreadable; starting fresh")
            return {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
