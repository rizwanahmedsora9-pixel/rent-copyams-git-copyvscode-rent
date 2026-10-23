from .__base import *  # noqa
from .helpers import *  # noqa

class Material(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('material_category.id'), index=True, nullable=True)
    unit_price = db.Column(db.Float, default=0)
    total = db.Column(db.Float, default=0)
    unit = db.Column(db.String(20), default='Bags')
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)

    category = db.relationship('MaterialCategory', backref=db.backref('materials', lazy=True))


class Service(db.Model):
    """A charge that is billed like a material but is never stock.

    Rent (``RENT-CEMENT``, ``RENT-STEEL``, …) is the motivating case: it is
    charged to the client per unit on the same bill line, discounted, booked,
    consumed against a booking and settled by the same payment — but it is not
    goods. It has no stock, needs no GRN and must never appear in a stock
    report.

    Services resolve by **name**, exactly the way materials do
    (``BookingItem.material_name``, ``DirectSaleItem.product_name``,
    ``Entry.material`` are all free-text strings, not FKs). That is what lets
    historical rows keep working untouched after a material is moved here.

    ``unit_price`` exists only so existing readers of ``.unit_price`` (returns,
    dispatch, edit-sale) keep working; it is never used to pre-fill a line rate,
    because service rates are typed per line. ``total`` is a read-only 0 so a
    stray stock write fails loudly instead of silently accumulating a number no
    report should show.
    """

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    unit = db.Column(db.String(20), default='Pcs')
    unit_price = db.Column(db.Float, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)
    # Set when this service was converted from a Material master row, so the
    # move can be audited (and reasoned about) after the fact.
    converted_from_material_id = db.Column(db.Integer, nullable=True)

    @property
    def total(self):
        return 0

    @property
    def category(self):
        return None

    @property
    def category_id(self):
        return None


class MaterialCategory(db.Model):
    __tablename__ = 'material_category'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)

