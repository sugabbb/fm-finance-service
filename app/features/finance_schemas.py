from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.core.pagination import StrictModel


class FeedPurchaseLine(StrictModel):
    id: int | None = None
    feed_type_id: int
    purchase_unit: Literal["KG", "SAK"] = "KG"
    purchase_quantity: Decimal = Field(ge=0)
    kg_per_sak: Decimal | None = None
    price_per_purchase_unit_idr: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def sack_shape(self) -> FeedPurchaseLine:
        if self.purchase_unit == "SAK":
            if self.purchase_quantity <= 0 or self.purchase_quantity != self.purchase_quantity.to_integral_value():
                raise ValueError("SAK purchase quantity must be a positive integer")
            if self.kg_per_sak is None or self.kg_per_sak <= 0:
                raise ValueError("kg_per_sak must be positive for SAK purchases")
        elif self.kg_per_sak is not None:
            raise ValueError("kg_per_sak is valid only for SAK purchases")
        return self


class FeedCashflow(StrictModel):
    cashflow_type: Literal["FEED_AYAM", "FEED_LELE"]
    transaction_date: date
    details: list[FeedPurchaseLine] = Field(min_length=1, max_length=30)


class SalaryDetails(StrictModel):
    employee_id: int
    salary_period: str = Field(min_length=1, max_length=40)


class SalaryCashflow(StrictModel):
    cashflow_type: Literal["SALARY"]
    transaction_date: date
    amount_idr: Decimal = Field(ge=0)
    details: SalaryDetails


class DescriptionDetails(StrictModel):
    description: str = Field(min_length=1, max_length=3000)


class UtilityCashflow(StrictModel):
    cashflow_type: Literal["UTILITY", "OTHER"]
    transaction_date: date
    amount_idr: Decimal = Field(ge=0)
    details: DescriptionDetails


class HealthDetails(DescriptionDetails):
    species: Literal["AYAM", "LELE"]


class HealthCashflow(StrictModel):
    cashflow_type: Literal["HEALTH"]
    transaction_date: date
    amount_idr: Decimal = Field(ge=0)
    details: HealthDetails


class NotesDetails(StrictModel):
    notes: str | None = Field(default=None, max_length=3000)


class LivestockPurchaseCashflow(StrictModel):
    cashflow_type: Literal["LELE_SEED_PURCHASE", "PULLET_PURCHASE", "BANK_ADMIN_FEE", "MAIN_ACCOUNT_TOPUP"]
    transaction_date: date
    amount_idr: Decimal = Field(ge=0)
    details: NotesDetails


OperationalInput = Annotated[
    FeedCashflow | SalaryCashflow | UtilityCashflow | HealthCashflow | LivestockPurchaseCashflow,
    Field(discriminator="cashflow_type"),
]


class OperationalCorrection(StrictModel):
    transaction: OperationalInput
    correction_reason: str = Field(min_length=1, max_length=3000)


class CustomerInline(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    phone: str = Field(min_length=1, max_length=50)
    customer_type: Literal["WARUNG", "AGEN"]
    notes: str | None = Field(default=None, max_length=3000)


class EggSaleLine(StrictModel):
    id: int | None = None
    egg_category: Literal["UTUH", "PECAH", "REJECT"]
    weight_kg: Decimal = Field(ge=0)
    quantity_pieces: int = Field(ge=0)
    price_per_kg_idr: Decimal = Field(ge=0)
    notes: str | None = Field(default=None, max_length=1000)


class SaleBase(StrictModel):
    transaction_date: date
    payment_method: Literal["CASH", "DIRECT_TRANSFER"]
    direct_transfer_date: date | None = None
    customer_id: int | None = None
    new_customer: CustomerInline | None = None

    @model_validator(mode="after")
    def sale_shape(self) -> SaleBase:
        if (self.customer_id is None) == (self.new_customer is None):
            raise ValueError("choose exactly one existing or new customer")
        if self.payment_method == "DIRECT_TRANSFER" and self.direct_transfer_date is None:
            raise ValueError("direct_transfer_date is required")
        if self.payment_method == "CASH" and self.direct_transfer_date is not None:
            raise ValueError("direct_transfer_date is valid only for DIRECT_TRANSFER")
        return self


class EggSale(SaleBase):
    revenue_type: Literal["EGG_SALE"]
    details: list[EggSaleLine] = Field(min_length=1, max_length=3)


class FishDetails(StrictModel):
    weight_kg: Decimal = Field(ge=0)
    price_per_kg_idr: Decimal = Field(ge=0)
    notes: str | None = Field(default=None, max_length=3000)


class FishSale(SaleBase):
    revenue_type: Literal["FISH_SALE"]
    details: FishDetails


class OtherRevenue(StrictModel):
    revenue_type: Literal["OTHER"]
    transaction_date: date
    amount_idr: Decimal = Field(ge=0)
    payment_method: Literal["CASH", "DIRECT_TRANSFER"]
    direct_transfer_date: date | None = None
    details: NotesDetails

    @model_validator(mode="after")
    def payment_shape(self) -> OtherRevenue:
        if self.payment_method == "DIRECT_TRANSFER" and self.direct_transfer_date is None:
            raise ValueError("direct_transfer_date is required")
        if self.payment_method == "CASH" and self.direct_transfer_date is not None:
            raise ValueError("direct_transfer_date is valid only for DIRECT_TRANSFER")
        return self


class TransferDetails(StrictModel):
    gross_amount_idr: Decimal = Field(ge=0)
    payment_cost_idr: Decimal = Field(default=Decimal("0"), ge=0)
    notes: str | None = Field(default=None, max_length=3000)

    @model_validator(mode="after")
    def costs(self) -> TransferDetails:
        if self.payment_cost_idr > self.gross_amount_idr:
            raise ValueError("payment cost cannot exceed gross amount")
        return self


class MainAccountTransfer(StrictModel):
    revenue_type: Literal["MAIN_ACCOUNT_TRANSFER"]
    transaction_date: date
    details: TransferDetails


RevenueInput = Annotated[
    EggSale | FishSale | OtherRevenue | MainAccountTransfer,
    Field(discriminator="revenue_type"),
]


class RevenueCorrection(StrictModel):
    transaction: RevenueInput
    correction_reason: str = Field(min_length=1, max_length=3000)
    acknowledge_negative_balance: bool = False


class Decision(StrictModel):
    decision: Literal["APPROVED", "REJECTED"]
    reason: str | None = Field(default=None, max_length=3000)
    acknowledge_negative_balance: bool = False

    @model_validator(mode="after")
    def reason_required(self) -> Decision:
        if self.decision == "REJECTED" and not self.reason:
            raise ValueError("reason is required for rejection")
        return self


class PaymentInput(StrictModel):
    payment_mode: Literal["PARTIAL", "FULL"] = "FULL"
    paid_date: date
    amount_idr: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def amount_shape(self) -> PaymentInput:
        if self.payment_mode == "PARTIAL" and (self.amount_idr is None or self.amount_idr <= 0):
            raise ValueError("positive amount_idr is required for partial payment")
        if self.payment_mode == "FULL" and self.amount_idr is not None:
            raise ValueError("amount_idr is prohibited for full payment")
        return self


class VoidPayment(StrictModel):
    reason: str = Field(min_length=1, max_length=3000)


class ReceiveFeed(StrictModel):
    received_date: date
