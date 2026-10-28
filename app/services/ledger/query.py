"""Read the book.  Every report in the application goes through this module.

All amounts are integer paisa.  ``balance`` is always *signed net debit*
(``debit - credit``), and each report applies the account's natural side when it
needs a positive number — so one convention, no per-page arithmetic.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy import func, or_

from models import (
    db,
    AccountingPeriod,
    Client,
    DeliveryPerson,
    GLAccount,
    JournalEntry,
    JournalLine,
    LedgerVersion,
    Supplier,
)
from app.services.ledger import accounts as coa
from app.services.ledger import periods as period_svc

log = logging.getLogger("app.ledger.query")

__all__ = [
    "entry_query",
    "journal_entries",
    "account_movements",
    "account_balances",
    "trial_balance",
    "profit_and_loss",
    "balance_sheet",
    "cash_positions",
    "client_subledger",
    "supplier_subledger",
    "driver_subledger",
    "expense_breakdown",
    "revenue_by_kind",
    "period_totals",
    "coverage",
    "ledger_version",
]


def _day(value):
    if value in (None, ""):
        return None
    return period_svc.normalize_date(value)


def _apply_dates(query, start=None, end=None, column=JournalEntry.entry_date):
    start = _day(start)
    end = _day(end)
    if start is not None:
        query = query.filter(column >= start)
    if end is not None:
        query = query.filter(column <= end)
    return query


def ledger_version() -> int:
    try:
        row = db.session.query(LedgerVersion.version).filter(LedgerVersion.id == 1).first()
        return int(row[0] if row else 0)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Entries and movements
# ---------------------------------------------------------------------------
def entry_query(*, start=None, end=None, source_type=None, event=None, client_id=None,
                supplier_id=None, document_id=None, text=None, include_reversals=True):
    query = db.session.query(JournalEntry).join(
        JournalLine, JournalLine.entry_id == JournalEntry.id
    )
    query = _apply_dates(query, start, end)
    if source_type:
        query = query.filter(JournalEntry.source_type == source_type)
    if event:
        query = query.filter(JournalEntry.event == event)
    if client_id:
        query = query.filter(JournalLine.client_id == int(client_id))
    if supplier_id:
        query = query.filter(JournalLine.supplier_id == int(supplier_id))
    if document_id:
        query = query.filter(JournalLine.document_id == int(document_id))
    if text:
        like = f"%{text.strip()}%"
        query = query.filter(or_(
            JournalEntry.entry_no.ilike(like),
            JournalEntry.description.ilike(like),
            JournalEntry.reference.ilike(like),
            JournalLine.material_name.ilike(like),
        ))
    if not include_reversals:
        query = query.filter(JournalEntry.is_reversal_of.is_(None))
    return query


def journal_entries(*, page: int = 1, per_page: int = 50, **filters) -> dict:
    page = max(1, int(page or 1))
    per_page = max(1, min(500, int(per_page or 50)))
    query = entry_query(**filters).distinct()
    total = query.order_by(None).count()
    rows = (
        query.order_by(JournalEntry.entry_date.desc(), JournalEntry.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return {
        "entries": rows,
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": max(1, (total + per_page - 1) // per_page),
    }


def account_movements(code: str, *, start=None, end=None, limit: int = 500) -> dict:
    gl = coa.account_by_code(code)
    if gl is None:
        return {"account": None, "rows": [], "opening": 0, "closing": 0}
    signed = (
        func.sum(JournalLine.debit_minor) - func.sum(JournalLine.credit_minor)
    )
    opening_query = db.session.query(signed).join(
        JournalEntry, JournalEntry.id == JournalLine.entry_id
    ).filter(JournalLine.gl_account_id == gl.id)
    start_day = _day(start)
    if start_day is not None:
        opening_query = opening_query.filter(JournalEntry.entry_date < start_day)
    opening = int(opening_query.scalar() or 0)

    query = db.session.query(JournalLine, JournalEntry).join(
        JournalEntry, JournalEntry.id == JournalLine.entry_id
    ).filter(JournalLine.gl_account_id == gl.id)
    query = _apply_dates(query, start, end)
    rows = query.order_by(JournalEntry.entry_date.asc(), JournalEntry.id.asc()).limit(limit).all()
    running = opening
    shaped = []
    for line, entry in rows:
        running += int(line.debit_minor or 0) - int(line.credit_minor or 0)
        shaped.append({
            "entry": entry,
            "line": line,
            "debit": int(line.debit_minor or 0),
            "credit": int(line.credit_minor or 0),
            "running": running,
        })
    return {"account": gl, "rows": shaped, "opening": opening, "closing": running}


def account_balances(*, start=None, end=None, as_of=None):
    end = end or as_of
    query = db.session.query(
        GLAccount.id,
        GLAccount.code,
        GLAccount.name,
        GLAccount.account_class,
        GLAccount.subcategory,
        GLAccount.normal_side,
        GLAccount.is_control,
        GLAccount.is_cash,
        GLAccount.linked_account_id,
        func.coalesce(func.sum(JournalLine.debit_minor), 0),
        func.coalesce(func.sum(JournalLine.credit_minor), 0),
    ).join(JournalLine, JournalLine.gl_account_id == GLAccount.id).join(
        JournalEntry, JournalEntry.id == JournalLine.entry_id
    )
    query = _apply_dates(query, start, end)
    query = query.group_by(
        GLAccount.id, GLAccount.code, GLAccount.name, GLAccount.account_class,
        GLAccount.subcategory, GLAccount.normal_side, GLAccount.is_control,
        GLAccount.is_cash, GLAccount.linked_account_id,
    ).order_by(GLAccount.code.asc())

    rows = []
    for row in query.all():
        debit = int(row[9] or 0)
        credit = int(row[10] or 0)
        rows.append({
            "id": row[0],
            "code": row[1],
            "name": row[2],
            "account_class": row[3],
            "subcategory": row[4],
            "normal_side": row[5],
            "is_control": bool(row[6]),
            "is_cash": bool(row[7]),
            "linked_account_id": row[8],
            "debit": debit,
            "credit": credit,
            "balance": debit - credit,
            "natural": (debit - credit) if (row[5] or "debit") == "debit" else (credit - debit),
        })
    return rows


def trial_balance(*, start=None, end=None, as_of=None) -> dict:
    rows = account_balances(start=start, end=end, as_of=as_of)
    rows = [row for row in rows if row["debit"] or row["credit"]]
    total_debit = sum(row["debit"] for row in rows)
    total_credit = sum(row["credit"] for row in rows)
    return {
        "rows": rows,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "difference": total_debit - total_credit,
        "balanced": total_debit == total_credit,
        "start": _day(start),
        "end": _day(end) or _day(as_of),
    }


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------
_CLASS_GROUPS = {
    "asset": "assets",
    "liability": "liabilities",
    "equity": "equity",
    "revenue": "revenue",
    "cost": "cost",
    "expense": "expense",
    "other_loss": "other_loss",
    "other_income": "other_income",
}


def profit_and_loss(*, start=None, end=None) -> dict:
    rows = account_balances(start=start, end=end)
    revenue, discounts, costs, expenses, other_losses, other_income = [], [], [], [], [], []
    for row in rows:
        if not (row["debit"] or row["credit"]):
            continue
        cls = row["account_class"]
        if cls == "revenue":
            if row["code"] == coa.GL.TRADE_DISCOUNTS:
                discounts.append({**row, "amount": row["debit"] - row["credit"]})
            elif row["code"] == coa.GL.ROUNDING:
                other_income.append({**row, "amount": row["credit"] - row["debit"]})
            elif row["subcategory"] == "Other income":
                other_income.append({**row, "amount": row["credit"] - row["debit"]})
            else:
                revenue.append({**row, "amount": row["credit"] - row["debit"]})
        elif cls == "cost":
            costs.append({**row, "amount": row["debit"] - row["credit"]})
        elif cls == "expense":
            expenses.append({**row, "amount": row["debit"] - row["credit"]})
        elif cls == "other_loss":
            other_losses.append({**row, "amount": row["debit"] - row["credit"]})
        elif cls == "other_income":
            other_income.append({**row, "amount": row["credit"] - row["debit"]})

    gross_revenue = sum(row["amount"] for row in revenue)
    discount_total = sum(row["amount"] for row in discounts)
    net_revenue = gross_revenue - discount_total
    cost_total = sum(row["amount"] for row in costs)
    expense_total = sum(row["amount"] for row in expenses)
    loss_total = sum(row["amount"] for row in other_losses)
    income_total = sum(row["amount"] for row in other_income)
    gross_profit = net_revenue - cost_total
    operating = gross_profit + income_total - expense_total
    net_profit = operating - loss_total
    return {
        "start": _day(start),
        "end": _day(end),
        "revenue": revenue,
        "discounts": discounts,
        "costs": costs,
        "expenses": expenses,
        "other_losses": other_losses,
        "other_income": other_income,
        "gross_revenue": gross_revenue,
        "discount_total": discount_total,
        "net_revenue": net_revenue,
        "cost_total": cost_total,
        "expense_total": expense_total,
        "other_loss_total": loss_total,
        "other_income_total": income_total,
        "gross_profit": gross_profit,
        "net_profit": net_profit,
    }


def balance_sheet(*, as_of=None) -> dict:
    rows = account_balances(end=as_of)
    assets, liabilities, equity = [], [], []
    for row in rows:
        if not (row["debit"] or row["credit"]):
            continue
        cls = row["account_class"]
        if cls == "asset":
            assets.append({**row, "amount": row["balance"]})
        elif cls == "liability":
            liabilities.append({**row, "amount": -row["balance"]})
        elif cls == "equity":
            equity.append({**row, "amount": -row["balance"]})

    pnl = profit_and_loss(end=as_of)
    earnings = int(pnl["net_profit"])
    asset_total = sum(row["amount"] for row in assets)
    liability_total = sum(row["amount"] for row in liabilities)
    equity_total = sum(row["amount"] for row in equity)
    retained = equity_total + earnings
    return {
        "as_of": _day(as_of),
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "asset_total": asset_total,
        "liability_total": liability_total,
        "equity_total": equity_total,
        "earnings_to_date": earnings,
        "retained_total": retained,
        "liabilities_and_equity": liability_total + retained,
        "difference": asset_total - (liability_total + retained),
        "balanced": asset_total == (liability_total + retained),
    }


def cash_positions(*, as_of=None) -> dict:
    rows = [row for row in account_balances(end=as_of) if row["is_cash"]]
    total = sum(row["balance"] for row in rows)
    return {"rows": rows, "total": total}


def _subledger(code: str, *, as_of=None, dimension: str, label_model=None) -> list[dict]:
    gl = coa.account_by_code(code)
    if gl is None:
        return []
    dim = getattr(JournalLine, dimension)
    query = db.session.query(
        dim,
        func.coalesce(func.sum(JournalLine.debit_minor), 0),
        func.coalesce(func.sum(JournalLine.credit_minor), 0),
    ).join(JournalEntry, JournalEntry.id == JournalLine.entry_id).filter(
        JournalLine.gl_account_id == gl.id, dim.isnot(None)
    )
    query = _apply_dates(query, None, as_of)
    query = query.group_by(dim)
    results = []
    ids = []
    raw = []
    for dim_id, debit, credit in query.all():
        debit, credit = int(debit or 0), int(credit or 0)
        raw.append((int(dim_id), debit, credit))
        ids.append(int(dim_id))
    names = {}
    if label_model is not None and ids:
        names = {
            row.id: row.name
            for row in label_model.query.filter(label_model.id.in_(ids)).all()
        }
    for dim_id, debit, credit in raw:
        if debit - credit == 0:
            continue
        results.append({
            "id": dim_id,
            "name": names.get(dim_id, f"#{dim_id}"),
            "debit": debit,
            "credit": credit,
            "balance": debit - credit,
            "amount": (credit - debit) if code in (coa.GL.PAYABLE, coa.GL.DRIVER_PAYABLE) else (debit - credit),
        })
    results.sort(key=lambda row: row["name"].lower())
    return results


def client_subledger(*, as_of=None) -> list[dict]:
    return _subledger(coa.GL.RECEIVABLE, as_of=as_of, dimension="client_id", label_model=Client)


def supplier_subledger(*, as_of=None) -> list[dict]:
    return _subledger(coa.GL.PAYABLE, as_of=as_of, dimension="supplier_id", label_model=Supplier)


def driver_subledger(*, as_of=None) -> list[dict]:
    return _subledger(coa.GL.DRIVER_PAYABLE, as_of=as_of, dimension="delivery_person_id",
                      label_model=DeliveryPerson)


def expense_breakdown(*, start=None, end=None) -> list[dict]:
    rows = account_balances(start=start, end=end)
    return sorted(
        [
            {"code": row["code"], "name": row["name"], "amount": row["debit"] - row["credit"]}
            for row in rows
            if row["account_class"] in ("expense", "other_loss") and (row["debit"] or row["credit"])
        ],
        key=lambda item: -item["amount"],
    )


def revenue_by_kind(*, start=None, end=None) -> dict:
    rows = account_balances(start=start, end=end)
    by_code = {row["code"]: row for row in rows}
    goods = by_code.get(coa.GL.SALES_GOODS)
    services = by_code.get(coa.GL.SALES_SERVICES)
    discount = by_code.get(coa.GL.TRADE_DISCOUNTS)
    return {
        "goods": (goods["credit"] - goods["debit"]) if goods else 0,
        "services": (services["credit"] - services["debit"]) if services else 0,
        "discounts": (discount["debit"] - discount["credit"]) if discount else 0,
    }


def period_totals(period) -> dict:
    if period is None:
        return {}
    pnl = profit_and_loss(start=period.start_date, end=period.end_date)
    tb = trial_balance(start=period.start_date, end=period.end_date)
    return {
        "period": period,
        "net_profit": pnl["net_profit"],
        "revenue": pnl["net_revenue"],
        "cost": pnl["cost_total"],
        "entries": db.session.query(func.count(JournalEntry.id)).filter(
            JournalEntry.entry_date >= period.start_date,
            JournalEntry.entry_date <= period.end_date,
        ).scalar() or 0,
        "balanced": tb["balanced"],
        "difference": tb["difference"],
    }


def _document_date_column(model):
    for attribute in ("date_posted", "created_at"):
        column = getattr(model, attribute, None)
        if column is not None:
            return column
    return None


def coverage(start=None, end=None) -> dict:
    """Document ↔ entry coverage, per watched source type.

    With *start*/*end* the count is limited to documents dated inside that
    window, which is what the month-end checklist needs: a missing entry in
    another month must not block this one from closing.
    """
    from app.services.ledger import sync

    start = _day(start)
    end = _day(end)
    result = {}
    for name, model in sync.MODELS.items():
        try:
            query = db.session.query(func.count(model.id))
            # Opening balances are the only thing these masters ever post, so a
            # master with a zero opening balance is not "missing an entry".
            if name in ("Client", "Supplier"):
                query = query.filter(func.coalesce(model.opening_balance, 0) != 0)
            elif name == "Account":
                query = query.filter(
                    or_(
                        func.coalesce(model.opening_balance_minor, 0) != 0,
                        func.coalesce(model.opening_balance, 0) != 0,
                    )
                )
            column = _document_date_column(model)
            if column is not None and (start or end):
                if start:
                    query = query.filter(func.date(column) >= start)
                if end:
                    query = query.filter(func.date(column) <= end)
            documents = query.scalar() or 0
            sources = db.session.query(JournalEntry.source_id).filter(
                JournalEntry.source_type == name,
                JournalEntry.is_reversal_of.is_(None),
                JournalEntry.reversed_at.is_(None),
            )
            if start or end:
                sources = sources.filter(
                    JournalEntry.entry_date >= (start or date.min),
                    JournalEntry.entry_date <= (end or date.max),
                )
            with_entries = sources.distinct().count()
            result[name] = {
                "documents": int(documents),
                "with_entries": int(with_entries),
                "missing": max(0, int(documents) - int(with_entries)),
            }
        except Exception:  # pragma: no cover - defensive
            db.session.rollback()
            result[name] = {"documents": 0, "with_entries": 0, "missing": 0}
    return result
