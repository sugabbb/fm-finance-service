"""Potential revenue of the monthly cut-off: the month-end egg stock and fish weight estimate,
valued at what the business actually sold them for.

The quantities are operational data. Finance does not read the operational database: it asks
operational-service for them through its private API, one read per dataset (contract:
``docs/operational-month-end-api.md``)::

    GET /api/v1/internal/month-end/{year}/{month}/egg-stock
    GET /api/v1/internal/month-end/{year}/{month}/fish-weight-estimate

Operational answers ``404`` until it has finalized the month. The cut-off then waits: no snapshot
is taken (:class:`MonthEndDataNotReadyError`) until both datasets are available.

Valuation (done here, in finance)
    ``value = weight_kg x price_per_kg``, rounded to the rupiah, per line (egg category or pond).
    ``price_per_kg`` is the weighted average price of the sales of the same product dated in the
    closed month: ``sum(weight_kg x price_per_kg_idr) / sum(weight_kg)`` over the sale lines of
    ``EGG_SALE`` (per egg category) or ``FISH_SALE`` revenues that are not draft or rejected.
    When the month has no such sale, the latest of the ``PRICE_LOOKBACK_MONTHS`` months before it
    that has one is used. Without any, the line is not valued (``0``) and a warning is recorded.

Potential revenue is a month-end **position**, not income: it is kept out of the cash figures and
the cash-basis P&L (which still reconcile with the cash movement exactly as before), and it is not
additive from one month to the next (stock closing one month is sold, and becomes revenue, in a
later one).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import Date, func, literal, or_, select
from sqlalchemy.orm import Session

from app.db.models import OperationalIntegrationOutbox, Revenue, RevenueEggDetail, RevenueFishDetail
from app.features.cash_statement_service import Period
from app.integrations.operational import OperationalNotFound, operational_client

ZERO = Decimal("0")
PRICE = Decimal("0.001")
MONEY = Decimal("1")

EGG_STOCK, FISH_STOCK = "EGG_STOCK", "FISH_STOCK"
ASSET_TYPES = (EGG_STOCK, FISH_STOCK)
EGG_CATEGORIES = ("UTUH", "PECAH", "REJECT")
FISH = "FISH"  # the price key of fish: fish sales have no grade
VALUATION = "AVERAGE_SALES_PRICE"

# A month without sales of a product is priced from the latest of this many months before it.
PRICE_LOOKBACK_MONTHS = 3
# Sales whose price counts: every sale except the ones still in draft or rejected.
PRICED_APPROVALS = ("NOT_REQUIRED", "SUBMITTED", "APPROVED")

EGG_STOCK_DATASET = "egg_stock"
FISH_ESTIMATE_DATASET = "fish_weight_estimate"


class MonthEndDataNotReadyError(RuntimeError):
    """Operational has not published (finalized) the month-end data of the month yet."""

    def __init__(self, period: Period, missing: Sequence[OperationalNotFound]) -> None:
        names = ", ".join(item.path for item in missing)
        super().__init__(f"operational month-end data of {period.label} is not available yet ({names})")
        self.period = period
        self.missing = list(missing)


class MonthEndDataInvalidError(RuntimeError):
    """Operational returned month-end data that breaks the contract: the cut-off must not use it."""


# --- Contract (operational -> finance) ----------------------------------------------------------
# Unknown fields are ignored so operational can add some without breaking finance; the fields
# finance uses are required and checked.

class _Contract(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class EggStockLine(_Contract):
    egg_category: Literal["UTUH", "PECAH", "REJECT"]
    weight_kg: Decimal = Field(ge=0, max_digits=14, decimal_places=3)
    quantity_pieces: int = Field(ge=0)


class FishPondEstimate(_Contract):
    pond_id: int
    pond_code: str = Field(min_length=1, max_length=60)
    pond_name: str = Field(min_length=1, max_length=255)
    estimated_weight_kg: Decimal = Field(ge=0, max_digits=14, decimal_places=3)
    estimated_fish_count: int | None = Field(default=None, ge=0)


class _MonthEndData(_Contract):
    year: int
    month: int = Field(ge=1, le=12)
    revision: int = Field(ge=1)
    finalized_at: AwareDatetime

    def check_period(self, period: Period) -> None:
        if (self.year, self.month) != (period.start.year, period.start.month):
            raise ValueError(f"data of {self.year}-{self.month:02d} returned for {period.label}")

    @property
    def source(self) -> dict[str, object]:
        return {"revision": self.revision, "finalized_at": self.finalized_at.isoformat()}


class EggStock(_MonthEndData):
    lines: list[EggStockLine]

    @model_validator(mode="after")
    def one_line_per_category(self) -> EggStock:
        categories = [line.egg_category for line in self.lines]
        if len(set(categories)) != len(categories):
            raise ValueError("an egg category is listed more than once")
        return self


class FishWeightEstimate(_MonthEndData):
    ponds: list[FishPondEstimate]

    @model_validator(mode="after")
    def one_line_per_pond(self) -> FishWeightEstimate:
        codes = [pond.pond_code for pond in self.ponds]
        if len(set(codes)) != len(codes):
            raise ValueError("a pond is listed more than once")
        return self


@dataclass(frozen=True)
class MonthEndAssets:
    eggs: EggStock
    fish: FishWeightEstimate


def _parse[T: _MonthEndData](model: type[T], dataset: str, data: dict[str, Any], period: Period) -> T:
    try:
        parsed = model.model_validate(data)
        parsed.check_period(period)
    except (ValidationError, ValueError) as exc:
        raise MonthEndDataInvalidError(f"operational {dataset} of {period.label} is invalid: {exc}") from exc
    return parsed


def fetch_month_end_assets(period: Period) -> MonthEndAssets:
    """Both month-end datasets of ``period`` from operational-service.

    Raises :class:`MonthEndDataNotReadyError` when either is not available yet (``404``; both are
    asked, so the error names everything still missing), :class:`MonthEndDataInvalidError` when a
    response breaks the contract, and ``AppError`` (502/503) when operational fails."""
    year, month = period.start.year, period.start.month
    readers: list[tuple[str, Callable[[int, int], dict[str, Any]]]] = [
        (EGG_STOCK_DATASET, operational_client.month_end_egg_stock),
        (FISH_ESTIMATE_DATASET, operational_client.month_end_fish_weight_estimate),
    ]
    found: dict[str, dict[str, Any]] = {}
    missing: list[OperationalNotFound] = []
    for dataset, read in readers:
        try:
            found[dataset] = read(year, month)
        except OperationalNotFound as not_found:
            missing.append(not_found)
    if missing:
        raise MonthEndDataNotReadyError(period, missing)
    return MonthEndAssets(
        eggs=_parse(EggStock, EGG_STOCK_DATASET, found[EGG_STOCK_DATASET], period),
        fish=_parse(FishWeightEstimate, FISH_ESTIMATE_DATASET, found[FISH_ESTIMATE_DATASET], period),
    )


# --- Valuation ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SalesPrice:
    """The average price per kg of a product's sales in ``month``."""

    month: Period
    price_per_kg: Decimal
    sales: int  # sale lines averaged
    weight_kg: Decimal


@dataclass(frozen=True)
class AssetLine:
    asset_type: str
    item_code: str  # egg category or pond code
    item_name: str
    source_id: int | None  # operational pond id (None for eggs)
    quantity_pieces: int | None  # eggs in stock / estimated fish count
    weight_kg: Decimal
    price: SalesPrice | None  # None: no sale to price it from

    @property
    def value(self) -> Decimal:
        if self.price is None:
            return ZERO
        return (self.weight_kg * self.price.price_per_kg).quantize(MONEY, rounding=ROUND_HALF_UP)


@dataclass
class PotentialRevenue:
    lines: list[AssetLine]
    sources: dict[str, dict[str, object]]  # operational revision/finalized_at per dataset
    warnings: list[str] = field(default_factory=list)

    def total(self, asset_type: str | None = None) -> Decimal:
        return sum(
            (line.value for line in self.lines if asset_type is None or line.asset_type == asset_type), ZERO
        )


def _months_back(period: Period, count: int) -> Period:
    start = period.start
    index = start.year * 12 + start.month - 1 - count
    return Period(date(index // 12, index % 12 + 1, 1))


def sales_prices(db: Session, period: Period) -> dict[str, SalesPrice]:
    """The price per kg of each product (egg category, ``FISH``): its sales of ``period`` or, if
    none, of the latest of the ``PRICE_LOOKBACK_MONTHS`` months before."""
    first = _months_back(period, PRICE_LOOKBACK_MONTHS)
    month = func.date_trunc("month", Revenue.transaction_date).cast(Date).label("month")
    priced = (
        Revenue.approval_status.in_(PRICED_APPROVALS),
        Revenue.transaction_date.between(first.start, period.end),
    )
    queries = [
        select(
            RevenueEggDetail.egg_category,
            month,
            func.count(),
            func.sum(RevenueEggDetail.weight_kg),
            func.sum(RevenueEggDetail.weight_kg * RevenueEggDetail.price_per_kg_idr),
        )
        .join(Revenue, Revenue.id == RevenueEggDetail.revenue_id)
        .where(Revenue.revenue_type == "EGG_SALE", RevenueEggDetail.weight_kg > 0, *priced)
        .group_by(RevenueEggDetail.egg_category, month),
        select(
            literal(FISH),
            month,
            func.count(),
            func.sum(RevenueFishDetail.weight_kg),
            func.sum(RevenueFishDetail.weight_kg * RevenueFishDetail.price_per_kg_idr),
        )
        .join(Revenue, Revenue.id == RevenueFishDetail.revenue_id)
        .where(Revenue.revenue_type == "FISH_SALE", RevenueFishDetail.weight_kg > 0, *priced)
        .group_by(month),
    ]
    prices: dict[str, SalesPrice] = {}
    for query in queries:
        for product, month_start, sales, weight, amount in db.execute(query.order_by(month.desc())).all():
            if product in prices:
                continue  # a later month already priced it
            weight = Decimal(weight)
            prices[product] = SalesPrice(
                month=Period(month_start),
                price_per_kg=(Decimal(amount) / weight).quantize(PRICE, rounding=ROUND_HALF_UP),
                sales=int(sales),
                weight_kg=weight,
            )
    return prices


def undelivered_egg_sales(db: Session, period: Period, finalized_at: datetime) -> int:
    """Egg sales dated up to the month's end whose latest inventory snapshot had not reached
    operational when it finalized the egg stock (still in the outbox, or delivered later): the
    stock may not reflect them, i.e. be overstated (or understated for a voided sale)."""
    outbox = OperationalIntegrationOutbox
    return int(
        db.scalar(
            select(func.count(func.distinct(outbox.aggregate_id))).where(
                outbox.aggregate_type == "EGG_REVENUE",
                outbox.status != "SUPERSEDED",
                outbox.payload["transaction_date"].astext.cast(Date) <= period.end,
                or_(
                    outbox.status != "DELIVERED",
                    outbox.processed_at > finalized_at,
                ),
            )
        )
        or 0
    )


def value_assets(db: Session, period: Period, assets: MonthEndAssets) -> PotentialRevenue:
    """The potential revenue of ``period`` (read only): each month-end asset line valued at its
    sales price, with a warning for every line that weighs something but could not be priced."""
    prices = sales_prices(db, period)
    lines = [
        AssetLine(
            asset_type=EGG_STOCK,
            item_code=line.egg_category,
            item_name=line.egg_category,
            source_id=None,
            quantity_pieces=line.quantity_pieces,
            weight_kg=line.weight_kg,
            price=prices.get(line.egg_category),
        )
        for line in sorted(assets.eggs.lines, key=lambda item: EGG_CATEGORIES.index(item.egg_category))
    ]
    lines += [
        AssetLine(
            asset_type=FISH_STOCK,
            item_code=pond.pond_code,
            item_name=pond.pond_name,
            source_id=pond.pond_id,
            quantity_pieces=pond.estimated_fish_count,
            weight_kg=pond.estimated_weight_kg,
            price=prices.get(FISH),
        )
        for pond in sorted(assets.fish.ponds, key=lambda item: item.pond_code)
    ]
    result = PotentialRevenue(
        lines=lines,
        sources={EGG_STOCK_DATASET: assets.eggs.source, FISH_ESTIMATE_DATASET: assets.fish.source},
    )
    first = _months_back(period, PRICE_LOOKBACK_MONTHS)
    unpriced: dict[str, Decimal] = {}
    for line in lines:
        if line.price is None and line.weight_kg > 0:
            product = "fish" if line.asset_type == FISH_STOCK else f"{line.item_code} egg"
            unpriced[product] = unpriced.get(product, ZERO) + line.weight_kg
    for product, weight in unpriced.items():
        result.warnings.append(
            f"no {product} sale from {first.label} to {period.label} to price it: "
            f"{weight} kg of month-end stock is not valued"
        )
    late = undelivered_egg_sales(db, period, assets.eggs.finalized_at)
    if late:
        result.warnings.append(
            f"{late} egg sale(s) dated up to {period.end.isoformat()} had not reached operational-service "
            "when it finalized the egg stock: the stock (and its potential revenue) may not reflect them"
        )
    return result
