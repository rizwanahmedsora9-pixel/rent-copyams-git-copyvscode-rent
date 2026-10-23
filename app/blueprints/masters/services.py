"""Services master — charges that are billed like a material but are never stock.

Rent (``RENT-CEMENT``, ``RENT-STEEL``, …) is the motivating case. It is charged
to the client per unit on an ordinary bill line, discounted, booked, consumed
against a booking and settled by the same payment — but no goods ever move, so
it must have no stock, no GRN and no place in a stock report.

Because ``BookingItem.material_name`` / ``DirectSaleItem.product_name`` /
``Entry.material`` are free-text strings rather than FKs, converting a Material
into a Service needs **no historical data change at all**: the name simply
resolves against the other master from that moment on. ``/services/migrate``
does exactly that, and is deliberately the only way the conversion happens.
"""
from ._common import *  # noqa

from flask import current_app
from app.services.lookups import (
    invalidate_service_cache,
    is_service_name,
    _material_lookup_key,
    _material_norm_key,
)
from app.services.void_rebuild import _rebuild_material_totals


def _invalidate_all_service_caches():
    invalidate_service_cache()
    try:
        current_app._dropdown_cache = None
    except Exception:
        pass


def _norm(value):
    return _material_norm_key(value)


def _matches_any(value, names, codes):
    key = _norm(value)
    return bool(key) and (key in names or key in codes)


def _collision_error(name, code, exclude_service_id=None, exclude_material_ids=None):
    """Refuse a name/code that would make resolution ambiguous.

    Both masters resolve by name **and** code, so either one colliding means a
    line could silently resolve to the wrong record. SQLite cannot express a
    cross-table unique constraint, so this is enforced here.

    Within Service we use the exact lookup key (matching Material's own rule so
    legacy ``RENT-STEEL`` and ``RENT STEEL`` can both convert); across
    Service <-> Material we also check the normalised key so punctuation
    variants cannot straddle the two tables.
    """
    exclude_mids = set(exclude_material_ids or ())
    lookup_name = _material_lookup_key(name)
    lookup_code = _material_lookup_key(code)
    norm_name = _norm(name)
    norm_code = _norm(code)

    q = Service.query.filter(Service.id != exclude_service_id) if exclude_service_id else Service.query
    for svc in q.all():
        if lookup_name and _material_lookup_key(svc.name) == lookup_name:
            return f'A service named "{svc.name}" already exists.'
        if lookup_code and _material_lookup_key(svc.code) == lookup_code:
            return f'A service with code "{svc.code}" already exists.'

    for mat in Material.query.all():
        if mat.id in exclude_mids:
            continue
        if (lookup_name and _material_lookup_key(mat.name) == lookup_name) or (norm_name and _norm(mat.name) == norm_name):
            return f'A material named "{mat.name}" already exists.'
        if (lookup_code and _material_lookup_key(mat.code) == lookup_code) or (norm_code and _norm(mat.code) == norm_code):
            return f'A material with code "{mat.code}" already exists.'
    return None


def _service_usage(name):
    """How many historical rows carry this name — used before allowing a delete."""
    key = (name or '').strip()
    if not key:
        return 0
    lower = key.lower()
    count = 0
    count += Entry.query.filter(func.lower(func.trim(Entry.material)) == lower).count()
    count += BookingItem.query.filter(func.lower(func.trim(BookingItem.material_name)) == lower).count()
    count += DirectSaleItem.query.filter(func.lower(func.trim(DirectSaleItem.product_name)) == lower).count()
    count += MaterialReturnItem.query.filter(func.lower(func.trim(MaterialReturnItem.material_name)) == lower).count()
    return count


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@bp.route('/services')
@login_required
def services():
    if not (getattr(current_user, 'can_manage_materials', False) or current_user.role == 'admin'):
        flash('You do not have permission to manage services.', 'danger')
        return redirect('/')

    q = (request.args.get('q') or '').strip()
    query = Service.query
    if q:
        query = query.filter(Service.name.ilike(f'%{q}%'))
    services_list = query.order_by(Service.name.asc()).all()

    # Rent materials still living in the Material master — what the migrate
    # button would convert. Shown so the operator can see the pending work.
    pending_rent = [m for m in Material.query.order_by(Material.name.asc()).all()
                    if _is_rent_like(m.name)]
    orphan_rent_names = _find_orphan_rent_entry_names(services_list, pending_rent)
    pending_rent_entries = sum(_service_usage(m.name) for m in pending_rent) + sum(
        _service_usage(n) for n in orphan_rent_names
    )

    rows = []
    for svc in services_list:
        rows.append({
            'service': svc,
            'usage': _service_usage(svc.name),
        })

    return render_template(
        'services.html',
        services=services_list,
        rows=rows,
        pending_rent=pending_rent,
        orphan_rent_names=orphan_rent_names,
        pending_rent_entries=pending_rent_entries,
        q=q,
    )


def _is_rent_like(name):
    """The historic rent rule, used to find candidate rows to convert."""
    txt = (name or '').strip().lower().replace('-', ' ').replace('_', ' ')
    return 'rent' in txt


def _find_orphan_rent_entry_names(existing_services, pending_materials):
    """Find distinct rent-like labels in historical entries/items that have
    neither a Service row nor a pending Material row."""
    known = {_material_lookup_key(s.name) for s in (existing_services or []) if s.name}
    known |= {_material_lookup_key(m.name) for m in (pending_materials or []) if m.name}
    found = {}
    for model, col in (
        (Entry, Entry.material),
        (Entry, Entry.booked_material),
        (BookingItem, BookingItem.material_name),
        (DirectSaleItem, DirectSaleItem.product_name),
        (MaterialReturnItem, MaterialReturnItem.material_name),
    ):
        for (raw,) in db.session.query(func.distinct(col)).filter(col.isnot(None)).all():
            clean = (raw or '').strip()
            if not clean or not _is_rent_like(clean):
                continue
            key = _material_lookup_key(clean)
            if key and key not in known and key not in found:
                found[key] = clean
    return sorted(found.values())


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@bp.route('/services/add', methods=['POST'])
@login_required
def add_service():
    name = (request.form.get('service_name') or '').strip()
    code = (request.form.get('service_code') or '').strip()
    unit = (request.form.get('service_unit') or '').strip() or 'Pcs'
    if not name:
        flash('Service name is required.', 'danger')
        return redirect(url_for('masters.services'))
    if not code:
        code = _next_service_code(name)

    err = _collision_error(name, code)
    if err:
        flash(err, 'danger')
        return redirect(url_for('masters.services'))

    try:
        svc = Service(name=name, code=code, unit=unit, unit_price=0)
        db.session.add(svc)
        db.session.commit()
        _invalidate_all_service_caches()
        flash(f'Service "{name}" added.', 'success')
    except Exception:
        db.session.rollback()
        logging.getLogger(__name__).exception('add_service failed for %r', name)
        flash('The service could not be saved. Check the name and code are unique.', 'danger')
    return redirect(url_for('masters.services'))


@bp.route('/services/edit/<int:id>', methods=['POST'])
@login_required
def edit_service(id):
    svc = db.session.get(Service, id)
    if not svc:
        flash('Service not found.', 'danger')
        return redirect(url_for('masters.services'))

    new_name = (request.form.get('service_name') or '').strip()
    new_code = (request.form.get('service_code') or '').strip()
    new_unit = (request.form.get('service_unit') or '').strip() or 'Pcs'
    if not new_name:
        flash('Service name is required.', 'danger')
        return redirect(url_for('masters.services'))
    if not new_code:
        new_code = svc.code

    err = _collision_error(new_name, new_code, exclude_service_id=id)
    if err:
        flash(err, 'danger')
        return redirect(url_for('masters.services'))

    old_name = svc.name
    try:
        svc.name = new_name
        svc.code = new_code
        svc.unit = new_unit
        db.session.flush()
        # Renaming must cascade into the free-text columns, exactly as it does
        # for materials, or every historical row would orphan silently.
        if old_name.strip().lower() != new_name.strip().lower():
            _cascade_rename_service_label(old_name, new_name)
        db.session.commit()
        _invalidate_all_service_caches()
        flash(f'Service updated to "{new_name}". Historical records updated.', 'success')
    except Exception:
        db.session.rollback()
        logging.getLogger(__name__).exception('edit_service failed for id=%s', id)
        flash('The service could not be updated.', 'danger')
    return redirect(url_for('masters.services'))


@bp.route('/services/toggle/<int:id>', methods=['POST'])
@login_required
def toggle_service(id):
    svc = db.session.get(Service, id)
    if not svc:
        flash('Service not found.', 'danger')
        return redirect(url_for('masters.services'))
    try:
        svc.is_active = not bool(svc.is_active)
        db.session.commit()
        _invalidate_all_service_caches()
        state = 'activated' if svc.is_active else 'suspended'
        flash(f'Service "{svc.name}" {state}.', 'success')
    except Exception:
        db.session.rollback()
        flash('The service could not be updated.', 'danger')
    return redirect(url_for('masters.services'))


@bp.route('/services/delete/<int:id>', methods=['POST'])
@login_required
def delete_service(id):
    """Refuse while history references the name — deactivate instead."""
    svc = db.session.get(Service, id)
    if not svc:
        flash('Service not found.', 'danger')
        return redirect(url_for('masters.services'))
    usage = _service_usage(svc.name)
    if usage:
        flash(
            f'Cannot delete "{svc.name}": {usage} historical record(s) still '
            'reference it. Suspend it instead — suspending keeps every old bill, '
            'entry and booking intact.',
            'danger',
        )
        return redirect(url_for('masters.services'))
    try:
        db.session.delete(svc)
        db.session.commit()
        _invalidate_all_service_caches()
        flash(f'Service "{svc.name}" deleted.', 'success')
    except Exception:
        db.session.rollback()
        flash('The service could not be deleted.', 'danger')
    return redirect(url_for('masters.services'))


def _next_service_code(name):
    base = re.sub(r'[^A-Za-z0-9]+', '', (name or '').strip().upper())[:8] or 'SVC'
    prefix = f'SVC-{base}'
    existing = {
        (s.code or '').strip().upper() for s in Service.query.all()
    }
    existing |= {(m.code or '').strip().upper() for m in Material.query.all()}
    if prefix not in existing:
        return prefix
    i = 2
    while f'{prefix}-{i}' in existing:
        i += 1
    return f'{prefix}-{i}'


def _cascade_rename_service_label(old_value, new_value):
    """Rename a service across every free-text column that can hold it."""
    old_value = (old_value or '').strip()
    new_value = (new_value or '').strip()
    if not old_value or not new_value or old_value.lower() == new_value.lower():
        return

    def _rename_filter(col, old):
        old_lower = old.lower()
        old_nospace = old_lower.replace(' ', '')
        return or_(
            func.lower(func.trim(col)) == old_lower,
            func.lower(func.replace(func.trim(col), ' ', '')) == old_nospace,
        )

    updates = [
        (Entry, Entry.material),
        (Entry, Entry.booked_material),
        (BookingItem, BookingItem.material_name),
        (DirectSaleItem, DirectSaleItem.product_name),
        (MaterialReturnItem, MaterialReturnItem.material_name),
        (DeliveryItem, DeliveryItem.product),
        (ReconBasket, ReconBasket.inv_material),
    ]
    for model, col in updates:
        model.query.filter(_rename_filter(col, old_value)).update(
            {col: new_value}, synchronize_session=False
        )


# ---------------------------------------------------------------------------
# Migration — convert rent materials into services
# ---------------------------------------------------------------------------

@bp.route('/services/migrate', methods=['POST'])
@login_required
def migrate_rent_to_services():
    """Convert every rent-like Material row into a Service, in one transaction.

    No historical row is modified. Entry / BookingItem / DirectSaleItem store
    the name as free text, so after this runs those 1,607 sale lines and 301
    booking lines resolve against the Service master instead — which is what
    makes them stop being stock while every bill, ledger and PDF stays exactly
    as it is.

    Guards before doing anything:
      * refuse if a GRN line references a candidate (that would be a purchase
        of a charge — a fake one — and needs voiding first);
      * refuse if a Service with the same name or code already exists.
    """
    confirmed = (request.form.get('confirm') or '').strip() == 'yes'
    if not confirmed:
        flash('Tick the confirmation box to convert rent materials into services.', 'warning')
        return redirect(url_for('masters.services'))

    candidates = [m for m in Material.query.order_by(Material.name.asc()).all()
                  if _is_rent_like(m.name)]
    existing_services = Service.query.all()
    orphan_names = _find_orphan_rent_entry_names(existing_services, candidates)
    if not candidates and not orphan_names:
        flash('Nothing to convert — all rent materials and entries are already in Services.', 'info')
        return redirect(url_for('masters.services'))

    # --- pre-flight -------------------------------------------------------
    blocks = []
    candidate_ids = {m.id for m in candidates}
    for mat in candidates:
        grn_lines = GRNItem.query.filter(
            func.lower(func.trim(GRNItem.mat_name)) == (mat.name or '').strip().lower()
        ).count()
        if grn_lines:
            blocks.append(f'{mat.name}: {grn_lines} GRN line(s) exist — void them first.')
        err = _collision_error(mat.name, mat.code, exclude_material_ids=candidate_ids)
        if err:
            blocks.append(f'{mat.name}: {err}')
    if blocks:
        for b in blocks:
            flash(b, 'danger')
        return redirect(url_for('masters.services'))

    # --- convert ----------------------------------------------------------
    converted = []
    try:
        for mat in candidates:
            name = (mat.name or '').strip()
            usage = _service_usage(name)
            db.session.add(Service(
                name=name,
                code=mat.code,
                unit=(mat.unit or 'Pcs'),
                # Carried across purely so the value is preserved in data; it is
                # never used to price a line. See SERVICES_PLAN.md §15 #2.
                unit_price=float(mat.unit_price or 0),
                converted_from_material_id=mat.id,
            ))
            db.session.delete(mat)
            converted.append((name, usage))

        db.session.flush()
        for orphan_name in orphan_names:
            code = _next_service_code(orphan_name)
            usage = _service_usage(orphan_name)
            db.session.add(Service(
                name=orphan_name,
                code=code,
                unit='Pcs',
                unit_price=0,
            ))
            converted.append((orphan_name, usage))

        db.session.flush()
        _rebuild_material_totals()
        db.session.commit()
        _invalidate_all_service_caches()
    except Exception:
        db.session.rollback()
        logging.getLogger(__name__).exception('services/migrate failed')
        flash('Conversion failed and nothing was changed. No rows were altered.', 'danger')
        return redirect(url_for('masters.services'))

    total = sum(u for _n, u in converted)
    names = ', '.join(n for n, _u in converted)
    flash(
        f'Converted {len(converted)} material(s) into services: {names}.',
        'success',
    )
    flash(
        f'{total} historical record(s) now resolve as services. No bill, entry, '
        'payment or balance was modified — they are name-keyed, so they simply '
        'resolve against the new master.',
        'info',
    )
    return redirect(url_for('masters.services'))



@bp.route('/services/merge', methods=['POST'])
@login_required
def merge_services():
    """Merge one service into another and cascade all historical entries/items."""
    source_id = (request.form.get('source_service_id') or '').strip()
    target_id = (request.form.get('target_service_id') or '').strip()
    if not source_id or not target_id or source_id == target_id:
        flash('Select two different services to merge.', 'danger')
        return redirect(url_for('masters.services'))

    source_svc = db.session.get(Service, int(source_id))
    target_svc = db.session.get(Service, int(target_id))
    if not source_svc or not target_svc:
        flash('One or both services were not found.', 'danger')
        return redirect(url_for('masters.services'))

    source_name = source_svc.name
    target_name = target_svc.name
    try:
        _cascade_rename_service_label(source_name, target_name)
        db.session.delete(source_svc)
        db.session.commit()
        _invalidate_all_service_caches()
        flash(f'Merged "{source_name}" into "{target_name}". All historical entries and bill lines updated.', 'success')
    except Exception:
        db.session.rollback()
        logging.getLogger(__name__).exception('merge_services failed')
        flash('Services could not be merged.', 'danger')
    return redirect(url_for('masters.services'))
