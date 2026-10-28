"""The posting engine — the only writer of ``journal_entry`` / ``journal_line``.

Contract (plan §6 S1):

* ``post_spec`` writes one balanced entry for one document event, idempotently.
  Re-posting the same event with the same amounts is a no-op; re-posting it with
  different amounts reverses the old entry and writes the new one.
* ``reverse_entry`` writes the mirror image of an entry.  It is the *only* way a
  posted number changes.
* A locked period refuses new entries.  A reversal of an entry that sits in a
  locked period is dated into the current open period instead, so history never
  moves and the cumulative balance still ends up right.
* Every write bumps ``ledger_version``, which is what read-model caches key on.

Nothing in this module knows about documents; the document → lines mapping lives
in ``posting_rules.py`` and the dispatch/caching in ``sync.py``.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text

from models import db, GLAccount, JournalEntry, JournalLine
from app.services.ledger import accounts as coa
from app.services.ledger import periods as period_svc
from app.services.ledger.money import format_minor

log = logging.getLogger("app.ledger.posting")

__all__ = [
    "LedgerError",
    "UnbalancedEntry",
    "MissingAccount",
    "Line",
    "EntrySpec",
    "post_spec",
    "post_specs",
    "reverse_entry",
    "entries_for",
    "active_entry",
    "ledger_version",
    "bump_version",
    "entry_display",
]


class LedgerError(ValueError):
    """Base class for accounting refusals (safe to show to the user)."""


class UnbalancedEntry(LedgerError):
    def __init__(self, debit: int, credit: int, event: str = ""):
        self.debit = int(debit)
        self.credit = int(credit)
        super().__init__(
            "This transaction does not balance: "
            f"debits {format_minor(self.debit)} vs credits {format_minor(self.credit)}"
            + (f" ({event})." if event else ".")
        )


class MissingAccount(LedgerError):
    pass


@dataclass
class Line:
    """One side of an entry, before it becomes a ``journal_line`` row."""

    code: str
    debit: int = 0
    credit: int = 0
    client_id: int | None = None
    supplier_id: int | None = None
    delivery_person_id: int | None = None
    material_name: str | None = None
    service_name: str | None = None
    document_type: str | None = None
    document_id: int | None = None
    memo: str | None = None

    def normalized(self) -> "Line":
        debit = int(self.debit or 0)
        credit = int(self.credit or 0)
        if debit < 0:
            credit += -debit
            debit = 0
        if credit < 0:
            debit += -credit
            credit = 0
        if debit and credit:
            # A line carries one side only; the larger side wins so a mistaken
            # double-sided amount cannot silently inflate both totals.
            net = debit - credit
            debit, credit = (net, 0) if net >= 0 else (0, -net)
        return Line(
            code=self.code,
            debit=debit,
            credit=credit,
            client_id=self.client_id,
            supplier_id=self.supplier_id,
            delivery_person_id=self.delivery_person_id,
            material_name=self.material_name,
            service_name=self.service_name,
            document_type=self.document_type,
            document_id=self.document_id,
            memo=self.memo,
        )

    @property
    def amount(self) -> int:
        return int(self.debit or 0) + int(self.credit or 0)


@dataclass
class EntrySpec:
    """A document event ready to post."""

    event: str
    entry_date: date
    description: str = ""
    reference: str | None = None
    lines: list[Line] = field(default_factory=list)
    adjustment: bool = False

    def clean_lines(self) -> list[Line]:
        return [line.normalized() for line in self.lines if line.normalized().amount]

    def totals(self) -> tuple[int, int]:
        lines = self.clean_lines()
        return (
            sum(int(line.debit or 0) for line in lines),
            sum(int(line.credit or 0) for line in lines),
        )


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------
def bump_version() -> int:
    """Increment and return the ledger version (single row, race-safe)."""
    try:
        db.session.execute(text(
            "INSERT INTO ledger_version (id, version) VALUES (1, 1) "
            "ON CONFLICT(id) DO UPDATE SET version = version + 1"
        ))
        row = db.session.execute(
            text("SELECT version FROM ledger_version WHERE id = 1")
        ).fetchone()
        return int(row[0] if row else 1)
    except Exception:
        log.exception("Could not bump ledger version")
        return 0


def ledger_version() -> int:
    try:
        row = db.session.execute(
            text("SELECT version FROM ledger_version WHERE id = 1")
        ).fetchone()
        return int(row[0] if row else 0)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# hashing / numbering
# ---------------------------------------------------------------------------
def _payload_hash(spec: EntrySpec) -> str:
    lines = spec.clean_lines()
    parts = [
        period_svc.normalize_date(spec.entry_date).isoformat(),
        spec.event,
        spec.description or "",
        spec.reference or "",
        "1" if spec.adjustment else "0",
    ]
    for line in sorted(
        lines,
        key=lambda ln: (
            ln.code, ln.debit, ln.credit, ln.client_id or 0,
            ln.supplier_id or 0, ln.delivery_person_id or 0,
            ln.material_name or "", ln.service_name or "",
        ),
    ):
        parts.append("|".join(str(x) for x in (
            line.code, line.debit, line.credit, line.client_id or "",
            line.supplier_id or "", line.delivery_person_id or "",
            line.material_name or "", line.service_name or "",
        )))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _next_entry_no(day: date) -> str:
    prefix = f"JE-{day.year}{day.month:02d}-"
    used = (
        db.session.query(JournalEntry.entry_no)
        .filter(JournalEntry.entry_no.like(f"{prefix}%"))
        .count()
    )
    return f"{prefix}{used + 1:05d}"


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------
def _write_entry(
    *,
    spec: EntrySpec,
    source_type: str,
    source_id: int,
    revision: int,
    payload_hash: str,
    is_reversal_of: int | None = None,
    actor: str | None = None,
    period=None,
) -> JournalEntry:
    day = period_svc.normalize_date(spec.entry_date)
    period = period or period_svc.get_or_create_period(day)
    lines = spec.clean_lines()
    if not lines:
        raise LedgerError("Refusing to post an empty journal entry.")
    debit = sum(line.debit for line in lines)
    credit = sum(line.credit for line in lines)
    if debit != credit:
        raise UnbalancedEntry(debit, credit, spec.event)

    key = (
        f"{source_type}:{source_id}:{spec.event}:r{revision}"
        if is_reversal_of is None
        else f"{source_type}:{source_id}:{spec.event}:rev:{is_reversal_of}"
    )
    entry = JournalEntry(
        entry_no=_next_entry_no(day),
        entry_date=day,
        period_id=period.id if period is not None else None,
        event=spec.event,
        source_type=source_type,
        source_id=int(source_id),
        reference=(spec.reference or "")[:80] or None,
        description=(spec.description or "")[:300] or None,
        revision=revision,
        payload_hash=payload_hash,
        is_reversal_of=is_reversal_of,
        is_adjustment=bool(spec.adjustment or (period is not None and period.status == period_svc.SOFT_CLOSED)),
        status="posted",
        idempotency_key=key[:140],
        created_by=(actor or "")[:80] or None,
    )
    db.session.add(entry)
    db.session.flush()

    for number, line in enumerate(lines, start=1):
        gl = coa.account_by_code(line.code)
        if gl is None:
            raise MissingAccount(
                f"Chart of accounts is missing '{line.code}'. Restore it under "
                "Ledger → Chart of accounts before posting."
            )
        db.session.add(JournalLine(
            entry_id=entry.id,
            line_no=number,
            gl_account_id=gl.id,
            debit_minor=int(line.debit or 0),
            credit_minor=int(line.credit or 0),
            client_id=line.client_id,
            supplier_id=line.supplier_id,
            delivery_person_id=line.delivery_person_id,
            material_name=(line.material_name or None),
            service_name=(line.service_name or None),
            document_type=(line.document_type or source_type)[:60],
            document_id=line.document_id if line.document_id is not None else int(source_id),
            memo=(line.memo or "")[:300] or None,
        ))
    db.session.flush()
    bump_version()
    return entry


def entries_for(source_type: str, source_id: int) -> list[JournalEntry]:
    return (
        JournalEntry.query.filter_by(source_type=source_type, source_id=int(source_id))
        .order_by(JournalEntry.id.asc())
        .all()
    )


def active_entry(source_type: str, source_id: int, event: str | None = None) -> JournalEntry | None:
    """The live (not reversed) entry for a document event, if any."""
    query = JournalEntry.query.filter_by(
        source_type=source_type, source_id=int(source_id), is_reversal_of=None
    ).filter(JournalEntry.reversed_at.is_(None))
    if event:
        query = query.filter(JournalEntry.event == event)
    return query.order_by(JournalEntry.id.desc()).first()


def reverse_entry(
    entry: JournalEntry,
    *,
    reason: str = "",
    actor: str | None = None,
    entry_date=None,
) -> JournalEntry | None:
    """Write the mirror image of *entry*.  Idempotent per source entry."""
    if entry is None:
        return None
    if entry.reversed_at is not None:
        return None
    existing = JournalEntry.query.filter_by(is_reversal_of=entry.id).first()
    if existing is not None:
        return existing

    day = period_svc.normalize_date(entry_date or entry.entry_date)
    try:
        period = period_svc.assert_can_post(day, actor=actor)
    except period_svc.PeriodLocked:
        # The original month is closed: keep it untouched and reverse into the
        # current open month, so the cumulative balance is still correct.
        day = date.today()
        period = period_svc.assert_can_post(day, actor=actor)

    spec = EntrySpec(
        event=entry.event,
        entry_date=day,
        description=f"Reversal of {entry.entry_no}" + (f" — {reason}" if reason else ""),
        reference=entry.reference,
        lines=[
            Line(
                code=(line.account.code if line.account else ""),
                debit=int(line.credit_minor or 0),
                credit=int(line.debit_minor or 0),
                client_id=line.client_id,
                supplier_id=line.supplier_id,
                delivery_person_id=line.delivery_person_id,
                material_name=line.material_name,
                service_name=line.service_name,
                document_type=line.document_type,
                document_id=line.document_id,
                memo="reversal",
            )
            for line in entry.lines
        ],
        adjustment=True,
    )
    mirror = _write_entry(
        spec=spec,
        source_type=entry.source_type,
        source_id=entry.source_id,
        revision=entry.revision,
        payload_hash="reversal:" + (entry.payload_hash or "")[:40],
        is_reversal_of=entry.id,
        actor=actor,
        period=period,
    )
    entry.reversed_at = datetime.now()
    entry.reversed_by = (actor or "")[:80] or None
    entry.reversal_reason = (reason or "")[:300] or None
    entry.status = "reversed"
    db.session.flush()
    return mirror


def post_spec(
    source_type: str,
    source_id: int,
    spec: EntrySpec,
    *,
    actor: str | None = None,
    allow_empty: bool = False,
) -> JournalEntry | None:
    """Post one document event, replacing a previous version if amounts changed."""
    if source_id in (None, ""):
        raise LedgerError("A journal entry needs a source document.")
    source_id = int(source_id)
    spec.entry_date = period_svc.normalize_date(spec.entry_date)
    lines = spec.clean_lines()
    if not lines:
        if allow_empty:
            return None
        return None

    debit = sum(line.debit for line in lines)
    credit = sum(line.credit for line in lines)
    if debit != credit:
        raise UnbalancedEntry(debit, credit, spec.event)

    digest = _payload_hash(spec)
    current = active_entry(source_type, source_id, spec.event)
    if current is not None and current.payload_hash == digest:
        # Already posted, unchanged: a retry, not a new entry.
        current._ams_unchanged = True
        return current

    period = period_svc.assert_can_post(spec.entry_date, actor=actor)
    if current is not None:
        # The document changed after it was posted: reverse, then repost.
        reverse_entry(current, reason="Document changed", actor=actor)
        revision = int(current.revision or 1) + 1
    else:
        prior = (
            JournalEntry.query.filter_by(source_type=source_type, source_id=source_id, event=spec.event)
            .order_by(JournalEntry.revision.desc())
            .first()
        )
        revision = int(prior.revision or 1) + 1 if prior is not None else 1

    entry = _write_entry(
        spec=spec,
        source_type=source_type,
        source_id=source_id,
        revision=revision,
        payload_hash=digest,
        actor=actor,
        period=period,
    )
    log.info(
        "Posted %s %s#%s → %s (%s lines, %s)",
        spec.event, source_type, source_id, entry.entry_no,
        len(lines), format_minor(debit),
    )
    return entry


def post_specs(source_type: str, source_id: int, specs, *, actor: str | None = None) -> list[JournalEntry]:
    posted = []
    for spec in specs:
        entry = post_spec(source_type, source_id, spec, actor=actor)
        if entry is not None:
            posted.append(entry)
    return posted


def entry_display(entry) -> dict:
    """A template-friendly shape for one entry."""
    return {
        "id": entry.id,
        "entry_no": entry.entry_no,
        "entry_date": entry.entry_date,
        "event": entry.event,
        "description": entry.description,
        "reference": entry.reference,
        "source_type": entry.source_type,
        "source_id": entry.source_id,
        "status": entry.status,
        "is_reversal_of": entry.is_reversal_of,
        "total": entry.total_minor,
        "lines": [
            {
                "account": f"{line.account.code} {line.account.name}" if line.account else "",
                "code": line.account.code if line.account else "",
                "debit": int(line.debit_minor or 0),
                "credit": int(line.credit_minor or 0),
                "memo": line.memo,
                "client_id": line.client_id,
                "supplier_id": line.supplier_id,
                "delivery_person_id": line.delivery_person_id,
            }
            for line in entry.lines
        ],
    }
