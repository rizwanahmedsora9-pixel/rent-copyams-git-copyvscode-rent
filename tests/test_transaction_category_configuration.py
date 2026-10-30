import pytest
from models import db, CashFlowCategory, CashFlowSubcategory
from blueprints.accounts.transaction_tree import build_unified_category_catalog, validate_transaction_labels


def test_catalog_has_no_built_in_or_generated_labels(app):
    with app.app_context():
        CashFlowSubcategory.query.delete()
        CashFlowCategory.query.delete()
        db.session.commit()
        assert build_unified_category_catalog()['all_categories'] == []
        cat = CashFlowCategory(name='My custom category', direction='out', is_active=True)
        db.session.add(cat)
        db.session.flush()
        assert build_unified_category_catalog()['by_name'][cat.name]['subcategories'] == []
        sub = CashFlowSubcategory(category_id=cat.id, name='My custom child', is_active=True)
        db.session.add(sub)
        db.session.commit()
        validate_transaction_labels(cat.name, sub.name, 'out')
        with pytest.raises(ValueError):
            validate_transaction_labels(cat.name, sub.name, 'in')
        with pytest.raises(ValueError):
            validate_transaction_labels(cat.name, 'Invented child', 'out')
        sub.is_active = False
        db.session.commit()
        with pytest.raises(ValueError):
            validate_transaction_labels(cat.name, sub.name, 'out')
        cat.is_active = False
        db.session.commit()
        assert build_unified_category_catalog()['all_categories'] == []
