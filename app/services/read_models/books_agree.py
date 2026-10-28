"""The "books agree" gate (master plan §25).

One page, every invariant, each check showing the book number, the operational
number computed from the documents, and the difference.  It replaces the pile of
audit markdown files: before a period close or a deployment, this page is either
green or it names the first break.

Structural invariants must be exact (fail otherwise):

* total debits = total credits,
* trial balance balances,
* balance sheet ties to equity + current earnings.

Control-account checks compare the book with the operational subledger.  A
difference here is a *report*, not a crash: it means some document has not been
posted (or was deleted after posting), and the coverage table below says which
type.  They are shown as warnings and never hidden.

Operational figures are computed with grouped SQL over the documents — never by
replaying FIFO per row — so the page stays fast as history grows.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func, or_, text

from models import (
    Account,
    Client,
    DeliveryPersonPayment,
    DeliveryRent,
    DirectSale,
    DirectSaleItem,
    GRNAllocation,
    GRN,
    GRNItem,
    MaterialReturn,
    Payment,
    SaleDeliveryPerson,
    Supplier,
    SupplierPayment,
    WaiveOff,
    db,
)
from app.services import cache
from app.services.ledger import accounts as coa
from app.services.ledger import query as q

__all__ = ["load", "checks"]


def _minor(value) -> int:
    return int(value or 0)


def _cents(value) -> int:
    return int(round(float(value or 0) * 100))


def _sum_scalar(query) -> int:
    try:
        return int(query.scalar() or 0)
    except Exception:
        db.session.rollback()
        return 0


def _operational_receivables() -> dict:
    sales_net = _sum_scalar(
        db.session.query(func.sum(DirectSale.amount - func.coalesce(DirectSale.discount, 0)))
        .filter(DirectSale.is_void.is_(False))
    )
    receipts = _sum_scalar(
        db.session.query(func.sum(Payment.amount)).filter(
            Payment.is_void.is_(False),
            Payment.payment_type.in_(["Receipt", "Refund"]),
        )
    )
    settlement_discount = _sum_scalar(
        db.session.query(func.sum(func.coalesce(Payment.discount, 0))).filter(
            Payment.is_void.is_(False),
            Payment.payment_type == "Receipt",
        )
    )
    returns = _sum_scalar(
        db.session.query(func.sum(MaterialReturn.amount)).filter(MaterialReturn.is_void.is_(False))
    )
    waive = _sum_scalar(
        db.session.query(func.sum(WaiveOff.amount)).filter(
            WaiveOff.is_void.is_(False), WaiveOff.payment_id.is_(None)
        )
    )
    opening = _sum_scalar(db.session.query(func.sum(func.coalesce(Client.opening_balance, 0))))
    return {
        "sales": _cents(sales_net),
        "receipts": _cents(receipts),
        "settlement_discount": _cents(settlement_discount),
        "returns": _cents(returns),
        "waive_off": _cents(waive),
        "opening": _cents(opening),
        "total": _cents(sales_net - receipts - settlement_discount - returns - waive + opening),
    }


def _operational_payables() -> dict:
    items_total = _cents(_sum_scalar(
        db.session.query(func.sum(GRNItem.qty * GRNItem.price_at_time))
        .join(GRN, GRNItem.grn_id == GRN.id)
        .filter(GRN.is_void.is_(False), GRNItem.is_void.is_(False))
    ))
    expenses = _cents(_sum_scalar(
        db.session.query(func.sum(
            func.coalesce(GRN.loading_cost, 0) + func.coalesce(GRN.freight_cost, 0)
            + func.coalesce(GRN.other_expense, 0) + func.coalesce(GRN.adjustment_amount, 0)
            - func.coalesce(GRN.discount, 0) + func.coalesce(GRN.tax_amount, 0)
        )).filter(GRN.is_void.is_(False))
    ))
    payments = _cents(_sum_scalar(
        db.session.query(func.sum(SupplierPayment.amount)).filter(SupplierPayment.is_void.is_(False))
    ))
    opening = _cents(_sum_scalar(db.session.query(func.sum(func.coalesce(Supplier.opening_balance, 0)))))
    total = items_total + expenses - payments + opening
    return {
        "grn_items": items_total,
        "grn_charges": expenses,
        "payments": payments,
        "opening": opening,
        "total": total,
    }


def _operational_inventory() -> dict:
    received = _cents(_sum_scalar(
        db.session.query(func.sum(GRNItem.qty * GRNItem.price_at_time))
        .join(GRN, GRNItem.grn_id == GRN.id)
        .filter(GRN.is_void.is_(False), GRNItem.is_void.is_(False))
    ))
    landed = _cents(_sum_scalar(
        db.session.query(func.sum(
            func.coalesce(GRN.loading_cost, 0) + func.coalesce(GRN.freight_cost, 0)
            + func.coalesce(GRN.other_expense, 0) + func.coalesce(GRN.adjustment_amount, 0)
            - func.coalesce(GRN.discount, 0)
        )).filter(GRN.is_void.is_(False))
    ))
    consumed = _cents(_sum_scalar(
        db.session.query(func.sum(GRNAllocation.qty * GRNAllocation.cost_rate))
        .filter(GRNAllocation.is_void.is_(False))
    ))
    return {
        "received": received,
        "landed": landed,
        "consumed": consumed,
        "total": received + landed - consumed,
    }


def _operational_driver_payable() -> dict:
    allocated = _cents(_sum_scalar(
        db.session.query(func.sum(SaleDeliveryPerson.rent_amount))
        .filter(SaleDeliveryPerson.is_void.is_(False))
    ))
    with_alloc = {
        row[0] for row in db.session.query(SaleDeliveryPerson.sale_id)
        .filter(SaleDeliveryPerson.is_void.is_(False)).distinct().all()
    }
    manual_rent = _cents(_sum_scalar(
        db.session.query(func.sum(DeliveryRent.amount)).filter(
            DeliveryRent.is_void.is_(False), DeliveryRent.sale_id.is_(None)
        )
    ))
    fallback = 0
    for sale in db.session.query(DirectSale.id, DirectSale.delivery_rent_cost).filter(
        DirectSale.is_void.is_(False), func.coalesce(DirectSale.delivery_rent_cost, 0) > 0
    ).all():
        if sale[0] not in with_alloc:
            fallback += _cents(sale[1])
    paid = _cents(_sum_scalar(
        db.session.query(func.sum(
            func.coalesce(DeliveryPersonPayment.amount_paid, 0)
            + func.coalesce(DeliveryPersonPayment.waive_off_amount, 0)
        )).filter(DeliveryPersonPayment.is_void.is_(False))
    ))
    return {
        "accrued": allocated + manual_rent + fallback,
        "settled": paid,
        "total": allocated + manual_rent + fallback - paid,
    }


def _operational_cash() -> dict:
    accounts = Account.query.all()
    total = 0
    rows = []
    for account in accounts:
        minor = (
            int(account.balance_minor)
            if getattr(account, "balance_minor", None) is not None
            else _cents(account.balance)
        )
        total += minor
        rows.append({"id": account.id, "name": account.name, "minor": minor})
    return {"total": total, "rows": rows}


def _check(key, title, *, book=0, operational=0, status=None, detail="", href=None, kind="tie"):
    difference = int(book) - int(operational)
    if status is None:
        if kind == "structural":
            status = "ok" if difference == 0 else "fail"
        elif kind == "tie":
            status = "ok" if difference == 0 else "warn"
        else:
            status = "ok"
    return {
        "key": key,
        "title": title,
        "book": int(book),
        "operational": int(operational),
        "difference": difference,
        "status": status,
        "detail": detail,
        "href": href,
        "kind": kind,
    }


def checks(as_of=None) -> list[dict]:
    as_of = as_of or date.today()
    balances = {row["code"]: row for row in q.account_balances(end=as_of)}
    tb = q.trial_balance(end=as_of)
    bs = q.balance_sheet(as_of=as_of)
    result = []

    total_debit = _minor(_sum_scalar(db.session.query(func.sum(q.JournalLine.debit_minor))))
    total_credit = _minor(_sum_scalar(db.session.query(func.sum(q.JournalLine.credit_minor))))
    result.append(_check(
        "double_entry", "Every entry has two sides",
        book=total_debit, operational=total_credit, kind="structural",
        detail="Total debits must equal total credits across the whole journal.",
        href="/ledger/trial_balance",
    ))
    result.append(_check(
        "trial_balance", "Trial balance balances",
        book=tb["total_debit"], operational=tb["total_credit"], kind="structural",
        detail="Debits and credits for the period to date.",
        href="/ledger/trial_balance",
    ))
    result.append(_check(
        "balance_sheet", "Balance sheet ties",
        book=bs["asset_total"], operational=bs["liabilities_and_equity"], kind="structural",
        detail="Assets must equal liabilities plus equity plus earnings to date.",
        href="/ledger/balance_sheet",
    ))

    cash_op = _operational_cash()
    cash_book = balances.get(coa.GL.CASH, {}).get("balance", 0) + sum(
        row["balance"] for row in balances.values() if row.get("is_cash") and row["code"] != coa.GL.CASH
    )
    result.append(_check(
        "cash_control", "Cash in the book equals the cash accounts",
        book=cash_book, operational=cash_op["total"], kind="tie",
        detail="Book cash (1010/1020/1030 + mapped accounts) vs account master balances.",
        href="/ledger/cash",
    ))

    ar_op = _operational_receivables()
    result.append(_check(
        "receivables", "Receivables control equals the client subledger",
        book=balances.get(coa.GL.RECEIVABLE, {}).get("balance", 0),
        operational=ar_op["total"], kind="tie",
        detail=(
            f"Documents: sales {ar_op['sales']} − receipts {ar_op['receipts']} "
            f"− settlement discounts {ar_op['settlement_discount']} − returns {ar_op['returns']} "
            f"− waive-offs {ar_op['waive_off']} + opening {ar_op['opening']}."
        ),
        href="/ledger/receivables",
    ))

    ap_op = _operational_payables()
    result.append(_check(
        "payables", "Payables control equals the supplier subledger",
        book=-balances.get(coa.GL.PAYABLE, {}).get("balance", 0),
        operational=ap_op["total"], kind="tie",
        detail=(
            f"Documents: GRN goods {ap_op['grn_items']} + charges {ap_op['grn_charges']} "
            f"− supplier payments {ap_op['payments']} + opening {ap_op['opening']}."
        ),
        href="/ledger/payables",
    ))

    inv_op = _operational_inventory()
    result.append(_check(
        "inventory", "Inventory control equals received minus consumed",
        book=balances.get(coa.GL.INVENTORY, {}).get("balance", 0),
        operational=inv_op["total"], kind="tie",
        detail=(
            f"Goods received {inv_op['received']} + landed {inv_op['landed']} "
            f"− consumed {inv_op['consumed']}. Returned goods are included in the book value."
        ),
        href="/ledger/inventory",
    ))

    drv_op = _operational_driver_payable()
    result.append(_check(
        "driver_payable", "Driver payable equals accrued rent minus settlements",
        book=-balances.get(coa.GL.DRIVER_PAYABLE, {}).get("balance", 0),
        operational=drv_op["total"], kind="tie",
        detail=f"Accrued {drv_op['accrued']} − settled {drv_op['settled']}.",
        href="/ledger/driver_payable",
    ))

    coverage = q.coverage()
    missing = sum(row["missing"] for row in coverage.values())
    result.append(_check(
        "coverage", "Every document has a journal entry",
        book=sum(row["with_entries"] for row in coverage.values()),
        operational=sum(row["documents"] for row in coverage.values()),
        status="ok" if missing == 0 else "warn", kind="info",
        detail=f"{missing} document(s) without a live entry.",
        href="/ledger/coverage",
    ))

    uncosted = _minor(_sum_scalar(
        db.session.query(func.count(DirectSaleItem.id))
        .join(DirectSale, DirectSaleItem.sale_id == DirectSale.id)
        .filter(
            DirectSale.is_void.is_(False),
            or_(DirectSaleItem.cost_rate_at_sale.is_(None), DirectSaleItem.cost_rate_at_sale == 0),
        )
    ))
    result.append(_check(
        "uncosted", "Every sold goods line has a FIFO cost",
        book=uncosted, operational=0, kind="info",
        status="ok" if uncosted == 0 else "warn",
        detail="Lines without a frozen cost contribute no COGS (they are listed, not guessed).",
        href="/ledger/coverage",
    ))
    return result


def load(as_of=None) -> dict:
    as_of = as_of or date.today()
    version = q.ledger_version()

    def build():
        rows = checks(as_of)
        failures = [row for row in rows if row["status"] == "fail"]
        warnings = [row for row in rows if row["status"] == "warn"]
        return {
            "as_of": as_of,
            "ledger_version": version,
            "checks": rows,
            "failures": failures,
            "warnings": warnings,
            "status": "fail" if failures else ("warn" if warnings else "ok"),
            "coverage": q.coverage(),
            "entry_count": int(
                _sum_scalar(db.session.query(func.count(q.JournalEntry.id)))
            ),
            "last_entry_at": db.session.query(func.max(q.JournalEntry.created_at)).scalar(),
        }

    return cache.get_or_build(("books_agree", str(as_of)), version, build)
