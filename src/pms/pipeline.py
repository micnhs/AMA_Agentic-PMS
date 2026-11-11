"""Signal pipeline and case services (concept 6.4, 6.5, 7, 9; Goal Card checks 3, 4, 6, 8).

Flow of one letter:

1. Intake (upload, API or seed import): text extraction; an unreadable file stays visible.
2. Rule code before any agent (:meth:`pms.rules.engine.RuleEngine.process_signal`: INJ, CRIT, LINK,
   REG, RS, DL). A critical result opens triage (status TRIAGE_ERFORDERLICH, task, alert row) - every
   critical letter gets its own task and alert, also on an already critical case ("new signal on
   critical case ...") - and the alert is delivered synchronously right after the rules (outbox file
   or demo e-mail) - before any analysis starts.
3. One analysis job is queued per signal; a worker (thread in-process, or the separate worker
   process in Azure) runs A1 -> A2 -> CORR-1.0 -> A6 -> A3 -> A4, at most two A6/A3 rounds.
   Agents only RAISE urgency (``Workflow.apply_ai_reading``); every run, tool call, cost and
   recommendation is audited exactly once; per-agent progress is stored for the dashboard.
4. Humans act through the workflow (:mod:`pms.workflow`) - the thin wrappers here add alert
   dispatch and re-analysis queueing.

All services come from :mod:`pms.runtime`; nothing here instantiates a concrete store or index.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from . import audit as A
from . import costs as costs_mod
from . import runtime as rt
from .agents.base import (
    WITHHELD_MARK, AgentInput, AgentResult, AgentRuntime, AgentSpec, LetterBlock, TokenUsage, draft_envelope,
    validate_schema,
)
from .agents.offline import MODEL as OFFLINE_MODEL
from .agents.offline import MODEL_VERSION as OFFLINE_MODEL_VERSION
from .agents.specs import (
    AGENTS, CAUSALITY_NOTE, CAUSALITY_SCALE, IMDRF_CODES, agent_catalogue, load_prompt, model_version_of,
)
from .agents.tools import ToolBox
from .agents.trend import trend_view as a5_trend_view
from .config import Settings
from .costs import PriceTable
from .intake import CHANNEL_LABELS, DecodedUpload, decode_upload, load_intake, safe_filename
from .interfaces import CaseStore, Delivery, FileStore, Job, JobQueue, KnowledgeSearch, Notifier
from .knowledge import ReferenceData
from .models import (
    CLOSED_STATUSES, REJECT_REASON_CODES, STATUS_LABELS_EN, Actor, Case, Clock, Signal, SignalInput, Status,
    SystemClock, iso_utc, parse_utc,
)
from .notify import case_link
from .rules.engine import RuleEngine
from .telemetry import AUDIT_VERIFY_INTERVAL_S, HEARTBEAT_INTERVAL_S, Telemetry
from .versions import ALL_RULE_VERSIONS, DL, RS, manifest
from .workflow import (
    ANALYSIS_DONE, ANALYSIS_FAILED, ANALYSIS_PENDING, ANALYSIS_RUNNING, TASK_UNMATCHED, TASK_UNREADABLE, Action,
    Workflow,
)

log = logging.getLogger("pms.pipeline")

MAX_ANALYSIS_ROUNDS = 2
MAX_ALERT_ATTEMPTS = 5
#: The upload path delivers its alert itself; the worker only picks up PENDING alerts older than this
#: (safety net, e.g. after a crash between commit and delivery) and retries FAILED ones.
ALERT_WORKER_GRACE_S = 10.0
ALERT_TARGET_SECONDS = 60
#: Shared store (Azure): one process claims a delivery attempt for this long (web sender thread vs. worker retry);
#: longer than one ACS send (runtime.ACS_SEND_TIMEOUT_S) and short enough for a takeover within the 60 s target.
ALERT_CLAIM_S = 30.0
STALE_JOB_AFTER = _dt.timedelta(minutes=15)
TASK_REANALYSIS = "RE_ANALYSIS"
STEPS: tuple[str, ...] = ("A1", "A2", "CORR", "A6", "A3", "A4")
_EXTRACTION_KEYS = ("patient_refs", "lots", "udi_dis", "models", "unknown_models", "families", "implant_dates",
                    "event_dates", "implant_sites", "sender_refs")
_FIELD_LABELS = {"device_model_or_version": "device model or version", "lot_or_udi": "lot or UDI-DI",
                 "patient_reference": "patient reference", "patient_exposure": "patient exposure",
                 "event_date": "event date", "event_description": "event description",
                 "product_family": "product family", "market": "market (incident state)", "outcome": "outcome",
                 "cause": "cause"}


class AnalysisFailed(RuntimeError):
    """An agent step failed; the job stops (no fictitious next step) and the failure stays visible."""


@dataclass
class IngestResult:
    signal_id: str
    case_id: str
    case_key: str
    relation: str
    status: str
    critical: bool
    newly_critical: bool
    severity: str
    registry_check: str
    injection_flag: bool
    readable: bool
    note: str | None
    job_id: str | None
    tasks: list[str]
    alerts: list[dict[str, Any]]
    timings: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DemoItem:
    kind: str  # letter | seed
    ref: str
    received_at: str
    text: str
    source: str
    filename: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ev(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"value": e.get("value"), "quote": e.get("quote"), "line": e.get("line")} for e in items or []]


class Pipeline:
    """Application service facade used by the CLI, the worker and the web layer."""

    def __init__(self, settings: Settings, *, store: CaseStore | None = None, ref: ReferenceData | None = None,
                 search: KnowledgeSearch | None = None, runtime: AgentRuntime | None = None,
                 notifier: Notifier | None = None, files: FileStore | None = None, jobs: JobQueue | None = None,
                 prices: PriceTable | None = None, telemetry: Telemetry | None = None, clock: Clock | None = None,
                 worker_id: str | None = None) -> None:
        self.settings = settings
        self.clock = clock or (store.clock if store is not None else SystemClock())
        self.store = store if store is not None else rt.get_store(settings, clock=self.clock)
        self.ref = ref or rt.get_reference_data(settings)
        self.search = search or rt.get_knowledge_search(self.ref, settings)
        self.engine = RuleEngine(self.store, self.ref, self.search, clock=self.clock)  # type: ignore[arg-type]
        self.workflow: Workflow = self.engine.workflow
        self.files = files or rt.get_file_store(settings)
        self.notifier = notifier or rt.get_notifier(settings, self.files)
        self.runtime = runtime or rt.get_agent_runtime(settings)
        self.jobs = jobs or rt.get_job_queue(self.store)
        self.prices = prices or rt.get_price_table(settings)
        self.telemetry = telemetry or rt.get_telemetry(settings)
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._alert_lock = threading.Lock()
        self._job_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_beat = 0.0
        self._last_verify = 0.0
        self._overdue_reported: dict[str, float] = {}
        self.timings: dict[str, dict[str, Any]] = {}
        self.last_loop_monotonic: float | None = None
        self._platform_cache: tuple[float, dict[str, Any]] | None = None
        self._last_sweep: float | None = None
        self.sweep_interval_s = rt.deadline_sweep_interval(settings)  # 0 offline (every loop); Azure: RU cost

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> "Pipeline":
        return cls(settings, **kwargs)

    # ------------------------------------------------------------------ helpers
    def now(self) -> _dt.datetime:
        return self.clock.now()

    def _agent_identity(self) -> tuple[str | None, str | None]:
        if self.runtime.backend == "offline":
            return OFFLINE_MODEL, OFFLINE_MODEL_VERSION
        return None, None

    def _agent_meta(self, spec: AgentSpec) -> dict[str, Any]:
        model, version = self._agent_identity()
        if model is None:
            model = self.settings.model_for(spec.model_role)
            version = model_version_of(model)
        return {"agent_version": spec.agent_version, "model": model, "model_version": version,
                "prompt_version": load_prompt(spec).prompt_version, "rule_versions": list(ALL_RULE_VERSIONS),
                "corpus_version": self.search.corpus_version}

    def _link(self, case: Case) -> str:
        return case_link(case, self.settings.public_base_url)

    # ------------------------------------------------------------------ intake
    def ingest_letter(self, *, filename: str | None = None, text: str | None = None,
                      content_base64: str | None = None, content: bytes | None = None,
                      received_at: str | _dt.datetime | None = None, ref: str | None = None,
                      actor: Actor | None = None, source: str = "upload", target_case_id: str | None = None,
                      metadata: dict[str, Any] | None = None) -> IngestResult:
        """Receive one letter (POST /api/letters, seed). Critical detection and the alert run
        before this returns; the analysis is only queued."""
        t0 = time.perf_counter()
        started_utc = iso_utc(_dt.datetime.now(_dt.timezone.utc), ms=True)
        decoded: DecodedUpload = decode_upload(filename, text=text, content_base64=content_base64, content=content)
        received = parse_utc(received_at) if received_at is not None else self.now()
        signal = SignalInput(text=decoded.text, received_at=received, source=source, ref=ref, filename=filename,
                             metadata=dict(metadata or {}), readable=decoded.readable,
                             actor_id=(actor.actor_id if actor else "system"))
        result = self.engine.process_signal(signal, actor=actor, target_case_id=target_case_id)
        t_rules = time.perf_counter()
        timing: dict[str, Any] = {"ingest_start_utc": started_utc, "ingest_start_perf": t0,
                                  "rules_ms": round((t_rules - t0) * 1000, 3), "alerts": [],
                                  "analysis_finished_ms": None, "analysis_finished_utc": None}
        self.timings[result.signal_id] = timing
        alerts = self.dispatch_pending_alerts(case_id=result.case_id, origin_perf=t0, signal_id=result.signal_id)
        timing["alerts"] = alerts
        if decoded.raw is not None:
            self.files.put("letters", f"{result.signal_id}_{safe_filename(filename)}", decoded.raw)
        job = None
        if result.status != Status.NICHT_LESBAR and decoded.readable:
            job = self.jobs.enqueue(result.case_id, result.signal_id, reason=f"new signal {result.signal_id}")
            timing["job_id"] = job.job_id
        return IngestResult(
            signal_id=result.signal_id, case_id=result.case_id, case_key=result.case_key, relation=result.relation,
            status=self.store.get_case(result.case_id).status, critical=result.critical,
            newly_critical=result.newly_critical, severity=result.severity, registry_check=result.registry_check,
            injection_flag=result.injection_flag, readable=decoded.readable, note=decoded.note,
            job_id=job.job_id if job else None, tasks=list(result.tasks), alerts=alerts,
            timings={k: v for k, v in timing.items() if k != "ingest_start_perf"})

    def add_signal(self, case_id: str, actor: Actor, **kwargs: Any) -> IngestResult:
        """A person adds a signal to an existing case (POST /api/cases/{id}/signals)."""
        return self.ingest_letter(actor=actor, target_case_id=case_id, **kwargs)

    # ------------------------------------------------------------------ demo data
    def demo_items(self) -> list[DemoItem]:
        """The 12 letters (receipt times from letters/intake.json) and the 6 seed messages, in received_at order."""
        letters_dir = self.settings.synthetic_data_dir / "letters"
        items = [DemoItem("letter", e.ref, e.received_at, (letters_dir / e.filename).read_text(encoding="utf-8"),
                          "upload", e.filename) for e in load_intake(self.settings.synthetic_data_dir)]
        items += [DemoItem("seed", str(s.ref), iso_utc(parse_utc(s.received_at)), s.text, "seed", None,
                           dict(s.metadata)) for s in self.ref.seed_signals()]
        return sorted(items, key=lambda i: (parse_utc(i.received_at), i.ref))

    def ingest_item(self, item: DemoItem) -> IngestResult:
        return self.ingest_letter(filename=item.filename, text=item.text, received_at=item.received_at, ref=item.ref,
                                  source=item.source, metadata=item.metadata or None)

    def seed_demo(self, actor: Actor | None = None, *, replay_clock: Any | None = None,
                  drain: bool = False) -> dict[str, Any]:
        """Load the demo letters and seed messages once (idempotent). Operators only (concept 7.3)."""
        if actor is not None:
            self.workflow.authorize(actor, Action.LOAD_DEMO_DATA, source="api")
        system = Actor.system()
        findings = self.workflow.import_process_findings(self.ref.process_findings(), system, source="seed")
        loaded = {(s.ref, s.text_sha256) for s in self.store.list_signals()}
        results, skipped = [], []
        for item in self.demo_items():
            if (item.ref, _sha(item.text)) in loaded:
                skipped.append(item.ref)
                continue
            if replay_clock is not None:
                replay_clock.set(item.received_at)
            result = self.ingest_item(item)
            if drain:
                self.drain()
            results.append(result)
        return {"loaded": [r.to_dict() for r in results], "skipped_already_loaded": skipped,
                "process_findings_created": [f.finding_id for f in findings],
                "triggered_by": actor.actor_id if actor else "system"}

    # ------------------------------------------------------------------ alerts
    def dispatch_pending_alerts(self, *, case_id: str | None = None, origin_perf: float | None = None,
                                signal_id: str | None = None, retry_failed: bool = False,
                                min_age_s: float = 0.0) -> list[dict[str, Any]]:
        """Deliver pending triage alerts now (outbox or demo e-mail); one ALERT_SENT record each."""
        background = getattr(self.notifier, "background", None)
        if background is not None and not background.is_sender_thread():
            # Azure e-mail: the caller (an upload) never waits for the e-mail service.
            return self._hand_over_alerts(background, case_id=case_id, origin_perf=origin_perf, signal_id=signal_id,
                                          retry_failed=retry_failed, min_age_s=min_age_s)
        out: list[dict[str, Any]] = []
        with self._alert_lock:
            now = self.now()
            alerts = [a for a in self.store.list_alerts(case_id=case_id, status="PENDING")
                      if (now - parse_utc(a.created_at)).total_seconds() >= min_age_s]
            if retry_failed:
                alerts += [a for a in self.store.list_alerts(case_id=case_id, status="FAILED")
                           if int(a.detail.get("attempts", 0)) < MAX_ALERT_ATTEMPTS]
            for alert in alerts:
                if not self._claim_alert(alert):
                    continue  # another process (web sender or worker) is delivering this alert
                case = self.store.get_case(alert.case_id)
                try:
                    delivery = self.notifier.send_alert(alert, case, link=self._link(case))
                except Exception as exc:  # the alert stays visible and is retried by the worker
                    delivery = Delivery(channel=self.notifier.channel, status="FAILED",
                                        detail={"error": f"{type(exc).__name__}: {exc}"[:300]})
                if origin_perf is not None and delivery.sent_perf is not None:
                    latency = delivery.sent_perf - origin_perf
                else:
                    latency = max(0.0, (self.now() - parse_utc(alert.created_at)).total_seconds())
                detail = {**alert.detail, **delivery.detail, "attempts": int(alert.detail.get("attempts", 0)) + 1,
                          "latency_s": round(latency, 4), "signal_id": signal_id or alert.detail.get("signal_id")}
                self.workflow.mark_alert_sent(alert.alert_id, channel=delivery.channel, status=delivery.status,
                                              detail=detail)
                self.telemetry.alert_sent(case_id=case.case_id, latency_s=latency, channel=delivery.channel,
                                          alert_id=alert.alert_id, status=delivery.status)
                out.append({"alert_id": alert.alert_id, "case_id": case.case_id, "status": delivery.status,
                            "channel": delivery.channel, "latency_ms": round(latency * 1000, 3),
                            "within_target": delivery.status == "SENT" and latency <= ALERT_TARGET_SECONDS,
                            "outbox_entry": delivery.detail.get("outbox_entry")})
        return out

    def _claim_alert(self, alert: Any) -> bool:
        claim = getattr(self.store, "claim_alert", None)  # shared store only (Cosmos DB); SQLite: one process
        return claim is None or bool(claim(alert.alert_id, self.worker_id, ALERT_CLAIM_S))

    def _hand_over_alerts(self, background: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """Queue the delivery for the notifier's sender thread; the result is recorded when it ran."""
        case_id = kwargs.get("case_id")
        pending = self.store.list_alerts(case_id=case_id, status="PENDING") if case_id is not None else []
        background.submit(lambda: self.dispatch_pending_alerts(**kwargs))
        return [{"alert_id": a.alert_id, "case_id": a.case_id, "status": "QUEUED", "channel": self.notifier.channel,
                 "latency_ms": None, "within_target": None, "outbox_entry": None} for a in pending]

    # ------------------------------------------------------------------ analysis queue and worker
    def enqueue_analysis(self, case_id: str, signal_id: str, reason: str) -> Job:
        return self.jobs.enqueue(case_id, signal_id, reason=reason)

    def process_next_job(self) -> Job | None:
        with self._job_lock:
            job = self.jobs.claim_next(self.worker_id)
        if job is None:
            return None
        self.process_job(job)
        return self.jobs.get(job.job_id)

    def drain(self, *, timeout: float = 120.0) -> int:
        """Process (or, with a running worker thread, wait for) all queued analysis jobs."""
        deadline = time.monotonic() + timeout
        processed = 0
        if self._thread is not None and self._thread.is_alive():
            while self.jobs.pending_count() and time.monotonic() < deadline:
                time.sleep(0.02)
            return processed
        while time.monotonic() < deadline and self.process_next_job() is not None:
            processed += 1
        return processed

    def heartbeat(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_beat >= HEARTBEAT_INTERVAL_S:
            self._last_beat = now
            pending = self.jobs.pending_count()
            self.store.set_meta("worker_heartbeat", iso_utc(self.now(), ms=True))
            self.store.set_meta("worker_heartbeat_wall", iso_utc(_dt.datetime.now(_dt.timezone.utc), ms=True))
            self.telemetry.worker_heartbeat(worker_id=self.worker_id, queue=pending, mode=self.settings.worker_mode)
        if force or now - self._last_verify >= AUDIT_VERIFY_INTERVAL_S:
            self._last_verify = now
            result = self.store.audit.verify()
            self.store.set_meta("audit_verify_last", f"{iso_utc(self.now(), ms=True)} ok={result.ok} "
                                                     f"checked={result.checked}")
            self.telemetry.audit_verify(ok=result.ok, records=result.checked, first_break_seq=result.first_break_seq)

    def sweep_deadlines(self, now: _dt.datetime | None = None) -> list[str]:
        """48-hour window sweep (raise-only) and CASE_OVERDUE telemetry; no status ever turns into a decision."""
        at = now or self.now()
        raised = self.engine.check_all_deadline_windows(now=at)
        if raised:
            self.dispatch_pending_alerts()
        mono = time.monotonic()
        for case in self.store.list_cases():
            if Status(case.status) in CLOSED_STATUSES:
                continue
            overview = self.workflow.deadline_overview(case.case_id, at)
            if overview["overdue"] and mono - self._overdue_reported.get(case.case_id, -1e12) >= 86400:
                self._overdue_reported[case.case_id] = mono
                drive = overview["driving"] or {}
                self.telemetry.case_overdue(case_id=case.case_id, due_date=drive.get("due_date"),
                                            rule_set=drive.get("rule_set"))
        return raised

    def run_worker_once(self, *, max_jobs: int | None = None) -> dict[str, Any]:
        self.last_loop_monotonic = time.monotonic()
        self.heartbeat()
        alerts = self.dispatch_pending_alerts(retry_failed=True, min_age_s=ALERT_WORKER_GRACE_S)
        raised = self.sweep_deadlines() if self._sweep_due() else []
        processed = 0
        while max_jobs is None or processed < max_jobs:
            if self.process_next_job() is None:
                break
            processed += 1
        return {"jobs": processed, "alerts": len(alerts), "raised": raised}

    def _sweep_due(self) -> bool:
        now = time.monotonic()
        if self.sweep_interval_s and self._last_sweep is not None and now - self._last_sweep < self.sweep_interval_s:
            return False
        self._last_sweep = now
        return True

    def recover(self) -> int:
        return self.jobs.recover_stale(STALE_JOB_AFTER)

    def start_worker(self, *, poll_s: float = 1.0) -> threading.Thread:
        """In-process background worker (local mode, WORKER_MODE=inprocess)."""
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self.recover()
        self._stop.clear()

        def loop() -> None:
            self.heartbeat(force=True)
            while not self._stop.is_set():
                try:
                    done = self.run_worker_once(max_jobs=1)["jobs"]
                except Exception:  # keep the worker alive; the failure is logged and visible
                    log.exception("worker iteration failed")
                    done = 0
                if not done:
                    self._stop.wait(poll_s)

        self._thread = threading.Thread(target=loop, name="pms-analysis-worker", daemon=True)
        self._thread.start()
        return self._thread

    def stop_worker(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._thread = None

    def close(self) -> None:
        self.stop_worker()
        self.search.close()
        self.store.close()

    # ------------------------------------------------------------------ one analysis job
    def process_job(self, job: Job) -> None:
        progress: dict[str, Any] = {step: {"status": "QUEUED"} for step in STEPS}
        self.jobs.update_progress(job.job_id, progress)
        try:
            signal = self.store.get_signal(job.signal_id)
            if not signal.readable:
                self.jobs.finish(job.job_id, "SKIPPED", error="unreadable input: no analysis (task stays visible)")
                return
            self.workflow.mark_analysis(job.case_id, ANALYSIS_RUNNING)
            ctx: dict[str, Any] = {"job": job, "signal": signal, "progress": progress, "round": 1}
            ctx["A1"] = self._step(ctx, "A1")
            ctx["A2"] = self._step(ctx, "A2")
            ctx["CORR"] = self._correlate(ctx)
            for rnd in range(1, MAX_ANALYSIS_ROUNDS + 1):
                ctx["round"] = rnd
                ctx["A6"] = self._step(ctx, "A6")
                ctx["A3"] = self._step(ctx, "A3")
                if not (ctx["A3"].output or {}).get("needs_more_evidence"):
                    break
            self.jobs.update_progress(job.job_id, progress, rounds=ctx["round"])
            ctx["A4"] = self._step(ctx, "A4")
            self.workflow.mark_analysis(job.case_id, ANALYSIS_DONE)
            self.jobs.finish(job.job_id, "DONE")
        except AnalysisFailed as exc:
            self.workflow.mark_analysis(job.case_id, ANALYSIS_FAILED)
            self.jobs.update_progress(job.job_id, progress)
            self.jobs.finish(job.job_id, "FAILED", error=str(exc))
        except Exception as exc:  # unexpected defect: visible as a failed analysis, never silent
            log.exception("analysis job %s failed", job.job_id)
            try:
                self.workflow.mark_analysis(job.case_id, ANALYSIS_FAILED)
            finally:
                self.jobs.update_progress(job.job_id, progress)
                self.jobs.finish(job.job_id, "FAILED", error=f"{type(exc).__name__}: {exc}"[:500])
        finally:
            final = self.jobs.get(job.job_id).status
            for task in self.store.list_tasks(case_id=job.case_id, status="OPEN", kind=TASK_REANALYSIS):
                self.store.update_task(task.task_id, status="DONE", closed_at=iso_utc(self.now(), ms=True),
                                       closed_by=self.worker_id, close_reason=f"analysis job {job.job_id} {final}")
                self.store.audit.append(event_type=A.TASK_CLOSED, source="system", actor_type="system",
                                        actor_id=self.worker_id, case_id=job.case_id,
                                        before={"task_id": task.task_id, "status": "OPEN"},
                                        after={"status": "DONE", "job_id": job.job_id, "job_status": final},
                                        reason=f"analysis job {job.job_id} {final}", ts=self.now())
            timing = self.timings.get(job.signal_id)
            if timing is not None and timing.get("analysis_finished_ms") is None:
                timing["analysis_finished_ms"] = round((time.perf_counter() - timing["ingest_start_perf"]) * 1000, 3)
                timing["analysis_finished_utc"] = iso_utc(_dt.datetime.now(_dt.timezone.utc), ms=True)
            self.dispatch_pending_alerts(case_id=job.case_id)

    def _set_progress(self, ctx: dict[str, Any], step: str, **values: Any) -> None:
        ctx["progress"][step] = {**ctx["progress"].get(step, {}), **values}
        self.jobs.update_progress(ctx["job"].job_id, ctx["progress"])

    def _correlate(self, ctx: dict[str, Any]) -> dict[str, Any]:
        self._set_progress(ctx, "CORR", status="RUNNING")
        result = self.engine.run_correlation(ctx["job"].case_id, ctx["job"].signal_id)
        self._set_progress(ctx, "CORR", status="DONE", hits=len(result.hits))
        return result.to_dict()

    def _step(self, ctx: dict[str, Any], code: str) -> AgentResult:
        spec = AGENTS[code]
        job = ctx["job"]
        case = self.store.get_case(job.case_id)
        signal: Signal = ctx["signal"]
        meta = self._agent_meta(spec)
        agent = Actor.agent(spec.agent_id)
        self._set_progress(ctx, code, status="RUNNING", round=ctx["round"], started_at=iso_utc(self.now(), ms=True))
        started_clock = self.now()
        tools = ToolBox(store=self.store, ref=self.ref, search=self.search, case_id=case.case_id,
                        signal_id=signal.signal_id, agent_id=spec.agent_id, agent_meta=meta, allowed=spec.tools,
                        clock=self.clock, max_calls=spec.max_tool_calls, max_rounds=spec.max_rounds,
                        max_calls_per_round=spec.max_calls_per_round if self.runtime.backend == "offline" else None)
        agent_input = self._input(code, ctx, case, signal)
        try:
            result = self.runtime.run(spec, agent_input, tools)
        except Exception as exc:  # runtimes should not raise; treat it as a failed run
            result = AgentResult(code=code, agent_id=spec.agent_id, status="FAILED", output=None,
                                 backend=self.runtime.backend, model=meta["model"],
                                 model_version=meta["model_version"], prompt_version=meta["prompt_version"],
                                 agent_version=spec.agent_version, usage=TokenUsage(reported=False),
                                 tool_calls=list(tools.calls), error=f"{type(exc).__name__}: {exc}")
        stored, error = self._postprocess(spec, result, ctx, case)
        status = "DONE" if error is None else "FAILED"
        cost = self.prices.estimate(result.priced_model or meta["model"], result.usage,
                                    simulated=result.backend == "offline")
        run_meta = {**meta, "model": result.model or meta["model"], "model_version": result.model_version or
                    meta["model_version"], "prompt_version": result.prompt_version or meta["prompt_version"]}
        if result.details.get("foundry_agent_version"):
            run_meta["agent_version"] = (f"{spec.agent_version} (Foundry {result.details.get('foundry_agent')} "
                                         f"v{result.details['foundry_agent_version']})")
        run = self.workflow.record_agent_run(
            case.case_id, agent, agent_meta=run_meta, status=status, output=stored, usage=result.usage.to_dict(),
            error=error, signal_id=signal.signal_id, started_at=started_clock, cost_estimate=cost)
        self.workflow.record_cost(case_id=case.case_id, agent_id=spec.agent_id, model=cost["model"],
                                  input_tokens=result.usage.input_tokens,
                                  cached_tokens=result.usage.cached_input_tokens,
                                  output_tokens=result.usage.output_tokens, cost_usd=cost["usd"],
                                  price_table_version=self.prices.version, simulated=cost["simulated"],
                                  run_id=run.run_id)
        self.telemetry.agent_run(
            status=status, agent=spec.agent_id, model=run_meta["model"], duration_ms=result.duration_ms,
            tokens_in=result.usage.input_tokens, tokens_out=result.usage.output_tokens, cost_usd=cost["usd"],
            case_id=case.case_id, signal_id=signal.signal_id, run_id=run.run_id, backend=result.backend,
            model_version=run_meta["model_version"], prompt_version=run_meta["prompt_version"],
            agent_version=spec.agent_version, rule_versions=run_meta["rule_versions"],
            corpus_version=run_meta["corpus_version"], tool_calls=len(result.tool_calls),
            started_utc=iso_utc(result.started_at, ms=True), finished_utc=iso_utc(result.finished_at, ms=True),
            error=error)
        self._set_progress(ctx, code, status=status, run_id=run.run_id, round=ctx["round"],
                           finished_at=iso_utc(self.now(), ms=True), tool_calls=len(result.tool_calls), error=error)
        if error is not None:
            raise AnalysisFailed(f"{spec.agent_id} failed: {error}")
        result.output = stored
        result.details["run_id"] = run.run_id
        if spec.may_raise_urgency:
            outcome = self.workflow.apply_ai_reading(case.case_id, agent, stored.get("ai_severity"), agent_meta=run_meta)
            stored["ai_reading_outcome"] = outcome
            self._set_progress(ctx, code, ai_reading=stored.get("ai_severity"), ai_reading_outcome=outcome)
            if outcome == "raised":
                self.dispatch_pending_alerts(case_id=case.case_id)
        if code == "A3":
            content = {**stored, "run_id": run.run_id, "round": ctx["round"],
                       "a6_run_id": ctx["A6"].details.get("run_id") if ctx.get("A6") else None}
            self.workflow.record_recommendation(case.case_id, agent, content, agent_meta=run_meta, run_id=run.run_id)
        return result

    # ------------------------------------------------------------------ agent inputs
    def _case_brief(self, case: Case) -> dict[str, Any]:
        return {"case_id": case.case_id, "case_key": case.case_key, "status": case.status, "critical": case.critical,
                "rule_severity": case.severity, "product_family": case.product_family,
                "rule_sets": list(case.rule_sets), "authority_labels": list(case.authority_labels),
                "route_unverified": case.route_unverified, "rs_notes": list(case.rs_notes),
                "awareness_anchor": case.awareness_anchor, "anchor_ref": case.anchor_ref}

    @staticmethod
    def _signal_brief(s: Signal) -> dict[str, Any]:
        return {"signal_id": s.signal_id, "ref": s.ref, "received_at": s.received_at, "source": s.source,
                "channel": CHANNEL_LABELS.get(s.source, s.source), "relation": s.relation, "language": s.language,
                "readable": s.readable, "injection_flag": bool(s.injection.get("flag"))}

    @staticmethod
    def _rule_results(s: Signal) -> dict[str, Any]:
        ex = s.extraction or {}
        crit = s.crit or {}
        loc = ex.get("incident_location")
        return {
            "crit": {"severity": crit.get("severity"), "groups": list(crit.get("groups", [])),
                     "indicators": [{"group": i["group"], "term_id": i["term_id"], "match": i["match"],
                                     "quote": i["quote"], "line": i["line"]} for i in crit.get("indicators", [])]},
            "registry_check": {k: s.registry.get(k) for k in ("status", "value", "entry_ids", "details")},
            "event_tags": [{"tag": t["tag"], "term": t["term"], "quote": t["quote"], "line": t["line"]}
                           for t in ex.get("event_tags", [])],
            "extraction": {**{k: _ev(ex.get(k, [])) for k in _EXTRACTION_KEYS},
                           "incident_location": ({"states": list(loc["states"]), "basis": loc["basis"],
                                                  "quote": loc["quote"], "line": loc["line"]} if loc else None),
                           "jurisdictions": [{"code": j["code"], "quote": j["quote"], "line": j["line"]}
                                             for j in ex.get("jurisdictions", [])]},
            "completeness": s.completeness,
        }

    @staticmethod
    def masked_letter(s: Signal) -> str:
        """Letter text for agents: paragraphs flagged by INJ-1.0 replaced by a visible marker."""
        text = s.text or ""
        spans = sorted(tuple(x) for x in (s.injection or {}).get("masked_spans", []))
        out, pos = [], 0
        for start, end in spans:
            out.append(text[pos:start])
            out.append(WITHHELD_MARK)
            pos = end
        out.append(text[pos:])
        return "".join(out)

    def _missing_data(self, case_id: str) -> list[str]:
        sigs = [s for s in self.store.list_signals(case_id) if s.readable]
        if not sigs:
            return ["letter text (unreadable input)"]
        missing = None
        for s in sigs:
            m = set((s.completeness or {}).get("missing", []))
            missing = m if missing is None else missing & m
        return [f for f in ("product_family", "market", "device_model_or_version", "lot_or_udi", "patient_reference",
                            "patient_exposure", "event_date", "event_description", "outcome", "cause")
                if f in (missing or set())]

    def _latest_output(self, case_id: str, agent_id: str, signal_id: str | None = None) -> dict[str, Any] | None:
        runs = [r for r in self.store.list_agent_runs(case_id) if r.agent_id == agent_id and r.status == "DONE"
                and (signal_id is None or r.signal_id == signal_id)]
        return runs[-1].output if runs else None

    def _input(self, code: str, ctx: dict[str, Any], case: Case, signal: Signal) -> AgentInput:
        brief = self._case_brief(case)
        letter = LetterBlock(signal.ref, self.masked_letter(signal))
        a1 = (ctx.get("A1").output if ctx.get("A1") else None) or {}
        a2 = (ctx.get("A2").output if ctx.get("A2") else None) or {}
        if code == "A1":
            payload = {"agent": code, "case": brief, "signal": self._signal_brief(signal),
                       "rule_results": self._rule_results(signal)}
            return AgentInput(case.case_id, signal.signal_id, payload, [letter])
        if code == "A2":
            rr = self._rule_results(signal)
            payload = {"agent": code, "case": brief, "signal": self._signal_brief(signal),
                       "rule_results": {"registry_check": rr["registry_check"], "extraction": rr["extraction"]},
                       "a1_facts": a1.get("facts", [])}
            return AgentInput(case.case_id, signal.signal_id, payload, [letter])
        corr = ctx.get("CORR") or {}
        if code == "A6":
            keys = corr.get("keys", {})
            payload = {"agent": code, "round": ctx["round"], "case": brief, "signal": self._signal_brief(signal),
                       "event_tags": list(corr.get("event_tags", [])),
                       "identifiers": {"family": keys.get("family"), "models": keys.get("models", []),
                                       "lots": keys.get("lots", [])},
                       "corr_1_0": {k: corr.get(k) for k in ("status", "hits", "counts", "note")},
                       "a1_facts": a1.get("facts", []),
                       "a2": {"registry_status": (a2.get("registry_check") or {}).get("status"),
                              "deviations": [d["detail"] for d in a2.get("deviations", [])]},
                       "a3_previous": (ctx["A3"].output if ctx.get("A3") else None),
                       "missing_data": self._missing_data(case.case_id),
                       "limits": {"max_tool_calls": 8, "max_rounds": 2}}
            return AgentInput(case.case_id, signal.signal_id, payload, [], round=ctx["round"])
        sigs = self.store.list_signals(case.case_id)
        if code == "A3":
            signals = []
            corr_all = []
            for s in sigs:
                rr = self._rule_results(s)
                signals.append({**self._signal_brief(s), "crit": rr["crit"], "registry_check": rr["registry_check"],
                                "event_tags": rr["event_tags"], "completeness": s.completeness,
                                "models": [e["value"] for e in rr["extraction"]["models"]]})
                found = self.store.list_correlations(case_id=case.case_id, signal_id=s.signal_id)
                if found:
                    corr_all.append({"signal_ref": s.ref, **{k: found[-1].result.get(k)
                                                            for k in ("status", "hits", "counts", "note")}})
            facts = []
            for s in sigs:
                out = a1 if s.signal_id == signal.signal_id else (
                    self._latest_output(case.case_id, AGENTS["A1"].agent_id, s.signal_id) or {})
                facts += [{**f, "signal_ref": s.ref} for f in out.get("facts", [])]
            payload = {"agent": code, "round": ctx["round"], "case": brief, "signals": signals, "a1_facts": facts,
                       "a2": a2, "a6": (ctx["A6"].output if ctx.get("A6") else None), "corr_1_0": corr_all,
                       "missing_data": self._missing_data(case.case_id), "causality_scale": list(CAUSALITY_SCALE),
                       "imdrf_allowed": {c: {"annex": v["annex"], "term": v["term"]} for c, v in IMDRF_CODES.items()}}
            letters = [LetterBlock(s.ref, self.masked_letter(s)) for s in sigs if s.readable]
            return AgentInput(case.case_id, signal.signal_id, payload, letters, round=ctx["round"])
        overview = self.workflow.deadline_overview(case.case_id)
        a3 = (ctx["A3"].output if ctx.get("A3") else None) or {}
        payload = {"agent": code, "case": brief, "deadlines": overview["candidates"], "driving": overview["driving"],
                   "clock": overview["clock"], "a1_facts": a1.get("facts", []),
                   "a3": {k: a3.get(k) for k in ("causality", "expectedness", "imdrf_candidates", "missing_data")},
                   "rule_versions": [DL, RS]}
        return AgentInput(case.case_id, signal.signal_id, payload, [letter])

    # ------------------------------------------------------------------ output checks (app code, not the model)
    def _postprocess(self, spec: AgentSpec, result: AgentResult, ctx: dict[str, Any],
                     case: Case) -> tuple[dict[str, Any], str | None]:
        envelope = {**draft_envelope(spec, result.details.get("generator") or f"{result.backend}: {result.model}"),
                    "tool_calls": [c.to_dict() for c in result.tool_calls],
                    "backend_details": {k: v for k, v in result.details.items() if k != "generator"}}
        if result.status != "DONE" or result.output is None:
            return {**envelope, "error": result.error}, result.error or "agent run failed"
        output = dict(result.output)
        checks: dict[str, Any] = {}
        if spec.code == "A3":
            output, checks = self._sanitize_imdrf(output)
        errors = validate_schema(output, spec.output_schema)
        if errors:
            return ({**envelope, "schema_errors": errors[:20]},
                    f"output does not match the {spec.code} schema: {'; '.join(errors[:3])}")
        if spec.code == "A1":
            checks.update(self._check_a1(output, ctx["signal"]))
        elif spec.code == "A2":
            checks.update(self._check_a2(output, ctx["signal"]))
        elif spec.code == "A6":
            checks.update(self._check_quotes(output.get("correlations", []), case.case_id))
        elif spec.code == "A3":
            checks.update(self._check_quotes(output.get("evidence_for", []) + output.get("evidence_against", []),
                                             case.case_id))
            output["not_clinically_validated"] = True
            output["causality_note"] = CAUSALITY_NOTE
        elif spec.code == "A4":
            checks.update(self._check_a4(output, case.case_id))
        return {**output, **envelope, "app_checks": checks}, None

    @staticmethod
    def _sanitize_imdrf(output: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        kept, removed = [], []
        for cand in output.get("imdrf_candidates") or []:
            code = cand.get("code") if isinstance(cand, dict) else None
            if code in IMDRF_CODES and code not in [k["code"] for k in kept]:
                kept.append({"code": code, "basis": str(cand.get("basis", ""))[:400], "quote": cand.get("quote")})
            else:
                removed.append(code)
        output = {**output, "imdrf_candidates": kept}
        checks: dict[str, Any] = {"imdrf_removed": removed}
        return output, checks

    @staticmethod
    def quote_in_letter(signal: Signal, quote: str | None) -> bool:
        """Verbatim in the original letter and not inside a paragraph withheld by INJ-1.0."""
        if not quote:
            return False
        text = signal.text or ""
        spans = [tuple(x) for x in (signal.injection or {}).get("masked_spans", [])]
        start = text.find(quote)
        while start != -1:
            end = start + len(quote)
            if not any(s < end and start < e for s, e in spans):
                return True
            start = text.find(quote, start + 1)
        return False

    def _check_a1(self, output: dict[str, Any], signal: Signal) -> dict[str, Any]:
        verified = unverified = 0
        for fact in output.get("facts", []):
            if fact.get("quote"):
                ok = self.quote_in_letter(signal, fact["quote"])
                fact["quote_verified"] = ok
                fact["verification"] = "verbatim in the letter" if ok else "unverified ('unbelegt') - quote not found"
                verified += ok
                unverified += not ok
            else:
                fact["quote_verified"] = None
                fact["verification"] = "no quote - value unknown" if fact.get("value") is None else "no quote"
        return {"quotes_verified": verified, "quotes_unverified": unverified}

    def _check_a2(self, output: dict[str, Any], signal: Signal) -> dict[str, Any]:
        rule = {"status": signal.registry.get("status"), "value": signal.registry.get("value")}
        if output.get("registry_check") != rule:
            output["registry_check"] = rule
            return {"registry_check": "replaced with the REG-1.0 result (rule code is authoritative)"}
        return {"registry_check": "consistent with REG-1.0"}

    def _resolve_source(self, source_id: str, case_id: str) -> tuple[bool, str | None]:
        """(known source?, verbatim text for quote checks)."""
        if source_id.startswith("LTR:"):
            parts = source_id.split(":")
            sig = next((s for s in self.store.list_signals(case_id) if len(parts) > 1 and s.ref == parts[1]), None)
            return sig is not None, (sig.text if sig else None)
        if source_id.startswith("CASE:"):
            return self.store.find_case_by_key(source_id[5:]) is not None, None
        if source_id in ALL_RULE_VERSIONS:
            return True, None
        text = self.ref.source_text(source_id)
        return text is not None, text

    def _check_quotes(self, items: list[dict[str, Any]], case_id: str) -> dict[str, Any]:
        verified = unverified = unknown = 0
        for item in items:
            source_id = item.get("source_id")
            known, text = self._resolve_source(source_id, case_id) if source_id else (True, None)
            if source_id and not known:
                unknown += 1
                item["source_check"] = "unknown source - not in the corpus, registry, dossiers or cases"
            quote = item.get("quote")
            if quote:
                if source_id and source_id.startswith("LTR:"):
                    sig = next((s for s in self.store.list_signals(case_id) if s.ref == source_id.split(":")[1]), None)
                    ok = bool(sig and self.quote_in_letter(sig, quote))
                else:
                    ok = bool(text and quote in text)
                item["quote_verified"] = ok
                verified += ok
                unverified += not ok
            else:
                item["quote_verified"] = None
        return {"quotes_verified": verified, "quotes_unverified": unverified, "unknown_sources": unknown}

    def _check_a4(self, output: dict[str, Any], case_id: str) -> dict[str, Any]:
        overview = self.workflow.deadline_overview(case_id)
        replaced = []
        for draft in output.get("report_drafts", []):
            mine = [c for c in overview["candidates"] if c["rule_set"] == draft["rule_set"]
                    and c["status"] in ("OPEN", "OVERDUE")]
            drive = min(mine, key=lambda c: (c["due_end_utc"], c["days"])) if mine else None
            rule = (drive["due_date"], drive["basis"], drive["days"]) if drive else (None, None, None)
            if (draft.get("driving_due_date"), draft.get("deadline_basis"), draft.get("deadline_days")) != rule:
                draft["driving_due_date"], draft["deadline_basis"], draft["deadline_days"] = rule
                replaced.append(draft["rule_set"])
            draft["status"] = "draft - not submitted; nothing is sent"
        return {"deadlines_from_rule_code": True, "deadlines_replaced": replaced,
                "rule_sets_without_draft": [r for r in self.store.get_case(case_id).rule_sets
                                            if r not in [d["rule_set"] for d in output.get("report_drafts", [])]]}

    # ------------------------------------------------------------------ human actions (wrappers)
    def take_triage(self, case_id: str, actor: Actor, reason: str | None = None, **kw: Any) -> Case:
        return self.workflow.take_triage(case_id, actor, reason, **kw)

    def start_review(self, case_id: str, actor: Actor, reason: str | None = None, **kw: Any) -> Case:
        return self.workflow.start_review(case_id, actor, reason, **kw)

    def decide(self, case_id: str, actor: Actor, decision: str, reason: str, *, reason_code: str | None = None,
               source: str = "api") -> Any:
        return self.workflow.decide(case_id, actor, decision, reason, reason_code=reason_code, source=source)

    def record_submission(self, case_id: str, actor: Actor, **kw: Any) -> Any:
        return self.workflow.record_submission(case_id, actor, **kw)

    def edit_metadata(self, case_id: str, actor: Actor, field_name: str, new_value: Any, reason: str,
                      **kw: Any) -> Any:
        return self.workflow.edit_metadata(case_id, actor, field_name, new_value, reason, **kw)

    def raise_urgency(self, case_id: str, actor: Actor, reason: str | None = None, *, source: str = "api") -> Case:
        case = self.workflow.raise_urgency(case_id, actor, reason, source=source)
        self.dispatch_pending_alerts(case_id=case_id)
        return case

    def lower_urgency(self, case_id: str, actor: Actor, reason: str, *, source: str = "api") -> Case:
        return self.workflow.lower_urgency(case_id, actor, reason, source=source)

    def classify_anticipated(self, case_id: str, actor: Actor, reason: str, **kw: Any) -> list[str]:
        return self.workflow.classify_deterioration_anticipated(case_id, actor, reason, **kw)

    def request_reanalysis(self, case_id: str, actor: Actor, reason: str | None = None, *,
                           source: str = "api") -> Job:
        readable = [s for s in self.store.list_signals(case_id) if s.readable]
        if not readable:
            raise ValueError("the case has no readable signal to analyse - upload a readable letter instead")
        case = self.store.get_case(case_id)
        if case.analysis_state == ANALYSIS_FAILED and case.status in (Status.IN_ANALYSE, Status.TRIAGE_ERFORDERLICH):
            # Manual continuation after a failed analysis (REQ-035): the status stays as it is (a critical case
            # keeps waiting for its triage); the request is audited as a visible re-analysis task.
            self.workflow.authorize(actor, Action.RERUN_ANALYSIS, case_id=case_id, source=source)
            text = (reason or "").strip() or f"re-analysis requested by {actor.actor_id}"
            self.workflow.create_task(case_id, TASK_REANALYSIS, f"Re-analysis after a failed run: {text}",
                                      actor=actor, source=source, now=self.now(), detail={"reason": text})
        else:
            case = self.workflow.request_reanalysis(case_id, actor, reason, source=source)
        return self.jobs.enqueue(case.case_id, readable[-1].signal_id,
                                 reason=f"re-analysis requested by {actor.actor_id}")

    # ------------------------------------------------------------------ views for the web layer
    def analysis_state_of(self, case: Case, jobs: list[Job] | None = None) -> str:
        """The analysis state people and API clients see. The stored state belongs to the last job the worker
        touched; a job of the case that is still queued or running (a new signal, a re-analysis) wins, so a case
        never shows DONE while the analysis of its newest signal is pending."""
        if jobs is None:
            jobs = self.jobs.list(case_id=case.case_id)
        open_states = {j.status for j in jobs if j.status in ("QUEUED", "RUNNING")}
        if "RUNNING" in open_states:
            return ANALYSIS_RUNNING
        if open_states:
            return ANALYSIS_PENDING
        return case.analysis_state

    def case_progress(self, case_id: str) -> dict[str, Any]:
        jobs = self.jobs.list(case_id=case_id)
        case = self.store.get_case(case_id)
        latest = jobs[-1] if jobs else None
        return {"analysis_state": self.analysis_state_of(case, jobs), "jobs": len(jobs),
                "latest_job": None if latest is None else {
                    "job_id": latest.job_id, "signal_id": latest.signal_id, "status": latest.status,
                    "reason": latest.reason, "rounds": latest.rounds, "error": latest.error,
                    "enqueued_at": latest.enqueued_at, "finished_at": latest.finished_at},
                "steps": {step: (latest.progress.get(step, {}) if latest else {"status": "NOT_QUEUED"})
                          for step in STEPS}}

    def _case_row(self, case: Case, now: _dt.datetime) -> dict[str, Any]:
        overview = self.workflow.deadline_overview(case.case_id, now)
        drive = overview["driving"] or {}
        tasks = self.store.list_tasks(case_id=case.case_id, status="OPEN")
        signals = self.store.list_signals(case.case_id)
        return {
            "case_id": case.case_id, "case_key": case.case_key, "status": case.status,
            "status_label": STATUS_LABELS_EN.get(Status(case.status), case.status), "critical": case.critical,
            "severity": case.severity, "product_family": case.product_family, "rule_sets": list(case.rule_sets),
            "authority_labels": list(case.authority_labels), "route_unverified": case.route_unverified,
            "driving_due_date": drive.get("due_date"), "driving_rule_set": drive.get("rule_set"),
            "countdown_seconds": drive.get("seconds_remaining"), "overdue": overview["overdue"],
            "clock": overview["clock"], "progress": self.case_progress(case.case_id),
            "open_tasks": [{"task_id": t.task_id, "kind": t.kind, "title": t.title} for t in tasks],
            "signals": len(signals), "injection_flag": any(s.injection.get("flag") for s in signals),
            "registry_checks": [s.registry.get("status") for s in signals], "updated_at": case.updated_at,
        }

    def dashboard(self, now: _dt.datetime | None = None) -> dict[str, Any]:
        """Critical queue first (earliest deadline countdown, overdue on top), then all other cases."""
        at = now or self.now()
        rows = [self._case_row(c, at) for c in self.store.list_cases()]
        key = lambda r: (r["countdown_seconds"] is None, r["countdown_seconds"] or 0, r["case_key"])  # noqa: E731
        critical = sorted([r for r in rows if r["critical"] or r["status"] == Status.TRIAGE_ERFORDERLICH], key=key)
        others = sorted([r for r in rows if r not in critical], key=key)
        return {"generated_at": iso_utc(at), "critical_queue": critical, "cases": others,
                "rejected": self.rejected_view(), "unreadable_unmatched": self.unreadable_unmatched_view(),
                "counts": {"cases": len(rows), "critical": len(critical),
                           "overdue": sum(1 for r in rows if r["overdue"]),
                           "analysis_pending": self.jobs.pending_count()}}

    def rejected_view(self) -> list[dict[str, Any]]:
        out = []
        for d in self.store.list_decisions(kind="reject"):
            case = self.store.get_case(d.case_id)
            out.append({"case_id": d.case_id, "case_key": case.case_key, "current_status": case.status,
                        "reason_code": d.reason_code, "reason_code_label": REJECT_REASON_CODES.get(d.reason_code or ""),
                        "reason": d.reason, "reviewer": d.actor_id, "ts": d.ts})
        return out

    def unreadable_unmatched_view(self) -> list[dict[str, Any]]:
        out = []
        for kind in (TASK_UNREADABLE, TASK_UNMATCHED):
            for t in self.store.list_tasks(kind=kind):
                signal = self.store.get_signal(t.detail["signal_id"]) if t.detail.get("signal_id") else None
                out.append({"task_id": t.task_id, "kind": t.kind, "status": t.status, "title": t.title,
                            "case_id": t.case_id, "signal_id": signal.signal_id if signal else None,
                            "signal_ref": signal.ref if signal else None,
                            "filename": signal.filename if signal else None,
                            "registry_check": signal.registry.get("value") if signal else None,
                            "created_at": t.created_at})
        return out

    def stored_originals(self, case_id: str) -> list[dict[str, Any]]:
        ids = {s.signal_id for s in self.store.list_signals(case_id)}
        return [{"signal_id": f.name.split("_", 1)[0], "name": f.name, "uri": f.uri, "size": f.size}
                for f in self.files.list("letters") if f.name.split("_", 1)[0] in ids]

    def case_view(self, case_id: str, now: _dt.datetime | None = None) -> dict[str, Any]:
        """Everything of the case view: original letters, all signals, facts, registry, drafts, deadlines, audit."""
        at = now or self.now()
        case = self.store.get_case(case_id)
        signals = self.store.list_signals(case_id)
        runs = self.store.list_agent_runs(case_id)
        rec = self.store.latest_recommendation(case_id)
        progress = self.case_progress(case_id)
        return {
            "case": {**asdict(case), "analysis_state": progress["analysis_state"],
                     "status_label": STATUS_LABELS_EN.get(Status(case.status), case.status)},
            "signals": [{**self._signal_brief(s), "filename": s.filename, "text": s.text,
                         "text_sha256": s.text_sha256, "registry": s.registry, "injection": s.injection,
                         "crit": {"severity": s.crit.get("severity"), "groups": s.crit.get("groups"),
                                  "indicators": [{"group": i.get("group"), "match": i.get("match"),
                                                  "line": i.get("line")} for i in s.crit.get("indicators") or []]},
                         "completeness": s.completeness, "deadline_not_reset": s.deadline_not_reset,
                         "link": s.link} for s in signals],
            "originals": self.stored_originals(case_id),
            "facts": [asdict(f) for f in self.store.list_facts(case_id)],
            "agent_runs": [{"run_id": r.run_id, "agent_id": r.agent_id, "status": r.status, "signal_id": r.signal_id,
                            "model": r.model, "model_version": r.model_version, "prompt_version": r.prompt_version,
                            "agent_version": r.agent_version, "started_at": r.started_at,
                            "finished_at": r.finished_at, "draft": r.draft, "usage": r.usage, "error": r.error,
                            "output": r.output} for r in runs],
            "recommendation": None if rec is None else {"rec_id": rec.rec_id, "version": rec.version,
                                                        "draft": rec.draft, "content": rec.content,
                                                        "content_hash": rec.content_hash},
            "correlations": [asdict(c) for c in self.store.list_correlations(case_id=case_id)],
            "deadlines": self.workflow.deadline_overview(case_id, at),
            "tasks": [asdict(t) for t in self.store.list_tasks(case_id=case_id)],
            "alerts": [asdict(a) for a in self.store.list_alerts(case_id=case_id)],
            "decisions": [asdict(d) for d in self.store.list_decisions(case_id=case_id)],
            "approvals": [asdict(a) for a in self.store.list_approvals(case_id)],
            "submissions": [asdict(s) for s in self.store.list_submissions(case_id)],
            "metadata_edits": [asdict(e) for e in self.store.list_metadata_edits(case_id)],
            "progress": progress,
            "audit": self.store.audit.records(case_id=case_id),
        }

    def trend_view(self) -> dict[str, Any]:
        return a5_trend_view(self.ref)

    def platform_costs(self, *, max_age_s: float = 600.0) -> dict[str, Any]:
        """Platform costs (Azure Cost Management or simulated offline), cached for ``max_age_s``."""
        mono = time.monotonic()
        if self._platform_cache is not None and mono - self._platform_cache[0] < max_age_s:
            return self._platform_cache[1]
        data = costs_mod.platform_costs(self.settings, self.now())
        self._platform_cache = (mono, data)
        return data

    def cost_overview(self) -> dict[str, Any]:
        """Data of the cost view (GET /api/costs)."""
        return costs_mod.cost_overview(self.store, self.settings, self.prices, self.now(),
                                       platform=self.platform_costs())

    def outbox_entries(self) -> list[dict[str, Any]]:
        entries = getattr(self.notifier, "entries", None)
        return entries() if callable(entries) else []

    def audit_records(self, *, case_id: str | None = None, event_type: str | None = None,
                      limit: int | None = None) -> list[dict[str, Any]]:
        """Audit records for GET /api/audit (read-only; there is no API to change or delete them)."""
        return self.store.audit.records(case_id=case_id, event_type=event_type, limit=limit)

    def verify_audit(self) -> dict[str, Any]:
        result = self.store.audit.verify()
        self.telemetry.audit_verify(ok=result.ok, records=result.checked, first_break_seq=result.first_break_seq)
        return result.to_dict()

    def versions(self) -> dict[str, Any]:
        return {**manifest(self.settings.synthetic_data_dir), "agents": agent_catalogue(self.settings),
                "price_table": self.prices.version, "agent_backend": self.runtime.backend}

    def healthz(self) -> dict[str, Any]:
        return {"status": "ok", "versions": self.versions(), "worker_mode": self.settings.worker_mode,
                "app_env": self.settings.app_env}

    def alert_timings(self) -> dict[str, dict[str, Any]]:
        return {sid: {k: v for k, v in t.items() if k != "ingest_start_perf"} for sid, t in self.timings.items()}


def build_pipeline(settings: Settings | None = None, **kwargs: Any) -> Pipeline:
    """Pipeline from the environment (CLI, worker, web)."""
    return Pipeline(settings or rt.get_settings(), **kwargs)
