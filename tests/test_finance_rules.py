from decimal import Decimal

import pytest

from app.core.errors import AppError
from app.features.finance_schemas import FeedPurchaseLine
from app.features.finance_service import payment_amount


def test_payment_amount_contract() -> None:
    assert payment_amount("FULL", None, Decimal("100")) == Decimal("100")
    assert payment_amount("PARTIAL", Decimal("40"), Decimal("100")) == Decimal("40")
    with pytest.raises(AppError):
        payment_amount("PARTIAL", Decimal("100"), Decimal("100"))


def test_sack_shape() -> None:
    line = FeedPurchaseLine(
        feed_type_id=1,
        purchase_unit="SAK",
        purchase_quantity="2",
        kg_per_sak="50",
        price_per_purchase_unit_idr="300000",
    )
    assert line.purchase_quantity == Decimal("2")
    with pytest.raises(ValueError):
        FeedPurchaseLine(
            feed_type_id=1,
            purchase_unit="SAK",
            purchase_quantity="1.5",
            kg_per_sak="50",
            price_per_purchase_unit_idr="300000",
        )
