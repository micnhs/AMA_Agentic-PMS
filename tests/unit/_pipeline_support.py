"""Helpers for the application-service tests (offline, standard library only).

Every test pipeline gets a fresh in-memory store, a fixed demo clock, an in-memory file store
(outbox, originals) and file-less telemetry unless a test asks for real folders. Real folders live in a
folder of THIS test process (``var/test-runs/pid<pid>-<random>``, never a system temp directory):
parallel test runs never share a folder, and :func:`cleanup_runs` only deletes what this process created.
Receipt times are the golden-set times (``_support.RECEIVED_AT``); only the golden comparator reads
expected.json.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from pms import runtime as rt
from pms.config import local_test_settings
from pms.models import FixedClock
from pms.pipeline import Pipeline
from pms.telemetry import Telemetry

from ._support import DATA, LETTERS, RECEIVED_AT, REPO, letter_text

RUNS_ROOT = REPO / "var" / "test-runs"
RUNS_ROOT.mkdir(parents=True, exist_ok=True)
#: The folder of this test process; removed when the process ends.
RUNS = Path(tempfile.mkdtemp(prefix=f"pid{os.getpid()}-", dir=RUNS_ROOT))
_created: list[Path] = []
_created_lock = threading.Lock()


def work_dir(name: str, *, create: bool = False) -> Path:
    path = RUNS / f"{name}-{uuid.uuid4().hex[:8]}"
    with _created_lock:
        _created.append(path)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def remove_tree(path: Path, attempts: int = 5) -> None:
    """rmtree with retries: the shared workspace file system sometimes lists stale entries for a moment."""
    for attempt in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.2 * (attempt + 1))


def cleanup_runs() -> None:
    """Delete the work folders this process created so far - never those of another (parallel) test run."""
    with _created_lock:
        paths = list(_created)
        _created.clear()
    for path in paths:
        remove_tree(path)


atexit.register(remove_tree, RUNS)


def new_pipeline(name: str = "p", *, at: str = "2026-11-16T08:00:00Z", runtime: Any = None, notifier: Any = None,
                 real_files: bool = False, telemetry: Any = None, **overrides: Any) -> tuple[Pipeline, FixedClock]:
    settings = local_test_settings(work_dir(name, create=real_files), **overrides)
    clock = FixedClock(at)
    store = rt.get_store(settings, path=":memory:", clock=clock)
    files = None if real_files else rt.MemoryFileStore()
    pipeline = Pipeline(settings, store=store, clock=clock, runtime=runtime, notifier=notifier, files=files,
                        telemetry=telemetry or Telemetry(trace_path=None))
    return pipeline, clock


def letter_file(letter_id: str) -> Path:
    return next(LETTERS.glob(f"{letter_id}_*.txt"))


def ingest(pipeline: Pipeline, clock: FixedClock, letter_id: str, received_at: str | None = None, **kw: Any):
    at = received_at or RECEIVED_AT[letter_id]
    clock.set(at)
    return pipeline.ingest_letter(filename=letter_file(letter_id).name, text=letter_text(letter_id),
                                  received_at=at, ref=letter_id, **kw)


def seeded(name: str = "seeded", **kw: Any) -> tuple[Pipeline, FixedClock]:
    """All letters and seed messages in received_at order, each analysed right away."""
    pipeline, clock = new_pipeline(name, **kw)
    pipeline.seed_demo(replay_clock=clock, drain=True)
    return pipeline, clock


__all__ = ["DATA", "RECEIVED_AT", "REPO", "RUNS", "cleanup_runs", "ingest", "letter_file", "new_pipeline",
           "remove_tree", "seeded", "work_dir"]
