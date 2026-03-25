from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SchedulerState:
    last_scheduled_run_local_date: str | None = None
    last_scheduled_target_date: str | None = None
    last_run_started_at_utc: str | None = None
    last_run_finished_at_utc: str | None = None
    last_run_status: str | None = None
    last_run_mode: str | None = None
    last_run_message: str | None = None
    catchup_executed_for_local_date: str | None = None
    in_progress_run_id: str | None = None
    last_preflight_local_date: str | None = None
    last_healthcheck_local_date: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "SchedulerState":
        raw = raw or {}
        return cls(
            last_scheduled_run_local_date=_as_optional_str(
                raw.get("last_scheduled_run_local_date")
            ),
            last_scheduled_target_date=_as_optional_str(raw.get("last_scheduled_target_date")),
            last_run_started_at_utc=_as_optional_str(raw.get("last_run_started_at_utc")),
            last_run_finished_at_utc=_as_optional_str(raw.get("last_run_finished_at_utc")),
            last_run_status=_as_optional_str(raw.get("last_run_status")),
            last_run_mode=_as_optional_str(raw.get("last_run_mode")),
            last_run_message=_as_optional_str(raw.get("last_run_message")),
            catchup_executed_for_local_date=_as_optional_str(
                raw.get("catchup_executed_for_local_date")
            ),
            in_progress_run_id=_as_optional_str(raw.get("in_progress_run_id")),
            last_preflight_local_date=_as_optional_str(raw.get("last_preflight_local_date")),
            last_healthcheck_local_date=_as_optional_str(
                raw.get("last_healthcheck_local_date")
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunHistoryEntry:
    run_id: str
    mode: str
    started_at_utc: str
    finished_at_utc: str
    target_date: str | None
    seat_attempt_order: list[str]
    chosen_seat: str | None
    status: str
    summary: str
    otp_requested: bool
    otp_received: bool
    screenshot_path: str | None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RunHistoryEntry":
        return cls(
            run_id=str(raw.get("run_id") or ""),
            mode=str(raw.get("mode") or ""),
            started_at_utc=str(raw.get("started_at_utc") or ""),
            finished_at_utc=str(raw.get("finished_at_utc") or ""),
            target_date=_as_optional_str(raw.get("target_date")),
            seat_attempt_order=[str(item) for item in (raw.get("seat_attempt_order") or [])],
            chosen_seat=_as_optional_str(raw.get("chosen_seat")),
            status=str(raw.get("status") or ""),
            summary=str(raw.get("summary") or ""),
            otp_requested=bool(raw.get("otp_requested")),
            otp_received=bool(raw.get("otp_received")),
            screenshot_path=_as_optional_str(raw.get("screenshot_path")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RunLockError(RuntimeError):
    pass


class RuntimeStateStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.scheduler_state_path = self.state_dir / "scheduler_state.json"
        self.run_history_path = self.state_dir / "run_history.jsonl"
        self.run_lock_path = self.state_dir / "run.lock"
        self._run_lock_handle = None

    def load_scheduler_state(self) -> SchedulerState:
        if not self.scheduler_state_path.exists():
            return SchedulerState()
        try:
            raw = json.loads(self.scheduler_state_path.read_text(encoding="utf-8"))
        except Exception:
            return SchedulerState()
        if not isinstance(raw, dict):
            return SchedulerState()
        return SchedulerState.from_dict(raw)

    def save_scheduler_state(self, state: SchedulerState) -> None:
        self.scheduler_state_path.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def append_run_history(self, entry: RunHistoryEntry, limit: int = 1000) -> None:
        items = self.read_run_history(limit=max(0, limit - 1))
        items.append(entry)
        if limit > 0 and len(items) > limit:
            items = items[-limit:]
        with self.run_history_path.open("w", encoding="utf-8", newline="\n") as handle:
            for item in items:
                handle.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")

    def read_run_history(self, limit: int = 10) -> list[RunHistoryEntry]:
        if not self.run_history_path.exists():
            return []
        out: list[RunHistoryEntry] = []
        with self.run_history_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    decoded = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(decoded, dict):
                    continue
                out.append(RunHistoryEntry.from_dict(decoded))
        if limit > 0:
            return out[-limit:]
        return out

    def read_last_history(self) -> RunHistoryEntry | None:
        items = self.read_run_history(limit=1)
        if not items:
            return None
        return items[-1]

    def acquire_run_lock(self, run_id: str, stale_after_sec: int) -> None:
        if os.name != "nt":
            self._acquire_posix_run_lock(run_id)
            return
        self._acquire_legacy_run_lock(run_id, stale_after_sec)

    def _acquire_posix_run_lock(self, run_id: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": run_id,
            "acquired_at_utc": utc_now().isoformat(),
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
        }
        handle = self.run_lock_path.open("a+", encoding="utf-8", newline="\n")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                info = self._read_lock_info()
                raise RunLockError(
                    "Another booking/preflight run is already in progress. "
                    f"Lock info: {info or '<unknown>'}"
                ) from None

            handle.seek(0)
            handle.truncate()
            json.dump(payload, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            self._run_lock_handle = handle
            return
        except Exception:
            try:
                handle.close()
            except Exception:
                pass
            raise

    def _acquire_legacy_run_lock(self, run_id: str, stale_after_sec: int) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": run_id,
            "acquired_at_utc": utc_now().isoformat(),
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
        }
        attempt = 0
        while True:
            attempt += 1
            try:
                fd = os.open(
                    str(self.run_lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                        json.dump(payload, handle, ensure_ascii=False)
                        handle.write("\n")
                except Exception:
                    try:
                        os.close(fd)
                    except Exception:
                        pass
                    raise
                return
            except FileExistsError:
                if not self._clear_stale_lock(stale_after_sec):
                    info = self._read_lock_info()
                    raise RunLockError(
                        "Another booking/preflight run is already in progress. "
                        f"Lock info: {info or '<unknown>'}"
                    ) from None
                if attempt >= 2:
                    raise RunLockError("Could not acquire run lock after stale cleanup.") from None

    def release_run_lock(self, run_id: str) -> None:
        if self._run_lock_handle is not None:
            try:
                self._run_lock_handle.seek(0)
                try:
                    fcntl.flock(self._run_lock_handle.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                self._run_lock_handle.close()
            finally:
                self._run_lock_handle = None
            try:
                self.run_lock_path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass
            return

        if not self.run_lock_path.exists():
            return
        info = self._read_lock_info()
        if info and str(info.get("run_id") or "") not in {"", run_id}:
            return
        try:
            self.run_lock_path.unlink()
        except FileNotFoundError:
            return

    def _clear_stale_lock(self, stale_after_sec: int) -> bool:
        if not self.run_lock_path.exists():
            return True
        info = self._read_lock_info()
        if info and self._lock_owner_is_definitely_dead(info):
            try:
                self.run_lock_path.unlink()
            except FileNotFoundError:
                return True
            return True
        if stale_after_sec <= 0:
            return False
        acquired_at = None
        if info:
            raw = info.get("acquired_at_utc")
            if isinstance(raw, str) and raw.strip():
                try:
                    acquired_at = datetime.fromisoformat(raw)
                except Exception:
                    acquired_at = None
        if acquired_at is None:
            try:
                stat = self.run_lock_path.stat()
                acquired_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            except Exception:
                acquired_at = None
        if acquired_at is None:
            return False
        if utc_now() - acquired_at <= timedelta(seconds=stale_after_sec):
            return False
        try:
            self.run_lock_path.unlink()
        except FileNotFoundError:
            return True
        return True

    def _read_lock_info(self) -> dict[str, Any] | None:
        if not self.run_lock_path.exists():
            return None
        try:
            raw = json.loads(self.run_lock_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        return raw

    def _lock_owner_is_definitely_dead(self, info: dict[str, Any]) -> bool:
        hostname = str(info.get("hostname") or "").strip()
        pid = info.get("pid")
        if hostname and hostname != socket.gethostname():
            return False
        if not isinstance(pid, int) or pid <= 0:
            return False
        return not _pid_exists(pid)


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
