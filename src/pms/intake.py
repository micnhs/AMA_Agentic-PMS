"""Letter intake: upload decoding, the intake register of the demo letters and PDF helpers.

* :func:`decode_upload` turns an upload (text or base64 content, ``.txt`` or ``.pdf``) into
  text. A PDF needs the optional ``pypdf`` package; without it - or without a text layer - the
  upload becomes visibly UNREADABLE (status NICHT_LESBAR and a task), it is never dropped
  (concept 6.4 step 2, ADR-003, D-02).
* :func:`load_intake` reads ``data/synthetic/letters/intake.json``: receipt time (UTC), intake
  reference and channel of each synthetic letter. This is INPUT metadata for seeding - the
  runtime never reads ``expected.json``.
* :func:`make_pdfs` (developer helper, needs ``reportlab``) renders the letter texts as PDFs with
  a text layer; :func:`extract_pdf_text` reads them back.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .versions import default_data_dir

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
LETTER_CHANNEL = "letter upload"
INTAKE_FILE = "intake.json"

CHANNEL_LABELS: dict[str, str] = {"upload": LETTER_CHANNEL, "seed": "seed import", "api": "API"}


class UploadRejected(ValueError):
    """The upload cannot be accepted at all (too large, invalid encoding of the request)."""


@dataclass(frozen=True)
class DecodedUpload:
    text: str
    readable: bool
    kind: str  # text | pdf | unknown
    note: str | None
    raw: bytes | None  # original bytes (kept for unreadable or binary uploads)


def normalise_whitespace(text: str) -> str:
    return " ".join(text.split())


def extract_pdf_text(data: bytes) -> tuple[str | None, str | None]:
    """Text layer of a PDF via the optional pypdf package: (text, problem)."""
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        return None, "PDF text extraction is unavailable (optional package pypdf is not installed)"
    import io

    def page_text(page: Any) -> str:
        # Layout mode keeps blank lines between paragraphs, which INJ-1.0 needs to mask a
        # whole injected paragraph; plain mode is the fallback.
        try:
            return page.extract_text(extraction_mode="layout") or ""
        except TypeError:
            return page.extract_text() or ""

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page_text(page) for page in reader.pages]
    except Exception as exc:  # broken or encrypted PDF: visible as unreadable
        return None, f"PDF could not be read: {type(exc).__name__}"
    lines: list[str] = []
    for page in pages:
        for line in page.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            cleaned = re.sub(r"[ \t]{2,}", " ", line.strip())
            if cleaned or (lines and lines[-1]):
                lines.append(cleaned)
    text = "\n".join(lines).strip("\n")
    if not text.strip():
        return None, "PDF has no text layer (scanned letters need OCR, which is roadmap only)"
    return text + "\n", None


def decode_upload(filename: str | None, *, text: str | None = None, content_base64: str | None = None,
                  content: bytes | None = None) -> DecodedUpload:
    """Decode an upload. Never raises for unreadable content - it is returned as unreadable."""
    if text is not None:
        if len(text.encode("utf-8", errors="replace")) > MAX_UPLOAD_BYTES:
            raise UploadRejected(f"upload larger than {MAX_UPLOAD_BYTES} bytes")
        ok = bool(text.strip())
        return DecodedUpload(text, ok, "text", None if ok else "empty text", None)
    if content_base64 is not None:
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise UploadRejected(f"content_base64 is not valid base64: {exc}") from exc
    if content is None:
        raise UploadRejected("either text or content_base64 is required")
    if len(content) > MAX_UPLOAD_BYTES:
        raise UploadRejected(f"upload larger than {MAX_UPLOAD_BYTES} bytes")
    name = (filename or "").lower()
    if content[:5] == b"%PDF-" or name.endswith(".pdf"):
        extracted, problem = extract_pdf_text(content)
        if extracted is None:
            return DecodedUpload("", False, "pdf", problem, content)
        return DecodedUpload(extracted, True, "pdf", None, content)
    if name.endswith((".txt", ".text", ".md")) or not name or "." not in name:
        try:
            decoded = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            return DecodedUpload("", False, "text", "text is not valid UTF-8", content)
        if not decoded.strip():
            return DecodedUpload("", False, "text", "empty text", content)
        return DecodedUpload(decoded, True, "text", None, None)
    return DecodedUpload("", False, "unknown", f"unsupported file type: {filename}", content)


# --------------------------------------------------------------------------- intake register
@dataclass(frozen=True)
class IntakeEntry:
    ref: str
    filename: str
    pdf_filename: str | None
    received_at: str
    channel: str


def intake_path(data_dir: str | Path | None = None) -> Path:
    base = Path(data_dir) if data_dir is not None else default_data_dir()
    return base / "letters" / INTAKE_FILE


def load_intake(data_dir: str | Path | None = None) -> list[IntakeEntry]:
    raw = json.loads(intake_path(data_dir).read_text(encoding="utf-8"))
    return [IntakeEntry(ref=e["ref"], filename=e["filename"], pdf_filename=e.get("pdf_filename"),
                        received_at=e["received_at"], channel=e.get("channel", LETTER_CHANNEL))
            for e in raw["letters"]]


# --------------------------------------------------------------------------- PDF generation (developer helper)
_PAGE_MARGIN_MM = 20
_FONT = "Helvetica"
_FONT_SIZE = 10
_LEADING = 13


def _wrap(line: str, width_pt: float, string_width: Any) -> list[str]:
    if not line.strip():
        return [""]
    words = line.split(" ")
    out: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if current and string_width(candidate, _FONT, _FONT_SIZE) > width_pt:
            out.append(current)
            current = word
        else:
            current = candidate
    out.append(current)
    return out


def render_pdf(text: str, out_path: Path, *, title: str) -> None:
    """Render ``text`` as a simple A4 PDF with a text layer (reportlab, invariant output)."""
    from reportlab.lib.pagesizes import A4  # type: ignore[import-not-found]
    from reportlab.lib.units import mm  # type: ignore[import-not-found]
    from reportlab.pdfbase.pdfmetrics import stringWidth  # type: ignore[import-not-found]
    from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-not-found]

    width, height = A4
    margin = _PAGE_MARGIN_MM * mm
    usable = width - 2 * margin
    canvas = Canvas(str(out_path), pagesize=A4, invariant=1)
    canvas.setTitle(title)
    canvas.setAuthor("pms-agent-demo (synthetic demo data)")
    canvas.setSubject("Synthetic demo letter - fictional data")
    canvas.setFont(_FONT, _FONT_SIZE)
    y = height - margin
    for raw_line in text.rstrip("\n").split("\n"):
        for line in _wrap(raw_line, usable, stringWidth):
            if y < margin:
                canvas.showPage()
                canvas.setFont(_FONT, _FONT_SIZE)
                y = height - margin
            if line:
                canvas.drawString(margin, y, line)
            y -= _LEADING
    canvas.showPage()
    canvas.save()


def make_pdfs(data_dir: str | Path | None = None) -> list[Path]:
    """(Re)generate ``letters/*.pdf`` from the ``.txt`` letters (the text files stay the source)."""
    try:
        import reportlab  # noqa: F401  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("make-pdfs needs the developer package reportlab (requirements-dev.txt)") from exc
    letters = (Path(data_dir) if data_dir is not None else default_data_dir()) / "letters"
    written = []
    for txt in sorted(letters.glob("*.txt")):
        pdf = txt.with_suffix(".pdf")
        render_pdf(txt.read_text(encoding="utf-8"), pdf, title=f"{txt.stem} - synthetic demo letter")
        written.append(pdf)
    return written


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str | None, default: str = "upload.bin") -> str:
    cleaned = _SAFE_NAME.sub("_", Path(name or default).name).strip("._") or default
    return cleaned[:120]
