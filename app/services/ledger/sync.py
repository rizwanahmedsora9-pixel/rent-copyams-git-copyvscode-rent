"""Document ↔ book synchronisation.

One document in, one set of entries out.  ``sync_document`` is idempotent and
convergent:

* a new document → post each of its specs;
* an unchanged document → nothing (payload hashes match);
* a changed document → the specs whose payload changed are reversed and
  re-posted, and specs the document no longer justifies (a discount removed,
  a freight cost zeroed) are reversed;
* a voided or deleted document → every live entry is reversed.

That is what makes an edit visible as a correction instead of a silent rewrite
of history (plan §18 S13).
"""
from __future__ import annotations

import logging

from models import db, JournalEntry, LedgerSyncEvent
from app.services.ledger import posting
from app.services.ledger import posting_rules

log = logging.getLogger("app.ledger.sync")

__all__ = [
    "MODELS",
    "CHILD_PARENTS",
    "sync_document",
    "release_document",
    "record_failure",
    "entries_for_document",
    "suspend",
    "is_suspended",
    "autopost_enabled",
]

_SUSPEND_KEY = "_ams_ledger_suspended"

#: child table → (parent document, foreign-key attribute).  Editing a line must
#: re-post the document that owns it; the child itself has no entry.
CHILD_PARENTS = {
    "DirectSaleItem": ("DirectSale", "sale_id"),
    "SaleDeliveryPerson": ("DirectSale", "sale_id"),
    "GRNAllocation": ("DirectSale", "sale_id"),
    "GRNItem": ("GRN", "grn_id"),
    "MaterialReturnItem": ("MaterialReturn", "material_return_id"),
    "BookingItem": ("Booking", "booking_id"),
}


def _model_map() -> dict:
    # Imported lazily so ``sync`` can be imported by ``posting_rules`` helpers
    # without a cycle at module import time.
    from models import (
        Account,
        AccountTransaction,
        Booking,
        CashFlowEntry,
        Client,
        DeliveryPersonPayment,
        DeliveryRent,
        DirectSale,
        GRN,
        MaterialReturn,
        Payment,
        Supplier,
        SupplierPayment,
        WaiveOff,
    )

    return {
        "Account": Account,
        "AccountTransaction": AccountTransaction,
        "Booking": Booking,
        "CashFlowEntry": CashFlowEntry,
        "Client": Client,
        "DeliveryPersonPayment": DeliveryPersonPayment,
        "DeliveryRent": DeliveryRent,
        "DirectSale": DirectSale,
        "GRN": GRN,
        "MaterialReturn": MaterialReturn,
        "Payment": Payment,
        "Supplier": Supplier,
        "SupplierPayment": SupplierPayment,
        "WaiveOff": WaiveOff,
    }


class _ModelsProxy(dict):
    """``sync.MODELS`` without importing every document model at import time."""

    def _refresh(self):
        if not dict.__len__(self):
            dict.update(self, _model_map())

    def __contains__(self, key):
        self._refresh()
        return dict.__contains__(self, key)

    def __getitem__(self, key):
        self._refresh()
        return dict.__getitem__(self, key)

    def keys(self):  # noqa: D401 - dict API
        self._refresh()
        return dict.keys(self)

    def items(self):
        self._refresh()
        return dict.items(self)

    def get(self, key, default=None):
        self._refresh()
        return dict.get(self, key, default)


MODELS = _ModelsProxy()


# ---------------------------------------------------------------------------
# suspend / resume (replay, wipes and tests use this)
# ---------------------------------------------------------------------------
def _info() -> dict:
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            return current_app.extensions.setdefault("ams_ledger", {})
    except Exception:  # pragma: no cover - no app context
        pass
    return _GLOBAL


_GLOBAL: dict = {}


class suspend:
    """Context manager: stop the listeners while a bulk operation runs."""

    def __init__(self, reason: str = "bulk"):
        self.reason = reason
        self._info = None

    def __enter__(self):
        self._info = _info()
        self._info[_SUSPEND_KEY] = self._info.get(_SUSPEND_KEY, 0) + 1
        return self

    def __exit__(self, exc_type, exc, tb):
        info = self._info if self._info is not None else _info()
        depth = info.get(_SUSPEND_KEY, 0) - 1
        info[_SUSPEND_KEY] = max(0, depth)
        return False


def is_suspended() -> bool:
    try:
        return bool(_info().get(_SUSPEND_KEY))
    except Exception:  # pragma: no cover - defensive
        return False


def autopost_enabled() -> bool:
    import os

    return (os.environ.get("AMS_LEDGER_AUTOPOST") or "1").strip().lower() not in {
        "0", "false", "off", "no",
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def entries_for_document(model_name: str, document_id: int) -> list[JournalEntry]:
    """Live (not reversed) entries for one document, oldest first."""
    return (
        JournalEntry.query.filter(
            JournalEntry.source_type == model_name,
            JournalEntry.source_id == int(document_id),
            JournalEntry.is_reversal_of.is_(None),
            JournalEntry.reversed_at.is_(None),
        )
        .order_by(JournalEntry.id.asc())
        .all()
    )


def record_failure(model_name: str, document_id, detail: str = "") -> None:
    """Leave a trace for Ledger → Books agree when a posting refused."""
    try:
        db.session.add(LedgerSyncEvent(
            action="failed",
            source_type=model_name,
            source_id=int(document_id) if document_id is not None else None,
            detail=(detail or "")[:500] or None,
            actor="autopost",
        ))
        # No commit here: this runs inside a failing ``before_commit`` hook and
        # committing would re-enter it.  The outer transaction stores the row.
        db.session.flush()
    except Exception:  # pragma: no cover - never mask the original failure
        try:
            db.session.rollback()
        except Exception:
            pass
        log.exception("Could not record ledger sync failure for %s#%s", model_name, document_id)


# ---------------------------------------------------------------------------
# the one public entry point
# ---------------------------------------------------------------------------
def sync_document(model_name: str, document_id, *, actor: str | None = None,
                  prune: bool = True) -> dict:
    """Post (or repair) the entries a document justifies. Idempotent."""
    model = MODELS.get(model_name)
    if model is None:
        return {"posted": [], "reversed": [], "skipped": True}
    document = db.session.get(model, int(document_id))
    if document is None:
        return release_document(model_name, document_id, actor=actor)

    builder = posting_rules.DOCUMENT_BUILDERS.get(model_name)
    if builder is None:
        return {"posted": [], "reversed": [], "skipped": True}

    specs = list(builder(document) or [])
    posted: list[str] = []
    reversed_entries: list[str] = []

    # Post/replace what the document justifies.
    for spec in specs:
        entry = posting.post_spec(
            model_name,
            int(document_id),
            spec,
            actor=actor,
        )
        if entry is not None and not getattr(entry, "_ams_unchanged", False):
            posted.append(entry.entry_no)

    if prune:
        wanted = {spec.event for spec in specs}
        for entry in entries_for_document(model_name, int(document_id)):
            if entry.event in wanted:
                continue
            mirror = posting.reverse_entry(
                entry,
                reason="Document no longer justifies this entry",
                actor=actor,
            )
            reversed_entries.append(mirror.entry_no)

    if posted or reversed_entries:
        log.info(
            "Ledger sync %s#%s posted=%s reversed=%s",
            model_name, document_id, posted, reversed_entries,
        )
    return {"posted": posted, "reversed": reversed_entries, "skipped": not (posted or reversed_entries)}


def release_document(model_name: str, document_id, *, actor: str | None = None) -> dict:
    """Reverse everything a now-voided/deleted document had posted."""
    reversed_entries: list[str] = []
    for entry in entries_for_document(model_name, int(document_id)):
        mirror = posting.reverse_entry(
            entry, reason="Source document voided or deleted", actor=actor
        )
        reversed_entries.append(mirror.entry_no)
    return {"posted": [], "reversed": reversed_entries, "skipped": not reversed_entries}
