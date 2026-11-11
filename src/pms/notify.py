"""Critical-case notifications (concept 6.4 step 3, 13; ADR-006; D-18).

* Offline: :class:`OutboxNotifier` writes one ``.eml`` message and one ``.json`` entry per alert
  (file names start with a UTC timestamp) through the file store into ``DATA_DIR/outbox``.
* Azure: :class:`AcsEmailNotifier` sends an e-mail with Azure Communication Services (managed
  identity, lazy import) ONLY to the configured demo recipients (``ALERT_RECIPIENTS``) and keeps a
  copy of every message in the file store (Blob container ``outbox``).

The e-mail never blocks an upload: in Azure the notifier carries a :class:`BackgroundAlertSender`.
The upload records the alert (PENDING row, ALERT_REQUESTED audit record, triage task) inside the rule
transaction and returns; :meth:`pms.pipeline.Pipeline.dispatch_pending_alerts` hands the delivery to the
sender thread, which sends the e-mail and records the result (one ALERT_SENT audit record per attempt,
SENT or FAILED). The worker container retries alerts that are still PENDING after a grace period or
FAILED; a delivery claim in the shared store keeps two processes from sending the same alert.

A message contains only the case id, the critical reason and a link - never letter text. No code
path sends anything to an authority: authorities are labels only (concept 3.4, 13).
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from email.message import EmailMessage
from typing import Any, Callable, Iterable

from .interfaces import Delivery, FileStore
from .models import Alert, Case, iso_utc, utcnow

log = logging.getLogger("pms.notify")

OUTBOX_AREA = "outbox"
#: ACS final states that mean "not delivered"; NotStarted/Running after the wait = accepted by the service.
ACS_FAILED_STATES: frozenset[str] = frozenset({"failed", "canceled", "cancelled"})
LOCAL_RECIPIENT = "triage-demo@outbox.invalid"  # local outbox only - nothing is sent
SUBJECT_PREFIX = "[PMS demo - synthetic data]"
#: Defence in depth: generic government / EU-institution mail domains are never accepted as recipients.
_PUBLIC_AUTHORITY_DOMAIN_RE = re.compile(r"(\.gov(\.[a-z]{2})?|\.gouv\.[a-z]{2}|\.gv\.[a-z]{2}|\.admin\.ch|"
                                         r"europa\.eu|\.nhs\.uk|\.bund\.de)$", re.IGNORECASE)


class RecipientRefused(ValueError):
    """A recipient is not a configured demo recipient (or looks like a public authority)."""


def check_recipients(recipients: Iterable[str], configured: Iterable[str]) -> list[str]:
    """Only configured demo recipients; never a public-authority domain."""
    allowed = {r.strip().lower() for r in configured if r.strip()}
    out = []
    for raw in recipients:
        address = raw.strip()
        domain = address.rsplit("@", 1)[-1] if "@" in address else ""
        if not domain:
            raise RecipientRefused(f"invalid recipient {address!r}")
        if _PUBLIC_AUTHORITY_DOMAIN_RE.search(domain):
            raise RecipientRefused(f"recipient domain {domain!r} looks like a public authority - refused")
        if address.lower() not in allowed:
            raise RecipientRefused(f"{address!r} is not a configured demo recipient (ALERT_RECIPIENTS)")
        out.append(address)
    return out


def case_link(case: Case, base_url: str | None) -> str:
    return f"{base_url.rstrip('/')}/cases/{case.case_id}" if base_url else f"/cases/{case.case_id}"


def alert_message(alert: Alert, case: Case, *, link: str, sender: str, recipients: list[str]) -> EmailMessage:
    """The alert e-mail: case id, critical reason and link only (no letter text)."""
    msg = EmailMessage()
    msg["Subject"] = f"{SUBJECT_PREFIX} Critical case {case.case_key} - human triage required"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["X-PMS-Alert-Id"] = alert.alert_id
    msg["X-PMS-Case-Id"] = case.case_id
    msg.set_content("\n".join([
        f"Case: {case.case_key} ({case.case_id})",
        f"Critical reason: {alert.reason}",
        f"Link: {link}",
        "",
        "A human must take the triage. Only the medical reviewer decides; nothing has been sent to an authority.",
        "Synthetic demo data - the demo illustrates workflows and makes no compliance statement.",
    ]))
    return msg


class OutboxNotifier:
    """Local outbox folder (offline mode). Nothing leaves the machine."""

    channel = "outbox"

    def __init__(self, files: FileStore, *, recipients: Iterable[str] = (), base_url: str | None = None) -> None:
        self.files = files
        self.recipients = list(recipients) or [LOCAL_RECIPIENT]
        self.base_url = base_url

    def send_alert(self, alert: Alert, case: Case, *, link: str | None = None) -> Delivery:
        now = utcnow()
        stamp = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}Z"
        stem = f"{stamp}_{alert.alert_id}_{case.case_id}"
        url = link or case_link(case, self.base_url)
        msg = alert_message(alert, case, link=url, sender="pms-demo@outbox.invalid", recipients=self.recipients)
        eml = self.files.put(OUTBOX_AREA, f"{stem}.eml", bytes(msg), content_type="message/rfc822")
        entry = {"alert_id": alert.alert_id, "case_id": case.case_id, "case_key": case.case_key,
                 "reason": alert.reason, "link": url, "written_at_utc": iso_utc(now, ms=True),
                 "recipients": self.recipients, "channel": self.channel, "eml": eml.name,
                 "note": "local outbox - nothing is sent"}
        meta = self.files.put(OUTBOX_AREA, f"{stem}.json",
                              json.dumps(entry, ensure_ascii=False, indent=2).encode("utf-8"),
                              content_type="application/json")
        return Delivery(channel=self.channel, status="SENT", sent_perf=time.perf_counter(),
                        detail={"outbox_entry": meta.name, "outbox_eml": eml.name, "outbox_uri": meta.uri,
                                "written_at_utc": entry["written_at_utc"]})

    def entries(self) -> list[dict[str, Any]]:
        return outbox_entries(self.files)


def outbox_entries(files: FileStore | None) -> list[dict[str, Any]]:
    """The ``.json`` entries of the outbox area, oldest first."""
    if files is None:
        return []
    out = []
    for stored in files.list(OUTBOX_AREA):
        if stored.name.endswith(".json"):
            out.append(json.loads(files.get(OUTBOX_AREA, stored.name).decode("utf-8")))
    return sorted(out, key=lambda e: e["written_at_utc"])


class BackgroundAlertSender:
    """One daemon thread per process that runs alert deliveries handed over by the upload path.

    The upload never waits for the e-mail service; if the process stops before a delivery ran, the alert
    stays PENDING in the shared store and the worker delivers it after its grace period."""

    def __init__(self, name: str = "pms-alert-sender") -> None:
        self.name = name
        self._queue: queue.Queue[Callable[[], Any] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._pending = 0
        self.submitted = 0
        self.failures = 0
        self.last_error: str | None = None

    def submit(self, task: Callable[[], Any]) -> None:
        with self._lock:
            self._pending += 1
            self.submitted += 1
            self._idle.clear()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
                self._thread.start()
        self._queue.put(task)

    def _run(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                return
            try:
                task()
            except Exception as exc:  # the alert stays PENDING/FAILED and visible; the worker retries
                self.failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.exception("background alert delivery failed")
            finally:
                with self._lock:
                    self._pending -= 1
                    if self._pending <= 0:
                        self._pending = 0
                        self._idle.set()

    def is_sender_thread(self) -> bool:
        return self._thread is not None and threading.current_thread() is self._thread

    def drain(self, timeout: float = 30.0) -> bool:
        """Wait until every handed-over delivery ran (tests, orderly shutdown)."""
        return self._idle.wait(timeout)

    def close(self, timeout: float = 5.0) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._queue.put(None)
            thread.join(timeout)

    def describe(self) -> dict[str, Any]:
        return {"thread": self.name, "running": bool(self._thread and self._thread.is_alive()),
                "pending": self._pending, "submitted": self.submitted, "failures": self.failures,
                "last_error": self.last_error}


class AcsEmailNotifier:
    """Azure Communication Services e-mail to configured demo recipients only (managed identity).

    With ``background`` (Azure runtime) deliveries run in the sender thread, never in the upload request.
    With ``files`` every message is also kept as an outbox copy (``.eml`` + ``.json``, Blob ``outbox``)."""

    channel = "acs-email"

    def __init__(self, *, endpoint: str, sender: str, recipients: Iterable[str], client_id: str | None = None,
                 base_url: str | None = None, client_factory: Callable[[], Any] | None = None,
                 timeout_s: float = 30.0, background: BackgroundAlertSender | None = None,
                 files: FileStore | None = None) -> None:
        self.endpoint = endpoint
        self.sender = sender
        self.recipients = check_recipients(list(recipients), list(recipients))
        if not self.recipients:
            raise RecipientRefused("ALERT_RECIPIENTS is empty: no demo recipient configured")
        self.client_id = client_id
        self.base_url = base_url
        self._client_factory = client_factory
        self.timeout_s = timeout_s
        self.background = background
        self.files = files
        self._client_obj: Any = None
        self._client_lock = threading.Lock()

    @property
    def asynchronous(self) -> bool:
        return self.background is not None

    def _client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        with self._client_lock:
            if self._client_obj is None:
                from azure.communication.email import EmailClient  # type: ignore[import-not-found]

                from .azure.identity import get_credential

                # in Azure: ManagedIdentityCredential(client_id=AZURE_CLIENT_ID) (INFRA_NOTES 5.7)
                self._client_obj = EmailClient(self.endpoint, get_credential(self.client_id))
            return self._client_obj

    @staticmethod
    def _result_fields(result: Any) -> tuple[str, Any, Any]:
        """(status, operation id, error) of the poller result; NotStarted/Running = accepted, still in flight."""
        if result is None:
            return "", None, None

        def get(name: str) -> Any:
            return result.get(name) if isinstance(result, dict) else getattr(result, name, None)

        return str(get("status") or ""), get("id"), get("error")

    def send_alert(self, alert: Alert, case: Case, *, link: str | None = None) -> Delivery:
        url = link or case_link(case, self.base_url)
        recipients = check_recipients(self.recipients, self.recipients)
        msg = alert_message(alert, case, link=url, sender=self.sender, recipients=recipients)
        payload = {
            "senderAddress": self.sender,
            "recipients": {"to": [{"address": r} for r in recipients]},
            "content": {"subject": str(msg["Subject"]), "plainText": msg.get_content()},
        }
        try:
            poller = self._client().begin_send(payload)
            result = poller.result(timeout=self.timeout_s) if hasattr(poller, "result") else poller
        except Exception as exc:  # delivery failure stays visible; the worker retries
            delivery = Delivery(channel=self.channel, status="FAILED",
                                detail={"error": f"{type(exc).__name__}: {exc}"[:300]})
            self._keep_copy(alert, case, url, msg, recipients, delivery)
            return delivery
        status, message_id, error = self._result_fields(result)
        ok = status.lower() not in ACS_FAILED_STATES
        detail: dict[str, Any] = {"acs_status": status or "accepted", "recipients": len(recipients),
                                  "message_id": message_id}
        if error:
            detail["error"] = str(error)[:300]
        delivery = Delivery(channel=self.channel, status="SENT" if ok else "FAILED", sent_perf=time.perf_counter(),
                            detail=detail)
        self._keep_copy(alert, case, url, msg, recipients, delivery)
        return delivery

    def _keep_copy(self, alert: Alert, case: Case, url: str, msg: EmailMessage, recipients: list[str],
                   delivery: Delivery) -> None:
        """Outbox copy of the message and the send result (best effort; never changes the delivery result)."""
        if self.files is None:
            return
        now = utcnow()
        stem = f"{now.strftime('%Y%m%dT%H%M%S')}{now.microsecond // 1000:03d}Z_{alert.alert_id}_{case.case_id}"
        entry = {"alert_id": alert.alert_id, "case_id": case.case_id, "case_key": case.case_key,
                 "reason": alert.reason, "link": url, "written_at_utc": iso_utc(now, ms=True),
                 "recipients": recipients, "channel": self.channel, "eml": f"{stem}.eml",
                 "delivery_status": delivery.status, "acs_status": delivery.detail.get("acs_status"),
                 "message_id": delivery.detail.get("message_id"), "error": delivery.detail.get("error"),
                 "note": "copy of the demo e-mail to the configured demo recipients - nothing goes to an authority"}
        try:
            self.files.put(OUTBOX_AREA, f"{stem}.eml", bytes(msg), content_type="message/rfc822")
            meta = self.files.put(OUTBOX_AREA, f"{stem}.json",
                                  json.dumps(entry, ensure_ascii=False, indent=2).encode("utf-8"),
                                  content_type="application/json")
            delivery.detail.update({"outbox_entry": meta.name, "outbox_eml": f"{stem}.eml",
                                    "outbox_uri": meta.uri})
        except Exception as exc:
            delivery.detail["outbox_copy_error"] = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("could not keep the outbox copy of alert %s: %s", alert.alert_id, exc)

    def entries(self) -> list[dict[str, Any]]:
        return outbox_entries(self.files)
