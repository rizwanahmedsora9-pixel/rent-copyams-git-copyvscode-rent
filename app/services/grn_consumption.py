"""GRN lot consumption — the authoritative "how much of this GRN is left?" ledger.

One GRN item is one FIFO cost lot.  This module replays the *physical* stock
issues in chronological order over those lots and reports, per lot:

    received qty  ->  consumed qty  ->  remaining qty  ->  consumption %

Two kinds of evidence are combined, in this order, exactly like the Profit &
Loss projection in :mod:`app.services.profit_loss`:

1. **Recorded FIFO allocations** (``GRNAllocation``) written by the sale
   posting path.  These are authoritative: the lot they name is the lot that
   was actually costed against the dispatch, so it is consumed first.
2. **Reconstructed FIFO** for physical issues that carry no recorded
   allocation (legacy / imported dispatches, booking supplies, standalone
   dispatch ``Entry`` OUT rows).  Oldest lot first — lot date, then GRN id,
   then lot id — and a lot is available from the calendar day its GRN was
   posted (the same rule the counter uses when it allocates).

Material returns restore quantity to the lots their original dispatch consumed
(proportional to the cost layers that dispatch used), so a returned bag is not
reported as permanently consumed.  A return that cannot be tied to a lot
becomes a *dated return layer* used for costing only; it never inflates a GRN's
original quantity.

Nothing here writes to the database.  The stock ``Entry`` ledger, the GRN rows
and ``GRNAllocation`` stay the source of truth; this is a read-only projection,
and every GRN row it reports can be traced back to those tables.

One deliberate design note: the per-lot replay below mirrors
``profit_loss._Projection._build_fifo_cost_plan``.  ``tests/test_grn_consumption_indicator.py``
asserts that both replays end on the same lot balances, so the GRN indicator and
the reported FIFO cost can never disagree.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time

from sqlalchemy import or_

from models import (
    BookingAllocation,
    Client,
    DirectSale,
    DirectSaleItem,
    Entry,
    GRN,
    GRNAllocation,
    GRNItem,
    Material,
    MaterialReturn,
    Service,
    db,
)

EPS = 1e-6

# Lot status, most to least certain.
LOT_RECORDED = "recorded"          # consumed through posted FIFO allocations
LOT_RECONSTRUCTED = "reconstructed"  # consumed through the FIFO replay only
LOT_UNTOUCHED = "untouched"        # nothing consumed yet
LOT_UNKNOWN = "unknown"            # no received quantity; cannot be measured

CONSUMPTION_THRESHOLDS = (
    (25.0, "low"),
    (50.0, "moderate"),
    (75.0, "high"),
    (99.999, "critical"),
    (float("inf"), "complete"),
)


def _f(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def material_key(value):
    """Same normalisation used by the sale-allocation path."""
    return "".join(ch for ch in str(value or "").strip().lower() if ch.isalnum())


def _as_dt(value, time_value=None):
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    raw = str(value or "").strip()
    if not raw:
        return None
    if time_value:
        raw = f"{raw} {str(time_value).strip()}"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except (TypeError, ValueError):
            continue
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def _norm(value):
    return " ".join(str(value or "").strip().casefold().split())


def _grn_reference(grn):
    manual = (getattr(grn, "manual_bill_no", None) or "").strip()
    auto = (getattr(grn, "auto_bill_no", None) or "").strip()
    return manual or auto or f"GRN-{getattr(grn, 'id', '')}"


def consumption_level(pct):
    """Return the colour band name for a consumption percentage."""
    value = max(0.0, min(100.0, _f(pct)))
    for ceiling, name in CONSUMPTION_THRESHOLDS:
        if value < ceiling:
            return name
    return "complete"


def consumption_percent(received, consumed):
    """Consumption percentage, clamped to 0–100 and safe at zero quantity.

    A lot with no received quantity has no meaningful percentage; it reports
    0% (and status ``unknown``) rather than dividing by zero.
    """
    received = _f(received)
    consumed = _f(consumed)
    if received <= EPS:
        return 0.0
    return round(max(0.0, min(100.0, consumed / received * 100.0)), 2)


def _service_names():
    """Names (and codes) that are charges, never stock."""
    names = set()
    for service in Service.query.all():
        names.add(material_key(service.name))
        names.add(material_key(service.code))
    names.discard("")
    return names


def _is_service(name, service_keys):
    key = material_key(name)
    if not key:
        return False
    if key in service_keys:
        return True
    return "rent" in str(name or "").casefold()


class _LotLedger:
    """Chronological FIFO replay of physical issues over GRN lots."""

    def __init__(self, *, grn_ids=None):
        # ``grn_ids`` narrows what is *reported*, never what is replayed: a lot
        # only looks correctly consumed if every earlier lot of its material is
        # in the queue, even when the page shows a filtered subset.  Filtering
        # the replay instead would silently push an old dispatch onto a newer
        # lot and change a GRN's percentage when the reader types in the search
        # box.  The replay therefore always covers the whole ledger.
        self.report_ids = {int(g) for g in grn_ids} if grn_ids else None
        self.service_keys = _service_names()
        self.lots = {}                 # grn_item_id -> lot dict
        self.lots_by_material = defaultdict(list)
        self.lot_hits = defaultdict(list)   # grn_item_id -> [trace]
        self.reconstructed_qty = defaultdict(float)
        self.recorded_qty = defaultdict(float)
        self.warnings = []
        self._load_lots()
        self._load_allocations()
        self._load_issues()
        self._replay()

    # -- loading ---------------------------------------------------------
    def _load_lots(self):
        query = GRN.query.filter(or_(GRN.is_void.is_(False), GRN.is_void.is_(None)))
        grns = query.order_by(GRN.date_posted.asc(), GRN.id.asc()).all()
        items = (
            GRNItem.query.filter(GRNItem.grn_id.in_([g.id for g in grns]))
            .order_by(GRNItem.grn_id.asc(), GRNItem.id.asc())
            .all()
            if grns
            else []
        )
        items_by_grn = defaultdict(list)
        for item in items:
            if bool(getattr(item, "is_void", False)):
                continue
            items_by_grn[item.grn_id].append(item)
        self.grn_order = []
        for grn in grns:
            live_items = items_by_grn.get(grn.id, [])
            if not live_items:
                continue
            self.grn_order.append(grn)
            for item in live_items:
                qty = max(0.0, _f(item.qty))
                rate = max(0.0, _f(item.price_at_time))
                lot = {
                    "grn_item_id": item.id,
                    "grn_id": grn.id,
                    "reference": _grn_reference(grn),
                    "supplier": grn.supplier or "",
                    "date": _as_dt(grn.date_posted),
                    "material": item.mat_name or "",
                    "received": qty,
                    "remaining": qty,
                    "rate": rate,
                    "key": material_key(item.mat_name),
                }
                self.lots[item.id] = lot
                if lot["key"]:
                    self.lots_by_material[lot["key"]].append(lot)
        for lots in self.lots_by_material.values():
            lots.sort(key=lambda lot: (lot["date"] or datetime.min, lot["grn_id"], lot["grn_item_id"]))
        self.grn_by_id = {grn.id: grn for grn in grns}

    def _load_allocations(self):
        """Recorded lot consumption per sale line (void-safe)."""
        self.allocations_by_sale_item = defaultdict(list)
        rows = (
            db.session.query(
                GRNAllocation.id,
                GRNAllocation.sale_item_id,
                GRNAllocation.grn_item_id,
                GRNAllocation.qty,
                GRNAllocation.cost_rate,
                DirectSale.id,
                DirectSale.date_posted,
            )
            .join(DirectSale, GRNAllocation.sale_id == DirectSale.id)
            .filter(
                or_(GRNAllocation.is_void.is_(False), GRNAllocation.is_void.is_(None)),
                or_(DirectSale.is_void.is_(False), DirectSale.is_void.is_(None)),
            )
            .order_by(GRNAllocation.id.asc())
            .all()
        )
        for alloc_id, sale_item_id, grn_item_id, qty, cost_rate, sale_id, posted in rows:
            if grn_item_id not in self.lots or _f(qty) <= EPS:
                continue
            self.allocations_by_sale_item[sale_item_id].append({
                "id": alloc_id,
                "grn_item_id": int(grn_item_id),
                "qty": _f(qty),
                "cost_rate": _f(cost_rate),
                "sale_id": sale_id,
                "date": _as_dt(posted),
            })

    def _client_canonical(self):
        """Name -> canonical client name, exactly as the P&L resolves parties.

        Returns are matched to the original dispatch by client, and legacy rows
        sometimes store a code or a differently spaced name; the P&L compares
        canonical master names, so this replay must too.
        """
        self.client_by_name = {}
        self.client_by_code = {}
        self.client_names = {}
        for client in Client.query.order_by(Client.id.asc()).all():
            self.client_by_name.setdefault(_norm(client.name), client.name)
            if client.code:
                self.client_by_code.setdefault(_norm(client.code), client.name)
            self.client_names[client.id] = client.name

    def canonical_client(self, name, code=None):
        if code:
            found = self.client_by_code.get(_norm(code))
            if found:
                return found
        if name:
            found = self.client_by_name.get(_norm(name))
            if found:
                return found
        return name or ""

    def _booking_context(self):
        """Booking item ids and the refs/names needed to spot booked dispatches."""
        from models import Booking, BookingItem

        self.booking_item_ids = set()
        self.booking_refs = set()
        self.booking_material_by_client = defaultdict(set)
        bookings = Booking.query.order_by(Booking.id.asc()).all()
        items_by_booking = defaultdict(list)
        if bookings:
            for item in BookingItem.query.filter(
                BookingItem.booking_id.in_([b.id for b in bookings])
            ).order_by(BookingItem.id.asc()).all():
                items_by_booking[item.booking_id].append(item)
                self.booking_item_ids.add(item.id)
        for booking in bookings:
            if bool(getattr(booking, "is_void", False)):
                continue
            client_name = self.canonical_client(booking.client_name)
            for ref in (booking.manual_bill_no, booking.auto_bill_no):
                if ref:
                    self.booking_refs.add(_norm(ref))
            for item in items_by_booking.get(booking.id, []):
                if material_key(item.material_name):
                    self.booking_material_by_client[_norm(client_name)].add(material_key(item.material_name))

    def _entry_looks_booked(self, entry_like, client_name):
        """Mirror the P&L rule for standalone dispatch rows."""
        text = " ".join(str(entry_like.get(field) or "") for field in (
            "transaction_category", "client_category", "nimbus_no", "transaction_type",
        ))
        refs = {
            _norm(entry_like.get("bill_no")),
            _norm(entry_like.get("auto_bill_no")),
            _norm(entry_like.get("source_bill_no")),
        } - {""}
        looks_booked = (
            "book" in text.casefold()
            or bool((entry_like.get("booked_material") or "").strip())
            or bool(refs & self.booking_refs)
        )
        if not looks_booked:
            return False
        booked_name = entry_like.get("booked_material") or entry_like.get("material")
        return material_key(booked_name) in self.booking_material_by_client.get(_norm(client_name), set())

    def _load_issues(self):
        """Build the physical-issue event list, oldest first.

        The rules mirror ``profit_loss._Projection._build_fifo_cost_plan``:
        a supplied line dispatches its chargeable quantity (quantity not already
        attributed to a booking allocation), every active booking allocation
        dispatches its own quantity, unlinked zero-priced rows still dispatch
        physically, and legacy OUT entries with no linked sale are dispatches.
        """
        events = []
        self._client_canonical()
        self._booking_context()

        booking_alloc_by_sale_item = defaultdict(list)
        for alloc in (
            BookingAllocation.query.filter(
                or_(BookingAllocation.is_void.is_(False), BookingAllocation.is_void.is_(None))
            )
            .order_by(BookingAllocation.id.asc())
            .all()
        ):
            booking_alloc_by_sale_item[alloc.sale_item_id].append(alloc)

        sale_rows = (
            db.session.query(
                DirectSale.id,
                DirectSale.date_posted,
                DirectSale.category,
                DirectSale.client_name,
                DirectSale.client_code,
                DirectSaleItem.id,
                DirectSaleItem.product_name,
                DirectSaleItem.qty,
                DirectSaleItem.price_at_time,
            )
            .join(DirectSaleItem, DirectSaleItem.sale_id == DirectSale.id)
            .filter(or_(DirectSale.is_void.is_(False), DirectSale.is_void.is_(None)))
            .all()
        )
        for sale_id, posted, category, client_name, client_code, item_id, product, qty, rate in sale_rows:
            if not material_key(product) or _is_service(product, self.service_keys):
                continue
            qty = _f(qty)
            rate = _f(rate)
            if qty <= EPS:
                continue
            allocations = booking_alloc_by_sale_item.get(item_id, [])
            category_key = str(category or "").strip().casefold()
            free_direct = rate <= 0 and not allocations and category_key not in {
                "mixed transaction", "booking delivery",
            }
            chargeable = 0.0
            if rate > 0 or free_direct:
                booked_qty = sum(_f(a.qty) for a in allocations)
                if booked_qty > EPS and category_key in {"mixed transaction", "booking delivery"}:
                    chargeable = max(0.0, qty - booked_qty)
                else:
                    chargeable = qty
            canonical_client = self.canonical_client(client_name, client_code)
            if chargeable > EPS:
                events.append((
                    _as_dt(posted), sale_id, 0, item_id, "direct", item_id,
                    product, chargeable, canonical_client, "direct",
                ))
            if allocations:
                for alloc in allocations:
                    if _f(alloc.qty) <= EPS:
                        continue
                    if alloc.booking_item_id not in self.booking_item_ids:
                        continue
                    events.append((
                        _as_dt(posted), sale_id, 1, alloc.id, "booking", alloc.id,
                        product, _f(alloc.qty), canonical_client, "booked",
                    ))
            elif chargeable <= EPS and qty > EPS and rate <= 0:
                # Unlinked zero-priced physical rows: no revenue, but the goods
                # still left the yard.
                events.append((
                    _as_dt(posted), sale_id, 2, item_id, "unrecognized", item_id,
                    product, qty, canonical_client, "booked",
                ))

        # Legacy dispatch rows with no linked sale document.
        linked_entry_ids = {
            row[0]
            for row in db.session.query(Entry.id)
            .filter(
                or_(
                    Entry.nimbus_no == "Direct Sale",
                    (Entry.source_module == "sales")
                    & (Entry.source_table == "direct_sale")
                    & (Entry.source_id.isnot(None)),
                )
            )
            .all()
        }
        entry_rows = (
            db.session.query(
                Entry.id, Entry.date, Entry.time, Entry.material, Entry.booked_material,
                Entry.qty, Entry.client, Entry.client_code, Entry.bill_no, Entry.auto_bill_no,
                Entry.source_bill_no, Entry.transaction_category, Entry.client_category,
                Entry.nimbus_no, Entry.transaction_type,
            )
            .filter(
                Entry.type == "OUT",
                or_(Entry.is_void.is_(False), Entry.is_void.is_(None)),
                Entry.qty > 0,
            )
            .all()
        )
        for row in entry_rows:
            (entry_id, entry_date, entry_time, material, booked_material, qty, client_name,
             client_code, bill_no, auto_bill_no, source_bill_no, transaction_category,
             client_category, nimbus_no, transaction_type) = row
            if entry_id in linked_entry_ids:
                continue
            if not material_key(material) or _is_service(material, self.service_keys):
                continue
            canonical_client = self.canonical_client(client_name, client_code)
            entry_like = {
                "material": material, "booked_material": booked_material,
                "bill_no": bill_no, "auto_bill_no": auto_bill_no,
                "source_bill_no": source_bill_no, "transaction_category": transaction_category,
                "client_category": client_category, "nimbus_no": nimbus_no,
                "transaction_type": transaction_type,
            }
            # Standalone entries only join the return-restoration pool when they
            # look like a booking dispatch, exactly as the P&L requires.
            bookable = self._entry_looks_booked(entry_like, canonical_client)
            events.append((
                _as_dt(entry_date, entry_time) or _as_dt(entry_date), entry_id or 0, 3,
                entry_id or 0, "entry", entry_id, material, _f(qty), canonical_client,
                "booked" if bookable else "entry-only",
            ))

        # Returns restore quantity to the lots the original dispatch consumed.
        for ret in (
            MaterialReturn.query.filter(
                or_(MaterialReturn.is_void.is_(False), MaterialReturn.is_void.is_(None))
            )
            .order_by(MaterialReturn.date_posted.asc(), MaterialReturn.id.asc())
            .all()
        ):
            for return_item in ret.items:
                if _f(return_item.qty) <= EPS or _is_service(return_item.material_name, self.service_keys):
                    continue
                events.append((
                    _as_dt(ret.date_posted), ret.id, 4, return_item.id, "return", return_item.id,
                    return_item.material_name, _f(return_item.qty),
                    self.canonical_client(ret.client_name),
                    "booked" if (ret.return_type or "normal").strip().lower() == "booked" else "direct",
                ))

        events.sort(key=lambda event: (event[0] or datetime.max, event[1] or 0, event[2], event[3] or 0))
        self.events = events

    # -- replay ----------------------------------------------------------
    def _lot_available(self, lot, tx_dt):
        if not lot["date"] or not tx_dt:
            return True
        return lot["date"].date() <= tx_dt.date()

    def _consume_fifo(self, material, qty, tx_dt):
        remaining = max(0.0, qty)
        layers = []
        for lot in self.lots_by_material.get(material_key(material), []):
            if remaining <= EPS:
                break
            if not self._lot_available(lot, tx_dt):
                continue
            take = min(remaining, lot["remaining"])
            if take <= EPS:
                continue
            lot["remaining"] -= take
            remaining -= take
            if lot.get("grn_item_id") is not None:
                layers.append((lot["grn_item_id"], take))
        return layers, remaining

    def _lot_history_exists(self, material, tx_dt):
        return any(
            self._lot_available(lot, tx_dt)
            for lot in self.lots_by_material.get(material_key(material), [])
        )

    def _consume_forward(self, material, qty):
        """Consume the next lots for a dispatch that predates every purchase.

        Mirrors ``profit_loss``: a delivery posted against negative stock is
        attributed, oldest lot first, to the purchases that later covered it,
        so the indicator's remaining quantity agrees with the costed quantity.
        """
        remaining = max(0.0, qty)
        layers = []
        for lot in self.lots_by_material.get(material_key(material), []):
            if remaining <= EPS:
                break
            take = min(remaining, lot["remaining"])
            if take <= EPS:
                continue
            lot["remaining"] -= take
            remaining -= take
            layers.append((lot["grn_item_id"], take))
        return layers, remaining

    def _consume_specific(self, grn_item_id, qty, tx_dt, *, recorded=True):
        lot = self.lots.get(grn_item_id)
        if lot is None or qty <= EPS or not self._lot_available(lot, tx_dt):
            return 0.0
        take = min(qty, lot["remaining"])
        if take <= EPS:
            return 0.0
        lot["remaining"] -= take
        if recorded:
            self.recorded_qty[grn_item_id] += take
        return take

    def _replay(self):
        supply_candidates = []
        for tx_dt, owner_id, priority, sequence, kind, object_id, material, qty, client_name, origin in self.events:
            if kind == "return":
                self._restore_return(tx_dt, owner_id, material, qty, client_name, origin, supply_candidates)
                continue
            layers = []
            covered = 0.0
            if kind == "direct":
                for alloc in self.allocations_by_sale_item.get(object_id, []):
                    take = min(alloc["qty"], max(0.0, qty - covered))
                    if take <= EPS:
                        continue
                    # ``covered`` counts the promised quantity even when the lot
                    # is already exhausted, exactly like the P&L replay, so both
                    # end on the same residual and the same lot balances.
                    covered += take
                    self._consume_specific(alloc["grn_item_id"], take, tx_dt, recorded=True)
                    layers.append((alloc["grn_item_id"], take))
            residual = max(0.0, qty - covered)
            if residual > EPS:
                fifo_layers, unavailable = self._consume_fifo(material, residual, tx_dt)
                for grn_item_id, take in fifo_layers:
                    self.reconstructed_qty[grn_item_id] += take
                # Synthetic return layers are consumed by the replay too, but
                # they belong to no GRN and never appear in the indicator.
                layers.extend(fifo_layers)
                if unavailable > EPS and not self._lot_history_exists(material, tx_dt):
                    # Negative-stock dispatch: the units were covered by later
                    # purchases, so attribute them there (oldest first) exactly
                    # like the P&L cost reconstruction.
                    forward_layers, unavailable = self._consume_forward(material, unavailable)
                    for grn_item_id, take in forward_layers:
                        self.reconstructed_qty[grn_item_id] += take
                    layers.extend(forward_layers)
                if unavailable > EPS:
                    self.warnings.append(
                        f"{material}: {unavailable:g} dispatched before any dated GRN lot was available; "
                        "no GRN quantity was attributed to it."
                    )
            # Only direct supplies and booked dispatches feed the return pool;
            # an unlinked zero-priced row is not a bill that can be returned.
            # Candidates are remembered even when no lot could be attributed,
            # because a return consumes the candidate quantity all the same.
            if kind in {"direct", "booking", "entry"} and origin != "entry-only":
                supply_candidates.append({
                    "origin": "direct" if kind == "direct" else "booked",
                    "material": material_key(material),
                    "client": _norm(client_name),
                    "date": tx_dt,
                    "object_id": object_id or owner_id or 0,
                    "qty": qty,
                    "remaining": qty,
                    "layers": layers,
                })

    def _restore_return(self, tx_dt, ret_id, material, qty, client_name, origin, supply_candidates):
        """Give returned quantity back to the lots its dispatch consumed.

        Matching is the same conservative FIFO pool the P&L uses: same origin
        (normal vs booked), same material, same canonical client, dispatched on
        or before the return date, oldest dispatch first.
        """
        remaining = max(0.0, qty)
        candidates = [
            candidate for candidate in supply_candidates
            if candidate["origin"] == origin
            and candidate["material"] == material_key(material)
            and candidate["client"] == _norm(client_name)
            and candidate["remaining"] > EPS
            and (candidate["date"] or datetime.max) <= (tx_dt or datetime.min)
        ]
        candidates.sort(key=lambda candidate: (candidate["date"] or datetime.min, candidate["object_id"] or 0))
        for candidate in candidates:
            if remaining <= EPS:
                break
            take = min(remaining, candidate["remaining"])
            if take <= EPS:
                continue
            candidate["remaining"] -= take
            remaining -= take
            restored = 0.0
            for grn_item_id, layer_qty in candidate["layers"]:
                share = take * layer_qty / candidate["qty"] if candidate["qty"] > EPS else 0.0
                lot = self.lots.get(grn_item_id)
                if lot is None or share <= EPS:
                    continue
                before = lot["remaining"]
                lot["remaining"] = min(lot["received"], lot["remaining"] + share)
                gained = lot["remaining"] - before
                restored += gained
                if gained > EPS:
                    # A returned unit is consumed again, not merely moved.
                    if self.reconstructed_qty[grn_item_id] >= gained:
                        self.reconstructed_qty[grn_item_id] -= gained
                    else:
                        leftover = gained - self.reconstructed_qty[grn_item_id]
                        self.reconstructed_qty[grn_item_id] = 0.0
                        self.recorded_qty[grn_item_id] = max(
                            0.0, self.recorded_qty[grn_item_id] - leftover
                        )
            synthetic_qty = max(0.0, take - restored)
            if synthetic_qty > EPS:
                # Returned goods with no surviving lot: keep them as a dated
                # return layer so later dispatches do not consume a GRN lot the
                # P&L left untouched.
                self._add_synthetic_return_lot(tx_dt, material, synthetic_qty, f"RTN-{ret_id}")
        if remaining > EPS:
            self.warnings.append(
                f"Return {ret_id}: {remaining:g} of {material} could not be traced to an original "
                "dispatch lot; the lot balances are unaffected."
            )

    def _add_synthetic_return_lot(self, tx_dt, material, qty, reference=""):
        """Returned quantity with no surviving GRN lot.

        The P&L keeps this as a dated return cost layer so later issues are not
        valued from an unrelated purchase.  Here it must also absorb later
        dispatches, otherwise those dispatches would consume a GRN lot that the
        P&L left untouched and the two lot balances would diverge.
        """
        lot = {
            "grn_item_id": None,
            "grn_id": None,
            "reference": reference,
            "supplier": "",
            "date": tx_dt,
            "material": material,
            "received": qty,
            "remaining": qty,
            "rate": 0.0,
            "key": material_key(material),
            "synthetic": True,
        }
        self.lots_by_material[lot["key"]].append(lot)
        self.lots_by_material[lot["key"]].sort(
            key=lambda row: (row["date"] or datetime.min, row["grn_id"] or 0, row["grn_item_id"] or 0)
        )


def build_grn_consumption(grn_ids=None, *, ledger=None):
    """Return the complete GRN consumption view model.

    ``GRNItem.qty`` is the received quantity; consumption is derived from the
    FIFO replay (recorded allocations first, then reconstructed FIFO), never by
    subtracting total sales from every GRN independently.
    """
    ledger = ledger or _LotLedger(grn_ids=grn_ids)
    units = {material_key(m.name): (m.unit or "") for m in Material.query.all()}

    report_ids = getattr(ledger, "report_ids", None)

    def _reported(lot):
        return report_ids is None or lot["grn_id"] in report_ids

    items = {}
    for grn_item_id, lot in ledger.lots.items():
        if not _reported(lot):
            continue
        received = lot["received"]
        consumed = max(0.0, received - lot["remaining"])
        remaining = max(0.0, lot["remaining"])
        recorded = min(consumed, ledger.recorded_qty.get(grn_item_id, 0.0))
        reconstructed = min(max(0.0, consumed - recorded), ledger.reconstructed_qty.get(grn_item_id, 0.0))
        if received <= EPS:
            status = LOT_UNKNOWN
        elif consumed <= EPS:
            status = LOT_UNTOUCHED
        elif recorded >= consumed - EPS:
            status = LOT_RECORDED
        else:
            status = LOT_RECONSTRUCTED
        pct = consumption_percent(received, consumed)
        items[grn_item_id] = {
            "grn_item_id": grn_item_id,
            "grn_id": lot["grn_id"],
            "reference": lot["reference"],
            "supplier": lot["supplier"],
            "date": lot["date"],
            "material": lot["material"],
            "unit": units.get(lot["key"], ""),
            "received_qty": received,
            "consumed_qty": consumed,
            "remaining_qty": remaining,
            "consumed_pct": pct,
            "remaining_pct": round(100.0 - pct, 2) if received > EPS else 0.0,
            "level": consumption_level(pct),
            "status": status,
            "fully_consumed": bool(received > EPS and remaining <= EPS),
            "over_consumed": bool(consumed > received + EPS),
            "rate": lot["rate"],
            "received_value": received * lot["rate"],
            "consumed_value": consumed * lot["rate"],
            "remaining_value": remaining * lot["rate"],
            "recorded_consumed_qty": recorded,
            "reconstructed_consumed_qty": reconstructed,
        }

    grns = {}
    for grn in ledger.grn_order:
        if report_ids is not None and grn.id not in report_ids:
            continue
        item_ids = [i.id for i in grn.items if i.id in items and not bool(getattr(i, "is_void", False))]
        if not item_ids:
            continue
        rows = [items[i] for i in item_ids]
        received = sum(row["received_qty"] for row in rows)
        consumed = sum(row["consumed_qty"] for row in rows)
        pct = consumption_percent(received, consumed)
        grns[grn.id] = {
            "grn_id": grn.id,
            "reference": _grn_reference(grn),
            "manual_bill_no": grn.manual_bill_no or "",
            "auto_bill_no": grn.auto_bill_no or "",
            "supplier": grn.supplier or "",
            "date": _as_dt(grn.date_posted),
            "received_qty": received,
            "consumed_qty": consumed,
            "remaining_qty": max(0.0, received - consumed),
            "consumed_pct": pct,
            "level": consumption_level(pct),
            "fully_consumed": bool(received > EPS and consumed >= received - EPS),
            "item_ids": item_ids,
            "items": rows,
            "remaining_value": sum(row["remaining_value"] for row in rows),
            "received_value": sum(row["received_value"] for row in rows),
            "reconstructed_qty": sum(row["reconstructed_consumed_qty"] for row in rows),
        }

    by_material = defaultdict(lambda: {
        "material": "", "unit": "", "received_qty": 0.0, "consumed_qty": 0.0,
        "remaining_qty": 0.0, "remaining_value": 0.0, "lot_count": 0,
    })
    for row in items.values():
        bucket = by_material[row["material"] or "-"]
        bucket["material"] = row["material"] or "-"
        bucket["unit"] = bucket["unit"] or row["unit"]
        bucket["received_qty"] += row["received_qty"]
        bucket["consumed_qty"] += row["consumed_qty"]
        bucket["remaining_qty"] += row["remaining_qty"]
        bucket["remaining_value"] += row["remaining_value"]
        bucket["lot_count"] += 1
    material_rows = sorted(
        by_material.values(), key=lambda row: row["remaining_value"], reverse=True
    )
    for row in material_rows:
        row["average_remaining_rate"] = (
            row["remaining_value"] / row["remaining_qty"] if row["remaining_qty"] > EPS else 0.0
        )

    total_remaining_value = sum(row["remaining_value"] for row in items.values())
    total_remaining_qty = sum(row["remaining_qty"] for row in items.values())
    notes = [
        "Consumption is the FIFO lot replay (recorded GRNAllocation rows first, then oldest-lot-first "
        "for physical dispatches that carry no recorded allocation).",
        "A GRN whose lots were consumed only by the replay is labelled reconstructed, not recorded.",
        "Material returns restore the quantity to the lots the original dispatch consumed.",
        "100% consumed means the received quantity has been dispatched or otherwise accounted for by the "
        "movement rules above; it is not a profit percentage.",
    ]
    return {
        "items": items,
        "grns": grns,
        "by_material": material_rows,
        "inventory": {
            "total_qty": total_remaining_qty,
            "total_value": total_remaining_value,
            "lot_count": len(items),
            "by_material": material_rows,
        },
        "reconstructed_lot_count": sum(1 for row in items.values() if row["status"] == LOT_RECONSTRUCTED),
        "recorded_lot_count": sum(1 for row in items.values() if row["status"] == LOT_RECORDED),
        "fully_consumed_lot_count": sum(1 for row in items.values() if row["fully_consumed"]),
        "warnings": sorted(set(ledger.warnings)),
        "notes": notes,
    }


def grn_consumption_for_page(grns):
    """Attach consumption data to a list of GRN ORM rows (no extra queries)."""
    if not grns:
        return {"grns": {}, "items": {}, "by_material": [], "inventory": {
            "total_qty": 0.0, "total_value": 0.0, "lot_count": 0, "by_material": [],
        }}
    return build_grn_consumption(grn_ids=[grn.id for grn in grns])
