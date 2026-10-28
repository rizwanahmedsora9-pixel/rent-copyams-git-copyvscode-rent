"""Keep the book current: documents post themselves, in the same transaction.

A page never writes a journal entry by hand.  When a document is added, changed
or voided, the session listeners here notice at flush time and call
``sync.sync_document`` before the transaction commits, so either both the
document and its posting are stored or neither is.

Design notes (the reasons matter — see the master plan §16):

* **Posting is per document, at commit, once.**  Listeners collect the touched
  documents into ``session.info`` and the work happens in ``before_commit``;
  the flush that follows a document save has already assigned ids.
* **Cheap detection.**  Only mapped classes in ``sync.MODELS`` are considered;
  an unrelated save does not pay for a bookkeeping check.
* **Never silently diverge.**  A posting failure is logged and recorded; the
  document still saves, because a store that cannot sell is worse than a store
  whose books-agree page shows a problem.
* **Re-entrancy guard.**  Posting writes journal rows, which flushes the
  session again; the guard in ``session.info`` stops the listener from
  recursing.
"""
from __future__ import annotations

import logging

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app.services.ledger import periods as period_svc
from app.services.ledger import posting, sync

log = logging.getLogger("app.ledger.autopost")

__all__ = ["register", "post_pending"]

_PENDING = "_ams_ledger_pending"


def _pending(session) -> dict:
    return session.info.setdefault(_PENDING, {})


_OPENING_FIELDS = {
    "Client": ("opening_balance", "opening_balance_date", "transferred_to_id", "is_active"),
    "Supplier": ("opening_balance", "opening_balance_date", "transferred_to_id", "is_active"),
    "Account": ("opening_balance", "opening_balance_minor", "opening_balance_date", "is_active"),
}


def _opening_changed(session, obj) -> bool:
    """A master's balance columns move all the time; only posting matters."""
    fields = _OPENING_FIELDS.get(type(obj).__name__)
    if not fields:
        return True
    state = inspect(obj)
    for name in fields:
        history = state.attrs[name].history
        if history.has_changes():
            return True
    return False


def _note(session, model_name: str, document_id, *, deleted: bool = False) -> None:
    key = (model_name, int(document_id))
    _pending(session)[key] = "delete" if deleted else "upsert"


def _model_name(obj) -> str | None:
    name = type(obj).__name__
    return name if name in sync.MODELS else None


def _note_child(session, obj) -> bool:
    """A child row (sale line, GRN line, driver allocation) changed.

    The parent document is what posts, so mark the parent.  Returns True when
    the object was a known child.
    """
    parent = sync.CHILD_PARENTS.get(type(obj).__name__)
    if not parent:
        return False
    parent_name, attribute = parent
    parent_id = getattr(obj, attribute, None)
    if parent_id:
        _note(session, parent_name, parent_id)
    return True


@event.listens_for(Session, "after_flush")
def _after_flush(session, flush_context) -> None:  # noqa: ARG001
    if sync.is_suspended():
        return
    for obj in list(session.new):
        name = _model_name(obj)
        if name and getattr(obj, "id", None) is not None:
            _note(session, name, obj.id)
        elif _note_child(session, obj):
            continue
    # Dirty objects are only dirty *before* the flush; afterwards they are
    # synchronized and the session reports them clean.  Capture them here.


@event.listens_for(Session, "before_flush")
def _before_flush(session, flush_context, instances) -> None:  # noqa: ARG001
    if sync.is_suspended():
        return
    for obj in list(session.dirty):
        name = _model_name(obj)
        if name and getattr(obj, "id", None) is not None:
            if _opening_changed(session, obj):
                _note(session, name, obj.id)
        else:
            _note_child(session, obj)
    # A deleted document must reverse its entries; the row is gone by commit
    # time, so the decision is recorded while it still exists.
    for obj in list(session.deleted):
        name = _model_name(obj)
        if name and getattr(obj, "id", None) is not None:
            _note(session, name, obj.id, deleted=True)
        else:
            _note_child(session, obj)


@event.listens_for(Session, "before_commit")
def _before_commit(session) -> None:
    # ``before_commit`` fires *before* SQLAlchemy's own flush, so the listener
    # flushes first (see ``post_pending``); otherwise an edit made in this
    # transaction would only be noticed by the next commit.
    post_pending(session)


def post_pending(session) -> int:
    """Post every document touched by this transaction. Returns the count."""
    if sync.is_suspended():
        return 0
    try:
        if session.new or session.dirty or session.deleted:
            session.flush()
    except Exception:  # pragma: no cover - a failed flush is the caller's error
        log.debug("Ledger autopost could not pre-flush", exc_info=True)
    pending = dict(_pending(session))
    if not pending:
        return 0
    _pending(session).clear()
    posted = 0
    for (model_name, document_id), action in pending.items():
        try:
            with session.begin_nested():
                if action == "delete":
                    sync.release_document(model_name, document_id, actor="autopost")
                else:
                    sync.sync_document(model_name, document_id, actor="autopost")
            posted += 1
        except (posting.LedgerError, period_svc.PeriodLocked):
            # A closed month or a broken entry is a refusal the user must see:
            # the document save is aborted, not silently posted around.
            log.warning("Posting refused for %s#%s", model_name, document_id)
            raise
        except Exception:
            # The document still commits; the books-agree page reports the gap
            # and Ledger → Sync re-posts it.
            log.exception("Autopost failed for %s#%s", model_name, document_id)
            sync.record_failure(model_name, document_id)
    if posted:
        # The journal rows were added after the flush finished; make sure they
        # are written before the transaction commits.
        session.flush()
    return posted


def register() -> None:
    """Compatibility shim: importing this module registers the listeners."""
    return None
