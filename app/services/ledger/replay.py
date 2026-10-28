"""Replay existing documents into the book, once, idempotently.

New saves post automatically (``autopost.py``).  History — everything entered
before the journal existed — is brought in by this module:

* ``replay_all()`` walks every mapped document table in date order and posts it;
* ``sync_missing()`` posts only documents that have no live entry yet, which is
  what the application runs at boot so a crashed/rolled-back posting is repaired
  on the next start without reposting the world.

Both are idempotent: ``posting.post_spec`` compares a payload hash and returns
the existing entry when nothing changed.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from models import (
    db,
    Account,
    AccountTransaction,
    Booking,
    CashFlowEntry,
    Client,
    DeliveryPersonPayment,
    DeliveryRent,
    DirectSale,
    GRN,
    JournalEntry,
    MaterialReturn,
    Payment,
    Supplier,
    SupplierPayment,
    WaiveOff,
)
from app.services.ledger import accounts as coa
from app.services.ledger import periods as period_svc
from app.services.ledger import posting, posting_rules, sync

log = logging.getLogger("app.ledger.replay")

__all__ = ["replay_all", "sync_missing", "bootstrap_ledger", "REPLAY_ORDER"]

# Order matters only for readability of the journal; every entry is independent.
REPLAY_ORDER = [
    ("Account", Account, "created_at"),
    ("Client", Client, "created_at"),
    ("Supplier", Supplier, "created_at"),
    ("GRN", GRN, "date_posted"),
    ("SupplierPayment", SupplierPayment, "date_posted"),
    ("Booking", Booking, "date_posted"),
    ("DirectSale", DirectSale, "date_posted"),
    ("MaterialReturn", MaterialReturn, "date_posted"),
    ("Payment", Payment, "date_posted"),
    ("WaiveOff", WaiveOff, "date_posted"),
    ("DeliveryRent", DeliveryRent, "date_posted"),
    ("DeliveryPersonPayment", DeliveryPersonPayment, "date_posted"),
    ("CashFlowEntry", CashFlowEntry, "date_posted"),
    ("AccountTransaction", AccountTransaction, "date_posted"),
]


def _ids_with_live_entries(model_name: str) -> set[int]:
    rows = (
        db.session.query(JournalEntry.source_id)
        .filter(
            JournalEntry.source_type == model_name,
            JournalEntry.is_reversal_of.is_(None),
            JournalEntry.reversed_at.is_(None),
        )
        .distinct()
        .all()
    )
    return {int(row[0]) for row in rows if row[0] is not None}


def _iter_documents(model, order_column: str, batch: int = 500):
    column = getattr(model, order_column, None) or model.id
    last_id = 0
    while True:
        rows = (
            model.query.filter(model.id > last_id)
            .order_by(column.asc(), model.id.asc())
            .limit(batch)
            .all()
        )
        if not rows:
            return
        for row in rows:
            last_id = row.id
            yield row


def _run(model_name: str, model, order_column: str, *, only_missing: bool, batch: int, actor: str) -> dict:
    known = _ids_with_live_entries(model_name) if only_missing else set()
    posted = reversed_count = skipped = failed = 0
    try:
        docs = _iter_documents(model, order_column, batch=batch)
        for document in docs:
            if only_missing and document.id in known:
                continue
            try:
                result = sync.sync_document(model_name, document.id, actor=actor)
            except Exception:
                failed += 1
                log.exception("Replay failed for %s#%s", model_name, document.id)
                db.session.rollback()
                continue
            posted += len(result.get("posted") or [])
            reversed_count += len(result.get("reversed") or [])
            if result.get("skipped"):
                skipped += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception("Replay pass failed for %s", model_name)
        failed += 1
    return {
        "source": model_name,
        "posted": posted,
        "reversed": reversed_count,
        "skipped": skipped,
        "failed": failed,
    }


def replay_all(*, batch: int = 500, actor: str = "replay", types=None) -> dict:
    """Post every document into the book (idempotent)."""
    coa.ensure_chart()
    coa.ensure_cash_accounts()
    period_svc.ensure_current_period()
    posting_rules.reset_lookup_cache()
    results = []
    with sync.suspend("replay"):
        for name, model, order_column in REPLAY_ORDER:
            if types and name not in types:
                continue
            results.append(_run(name, model, order_column, only_missing=False, batch=batch, actor=actor))
    total = {
        "posted": sum(r["posted"] for r in results),
        "reversed": sum(r["reversed"] for r in results),
        "failed": sum(r["failed"] for r in results),
        "sources": results,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
    log.info("Ledger replay finished: %s", {k: total[k] for k in ("posted", "reversed", "failed")})
    return total


def sync_missing(*, batch: int = 500, actor: str = "boot") -> dict:
    """Post only documents that have no live journal entry (fast, incremental)."""
    coa.ensure_chart()
    coa.ensure_cash_accounts()
    period_svc.ensure_current_period()
    posting_rules.reset_lookup_cache()
    results = []
    with sync.suspend("sync_missing"):
        for name, model, order_column in REPLAY_ORDER:
            results.append(_run(name, model, order_column, only_missing=True, batch=batch, actor=actor))
    return {
        "posted": sum(r["posted"] for r in results),
        "failed": sum(r["failed"] for r in results),
        "sources": [r for r in results if r["posted"] or r["failed"]],
    }


def bootstrap_ledger() -> dict:
    """Boot-time sync: seed the chart, map cash accounts, keep the book current.

    Never raises: a bookkeeping problem must not stop the store from opening,
    and the books-agree page will show whatever is missing.
    """
    from flask import current_app

    if not sync.autopost_enabled():
        return {"status": "disabled"}
    if (os.environ.get("AMS_LEDGER_SYNC_ON_BOOT") or "1").strip().lower() in {"0", "false", "off", "no"}:
        return {"status": "disabled-by-env"}
    try:
        coa.ensure_chart()
        created = coa.ensure_cash_accounts()
        period_svc.ensure_current_period()
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception("Ledger bootstrap failed while seeding the chart")
        return {"status": "failed"}

    try:
        entries = db.session.query(JournalEntry.id).count()
    except Exception:
        db.session.rollback()
        return {"status": "failed"}

    try:
        if entries == 0 and (os.environ.get("AMS_LEDGER_REPLAY_ON_BOOT") or "1").strip().lower() not in {"0", "false", "off", "no"}:
            report = replay_all(actor="boot-replay")
            report["mode"] = "full-replay"
        else:
            report = sync_missing(actor="boot-sync")
            report["mode"] = "incremental"
        report["status"] = "ok"
        report["cash_accounts_linked"] = created
    except Exception:
        db.session.rollback()
        log.exception("Ledger boot sync failed")
        return {"status": "failed"}
    try:
        current_app.logger.info("Ledger boot sync: %s", report.get("mode"))
    except Exception:
        pass
    return report


if __name__ == "__main__":  # pragma: no cover - manual repair entry point
    from app import create_app

    app = create_app()
    with app.app_context():
        print(sync_missing(actor="cli"))
