from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Intent = Literal["confirm", "date_change", "price_change", "cancel_balance", "split_delivery", "other"]
DateKind = Literal["arrival", "ship", "unknown"]


class ExtractedLine(BaseModel):
    po_number: str | None = Field(None, description="信中提到的採購單號，原樣照抄，例如 PUR-ORD-2026-00002")
    item_code: str | None = Field(None, description="信中提到的料號或供應商料號，原樣照抄")
    qty_text: str | None = Field(None, description="數量原文，例如 '1K'、'1,000 pcs'；未提及則為 null")
    unit_price_text: str | None = Field(None, description="單價原文；未提及則為 null")
    date_text: str | None = Field(None, description="日期原文，例如 '115/11/09'、'下週二'；不得自行換算")
    date_kind: DateKind = Field("unknown", description="arrival=到貨日, ship=出貨日, unknown=無法判斷")
    evidence: str = Field(..., description="支持此行擷取結果、逐字引用的原文片段")


class Extraction(BaseModel):
    intent: Intent
    lines: list[ExtractedLine] = Field(default_factory=list)
    confidence: float = Field(..., ge=0.0, le=1.0, description="擷取信心 0~1")
    notes: str | None = Field(None, description="無法分類或有疑慮時的簡短說明")

    @field_validator("lines")
    @classmethod
    def _cap(cls, v: list[ExtractedLine]) -> list[ExtractedLine]:
        if len(v) > 20:
            raise ValueError("單封信明細過多")
        return v


class NormalizedLine(BaseModel):
    index: int
    po_number: str | None
    item_code: str | None
    qty: float | None
    unit_price: float | None
    date_text: str | None
    date_kind: DateKind
    stated_date: date | None = None
    eta: date | None = None
    evidence: str = ""
    issues: list[str] = Field(default_factory=list)


class POLine(BaseModel):
    po: str
    line: str
    supplier: str
    item_code: str
    supplier_part_no: str | None = None
    qty: float
    received_qty: float = 0
    schedule_date: date
    rate: float | None = None
    po_modified: str = ""

    @property
    def remaining(self) -> float:
        return max(self.qty - self.received_qty, 0)


class Supplier(BaseModel):
    name: str
    supplier_name: str
    email: str | None = None
    transit_days: int | None = None


class Item(BaseModel):
    item_code: str
    item_name: str = ""
    inspection_category: str | None = None
    production_days: int | None = None
    test_pack_ship_days: int | None = None


class WorkOrder(BaseModel):
    name: str
    production_item: str
    qty: float
    planned_start_date: date | None
    sales_order: str | None
    required: dict[str, float]


class SalesOrder(BaseModel):
    name: str
    customer: str
    delivery_date: date | None


class ShortageTask(BaseModel):
    name: str
    supplier: str
    purchase_order: str
    po_item: str
    item_code: str
    qty: float
    required_date: date
    status: Literal["Open", "Closed", "Cancelled"] = "Open"
    assigned_to: str | None = None


class CaseStatus(str, Enum):
    PENDING = "Pending"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    EXPIRED = "Expired"
    APPLIED = "Applied"


class ReviewCase(BaseModel):
    name: str
    message_id: str
    supplier: str
    status: CaseStatus = CaseStatus.PENDING
    reasons: list[str] = Field(default_factory=list)
    proposed_changes: list[dict] = Field(default_factory=list)
    base_versions: dict[str, str] = Field(default_factory=dict)
    approver: str | None = None
    summary: str = ""


class LineMatch(BaseModel):
    index: int
    po_line: POLine | None = None
    shortage_task: ShortageTask | None = None
    inferred_po: bool = False
    issues: list[str] = Field(default_factory=list)


class LineDiff(BaseModel):
    index: int
    po: str
    line: str
    item_code: str
    original_date: date
    new_eta: date | None
    date_diff_days: int | None
    original_qty: float
    remaining_qty: float
    stated_qty: float | None
    qty_diff: float | None
    price_changed: bool | None
    shortage_task: str | None = None
    baseline_date: date | None = None
    cumulative_diff_days: int | None = None


class ImpactKind(str, Enum):
    NO_IMPACT = "no_impact"
    IMPACT_DETECTED = "impact_detected"
    UNKNOWN = "unknown"


class AffectedOrder(BaseModel):
    work_order: str
    sales_order: str | None
    customer_date: date | None
    planned_start: date
    projected_start: date | None
    projected_finish: date | None
    projected_ship: date | None
    delay_workdays: int | None
    short_items: list[str]


class ImpactResult(BaseModel):
    result: ImpactKind
    confidence: Literal["high", "low"]
    affected: list[AffectedOrder] = Field(default_factory=list)
    basis: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class RouteDecision(BaseModel):
    route: Literal["auto", "review", "manual", "stop"]
    reasons: list[str]
