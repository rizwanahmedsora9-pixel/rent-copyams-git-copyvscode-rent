"""Verify the point-value problem is gone: same scenarios as the diagnosis."""
import os, sys, tempfile
from datetime import datetime, date
from decimal import Decimal

sys.path.insert(0, "/home/user/rent-copyams-git-copyvscode-rent")
tmpdir = tempfile.mkdtemp()
os.environ.update({
    "APP_DB_PATH": os.path.join(tmpdir, "v.db"),
    "DB_HEALTH_SNAPSHOT_PATH": os.path.join(tmpdir, "s.json"),
    "ALLOW_EMPTY_DB": "1", "BACKUP_EMBEDDED_SCHEDULER": "0",
    "AMS_SCHEMA_VERSION": "v44", "SQLITE_JOURNAL_MODE": "DELETE",
})
from app import create_app
from models import (db, Material, MaterialCategory, Service, Client, GRN, GRNItem,
                    DirectSale, DirectSaleItem, Booking, BookingItem, BookingAllocation,
                    MaterialReturn, MaterialReturnItem)
from app.services.profit_loss import build_profit_loss_report

app = create_app(); app.config["TESTING"] = True

def long_decimals(report, path=""):
    bad = set()
    def walk(v, path=""):
        if isinstance(v, float):
            s = repr(abs(v))
            if "." in s and len(s.split(".")[1]) > 2 and abs(v - round(v, 2)) > 1e-9:
                bad.add(path + "=" + s)
        elif isinstance(v, dict):
            for k, x in v.items():
                walk(x, path + "." + str(k))
        elif isinstance(v, (list, tuple)):
            for i, x in enumerate(v):
                walk(x, path + f"[{i}]")
    walk(report)
    return bad

with app.app_context():
    db.create_all()
    from app.services.schema import _ensure_default_admin
    _ensure_default_admin(); db.session.commit()

    cat = MaterialCategory(name="CEMENT", is_active=True); db.session.add(cat); db.session.flush()
    db.session.add(Material(name="CEMENT-OPC", code="C-1", category_id=cat.id, unit="Bag", unit_price=1200))
    db.session.add(Material(name="STEEL-10MM", code="S-1", category_id=cat.id, unit="Kg", unit_price=300))
    db.session.add(Service(code="RENT-MIXER", name="RENT-MIXER", unit="Pcs", unit_price=0, is_active=True))
    db.session.add(Client(name="ACME Builders", code="CL-1")); db.session.flush()
    grn = GRN(supplier="SUP", manual_bill_no="G-1", date_posted=datetime(2026, 9, 1, 10, 0))
    db.session.add(grn); db.session.flush()
    db.session.add(GRNItem(grn_id=grn.id, mat_name="CEMENT-OPC", qty=500, price_at_time=1000))
    db.session.add(GRNItem(grn_id=grn.id, mat_name="STEEL-10MM", qty=500, price_at_time=250))
    db.session.flush()

    def sale(ref, lines, discount, day):
        amount = sum(q * r for _n, q, r in lines)
        s = DirectSale(client_name="ACME Builders", client_code="CL-1", amount=amount,
                       discount=discount, paid_amount=amount - discount, category="Cash",
                       date_posted=datetime(2026, 9, day, 11, 0), manual_bill_no=ref)
        db.session.add(s); db.session.flush()
        # store per-line discount exactly as the sale route now does
        from app.services import pricing
        rows = [{"name": n, "qty": q, "rate": r,
                 "gross": pricing.cents_to_float(pricing.line_gross_cents(q, r)),
                 "is_service": n.startswith("RENT")} for n, q, r in lines]
        shares = pricing.cap_shares(pricing.allocate_document_discount(discount, rows), rows)
        for (n, q, r), share in zip(lines, shares):
            db.session.add(DirectSaleItem(sale_id=s.id, product_name=n, qty=q,
                                          price_at_time=r, discount=float(share),
                                          discount_kind="allocated" if share > 0 else "none"))
        db.session.flush()
    sale("S-RENT", [("CEMENT-OPC", 30, 1200), ("RENT-MIXER", 1, 5000)], 2000, 10)
    sale("S-TWO", [("CEMENT-OPC", 10, 1000), ("STEEL-10MM", 10, 300)], 1000, 11)
    sale("S-ONE", [("CEMENT-OPC", 30, 1200)], 2000, 12)
    db.session.commit()

    # ---- Booking with a service line, delivered in two parts --------------
    # 10 bags at 1,200 plus a 6,000 mixer rent, 3,333 discount.  The goods line
    # carries the whole discount, the rent line none, and each partial delivery
    # recognises only its own share.  Nothing divides, so nothing drifts.
    from app.services import pricing as _pricing
    bk = Booking(client_name="ACME Builders", amount=126000, discount=3333, paid_amount=0,
                 manual_bill_no="B-PART", date_posted=datetime(2026, 9, 5, 10, 0))
    db.session.add(bk); db.session.flush()
    bk_rows = [
        {"name": "CEMENT-OPC", "qty": 10, "rate": 1200, "gross": 12000, "is_service": False},
        {"name": "RENT-MIXER", "qty": 1, "rate": 6000, "gross": 6000, "is_service": True},
    ]
    bk_shares = _pricing.cap_shares(
        _pricing.allocate_document_discount(3333, bk_rows), bk_rows)
    goods = BookingItem(booking_id=bk.id, material_name="CEMENT-OPC", qty=10,
                        price_at_time=1200, discount=float(bk_shares[0]))
    rent = BookingItem(booking_id=bk.id, material_name="RENT-MIXER", qty=1,
                       price_at_time=6000, discount=float(bk_shares[1]))
    db.session.add_all([goods, rent])

    # Two deliveries: 4 bags, then 6 bags.
    for index, (day, qty) in enumerate([(8, 4), (12, 6)], start=1):
        amt = qty * 1200
        ds = DirectSale(client_name="ACME Builders", client_code="CL-1", amount=amt,
                        discount=0, paid_amount=amt, category="Booking Delivery",
                        date_posted=datetime(2026, 9, day, 9, 0),
                        manual_bill_no=f"BD-{index}")
        db.session.add(ds); db.session.flush()
        item = DirectSaleItem(sale_id=ds.id, product_name="CEMENT-OPC", qty=qty,
                              price_at_time=1200, discount=0, discount_kind="none")
        db.session.add(item); db.session.flush()
        db.session.add(BookingAllocation(sale_id=ds.id, sale_item_id=item.id,
                                         booking_item_id=goods.id, qty=qty, is_void=False))
    db.session.commit()

    rep = build_profit_loss_report(start_date=date(2026, 9, 1), end_date=date(2026, 9, 30))
    print("=== TRANSACTION ROWS ===")
    for r in rep["transactions"]:
        print(f"{str(r['reference'])[:10]:10s} {str(r['material'])[:12]:12s} "
              f"gross={r['gross_revenue']:>10.2f} disc={r['discount']:>9.2f} "
              f"rev={r['revenue']:>10.2f} profit={r['profit']:>10.2f}")
    s = rep["summary"]
    print("\ntotal_revenue ", repr(s["total_revenue"]))
    print("total_discounts", repr(s["total_discounts"]))
    print("total_profit  ", repr(s["total_profit"]))
    print("long-decimal floats in report:", long_decimals(rep) or "NONE")

    # returns: rent line returned at gross -> must net to zero, not -243.90
    ret = MaterialReturn(client_name="ACME Builders", return_type="normal", amount=5000,
                         manual_bill_no="R-1", date_posted=datetime(2026, 9, 20, 10, 0))
    db.session.add(ret); db.session.flush()
    db.session.add(MaterialReturnItem(material_return_id=ret.id, material_name="RENT-MIXER",
                                      qty=1, unit_rate=5000, price_at_time=5000))
    db.session.commit()

    # ---- Return of a discounted goods line, stored the way the route does ----
    from app.services.return_discounts import discount_for_return_line
    ret_disc = discount_for_return_line("ACME Builders", "CEMENT-OPC", 10,
                                       as_of=datetime(2026, 9, 21, 10, 0))
    ret2 = MaterialReturn(client_name="ACME Builders", return_type="normal", amount=10000,
                          manual_bill_no="R-2", date_posted=datetime(2026, 9, 21, 10, 0))
    db.session.add(ret2); db.session.flush()
    db.session.add(MaterialReturnItem(material_return_id=ret2.id, material_name="CEMENT-OPC",
                                      qty=10, unit_rate=1000, price_at_time=1000,
                                      discount_reversed=ret_disc))
    db.session.commit()
    print(f"\nreturn of 10 bags withdrew a stored discount of {ret_disc:.2f} "
          f"(oldest discounted cement line: 30 bags with 2,000 off = 66.67 a bag)")

    rep2 = build_profit_loss_report(start_date=date(2026, 9, 1), end_date=date(2026, 9, 30))
    print("\n=== BOOKING PARTIAL DELIVERY (3,333 discount over 10 bags + 6,000 rent) ===")
    booking_rows = [r for r in rep["transactions"]
                    if "BD-" in str(r.get("reference") or "")]
    recognised = 0.0
    for r in booking_rows:
        recognised += float(r.get("discount") or 0)
        print(f"  {str(r.get('reference'))[:10]:10s} qty {r['qty']:>4}  "
              f"gross {r['gross_revenue']:>9.2f}  discount {r['discount']:>8.2f}  "
              f"revenue {r['revenue']:>9.2f}")
    print(f"  recognised discount {recognised:.2f} of 3333.00 "
          f"(remaining {3333.00 - recognised:.2f} stays on the undelivered bags)")

    print("\n=== AFTER RETURNING THE RENT LINE ===")
    for r in rep2["transactions"]:
        if "RENT" in str(r["material"]) or "Return" in str(r["transaction_type"]):
            print(f"{str(r['transaction_type'])[:24]:24s} {str(r['material'])[:12]:12s} "
                  f"gross={r['gross_revenue']:>10.2f} disc={r['discount']:>9.2f} rev={r['revenue']:>10.2f}")
    net = sum(r["revenue"] for r in rep2["transactions"] if "RENT" in str(r["material"]))
    print("net rent revenue across sale+return:", repr(net), "(should be 0.0)")

    cement_rows = [r for r in rep2["transactions"]
                   if r["material"] == "CEMENT-OPC" and "R-2" in str(r.get("reference") or "")]
    print("\n=== 10 DISCOUNTED BAGS COME BACK ===")
    for r in cement_rows:
        print(f"  {str(r['transaction_type'])[:24]:24s} qty {r['qty']:>5}  "
              f"gross {r['gross_revenue']:>9.2f}  disc {r['discount']:>8.2f}  "
              f"rev {r['revenue']:>9.2f}")
    print("  (gross withdrawn + discount withdrawn must equal the net the sale earned:",
          f"{-10000.00:.2f} + 666.67 = {-9333.33:.2f})")
