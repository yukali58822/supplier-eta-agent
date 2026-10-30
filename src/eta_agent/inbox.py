from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import ROOT

log = logging.getLogger(__name__)
MAX_EML = 2_000_000


def inbox_dir() -> Path:
    p = Path(os.environ.get("INBOX_DIR", ROOT / "inbox"))
    return p if p.is_absolute() else ROOT / p


def _move(src: Path, dest: Path) -> bool:
    try:
        shutil.move(str(src), dest)
        return True
    except FileNotFoundError:
        return False


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    for i in range(1, 10_000):
        cand = dest.with_name(f"{dest.stem}_{i}{dest.suffix}")
        if not cand.exists():
            return cand
    raise RuntimeError(f"無法為 {dest} 取得不重複的檔名")


@dataclass
class ScanResult:
    file: str
    status: str
    message_id: str | None = None
    error: str | None = None


@dataclass
class InboxScanner:
    agent: Callable[[], Any]
    folder: Path = field(default_factory=inbox_dir)
    min_age_seconds: float = 2.0
    last_scan_at: datetime | None = None
    last_error: str | None = None
    recent: list[ScanResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._recovered = False

    @property
    def processed_dir(self) -> Path:
        return self.folder / "processed"

    @property
    def failed_dir(self) -> Path:
        return self.folder / "failed"

    def ensure_folders(self) -> None:
        for d in (self.folder, self.processed_dir, self.failed_dir):
            d.mkdir(parents=True, exist_ok=True)

    def waiting_files(self) -> list[Path]:
        if not self.folder.exists():
            return []
        return sorted(p for p in self.folder.iterdir() if p.is_file() and p.suffix.lower() == ".eml")

    def failed_count(self) -> int:
        return sum(1 for p in self.failed_dir.glob("*.eml")) if self.failed_dir.exists() else 0

    def scan_once(self) -> list[ScanResult]:
        if not self._lock.acquire(blocking=False):
            return []
        try:
            self.ensure_folders()
            results = []
            for path in self.waiting_files():
                try:
                    if time.time() - path.stat().st_mtime < self.min_age_seconds:
                        continue
                except FileNotFoundError:
                    continue
                res = self._handle(path)
                if res is not None:
                    results.append(res)
            if not self._recovered:
                self._recovered = True
                for res in self.agent().recover_interrupted():
                    results.append(ScanResult("（上次中斷，已重新分析）", res.get("status", "?"), res.get("message_id")))
            for res in self.agent().retry_failed():
                results.append(ScanResult("（自動重試）", res.get("status", "?"), res.get("message_id")))
            self.last_scan_at = datetime.now()
            self.last_error = None
            self.recent = (results + self.recent)[:20]
            return results
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {e}"
            log.exception("收件資料夾檢查失敗")
            return []
        finally:
            self._lock.release()

    def _handle(self, path: Path) -> ScanResult | None:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            if len(raw) > MAX_EML:
                raise ValueError(f"信件超過 {MAX_EML // 1_000_000} MB")
            res = self.agent().process_eml(raw)
        except Exception as e:  # noqa: BLE001
            dest = _unique(self.failed_dir / path.name)
            if not _move(path, dest):
                return ScanResult(path.name, "failed", error=f"{type(e).__name__}: {e}")
            dest.with_name(dest.name + ".error.txt").write_text(
                f"處理時間：{datetime.now():%Y-%m-%d %H:%M:%S}\n錯誤：{type(e).__name__}: {e}\n"
                "排除問題後，把 .eml 移回 inbox 資料夾即可重新處理。\n", encoding="utf-8")
            log.warning("信件 %s 處理失敗：%s", path.name, e)
            return ScanResult(path.name, "failed", error=f"{type(e).__name__}: {e}")
        day = self.processed_dir / datetime.now().strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True)
        _move(path, _unique(day / path.name))
        mid = res.get("message_id")
        if res.get("status") != "duplicate":
            self.agent().store.audit(mid, "inbox_file", {"file": path.name})
        return ScanResult(path.name, res.get("status", "?"), mid)


class InboxWatcher:
    def __init__(self, scanner: InboxScanner, interval: float = 10.0):
        self.scanner, self.interval = scanner, interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="inbox-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.scanner.scan_once()
            self._stop.wait(self.interval)
