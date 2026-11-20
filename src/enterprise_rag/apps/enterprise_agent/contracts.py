"""Request scope, validated source evidence, and the report contract."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from enterprise_rag.core.citations import Evidence

Day = Literal["2026-06-01", "2026-06-02"]
Region = Literal["华东", "华南"]
SKU = Literal["A100", "B200", "C300"]
TaskKind = Literal["overview", "sales", "inventory", "policy"]
Role = Literal["analyst", "viewer"]
SNAPSHOT = "qh-2026-06-v1"


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class sales_summary(ToolInput):
    """Look up daily sales totals for the exact date and region in the task scope."""

    day: Day
    region: Region


class inventory_status(ToolInput):
    """Look up inventory for the exact SKU in the task scope."""

    sku: SKU


class warehouse_policy(ToolInput):
    """Find the approved warehouse policy for registering inventory discrepancies."""

    topic: Literal["discrepancy"]


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: TaskKind
    question: str = Field(min_length=1, max_length=1400)
    day: Day = "2026-06-01"
    region: Region = "华东"
    sku: SKU = "A100"


class SalesData(ToolInput):
    order_count: int = Field(ge=0)
    units: int = Field(ge=0)
    revenue_cents: int = Field(ge=0)


class InventoryData(ToolInput):
    sku: SKU
    snapshot: Literal["qh-2026-06-v1"]
    on_hand: int = Field(ge=0, le=100000)
    reserved: int = Field(ge=0, le=100000)
    safety_stock: int = Field(ge=0, le=100000)


class SQLEvidence(BaseModel):
    source_type: Literal["sql"] = "sql"
    evidence_id: str
    snapshot: str = SNAPSHOT
    query_id: Literal["daily_sales_v1"] = "daily_sales_v1"
    parameters: sales_summary
    data: SalesData
    content_hash: str


class APIEvidence(BaseModel):
    source_type: Literal["api"] = "api"
    evidence_id: str
    snapshot: str = SNAPSHOT
    endpoint: str
    data: InventoryData
    content_hash: str


class PolicyEvidence(BaseModel):
    source_type: Literal["document"] = "document"
    evidence_id: str
    document: Evidence


BusinessEvidence = Annotated[SQLEvidence | APIEvidence | PolicyEvidence, Field(discriminator="source_type")]


class ToolAudit(BaseModel):
    call_id: str
    tool: str
    status: Literal["ok", "rejected", "failed"]
    code: str
    elapsed_ms: float
    evidence_id: str | None = None


class ReportSection(BaseModel):
    title: str
    text: str
    evidence_id: str


class AgentReport(BaseModel):
    task_id: UUID
    status: Literal["completed", "partial", "incomplete", "denied"]
    stop_reason: str
    sections: list[ReportSection]
    evidence: list[BusinessEvidence]
    missing_tools: list[str]
    audit: list[ToolAudit]
    model_calls: int
    dropped_calls: int
    elapsed_ms: float
    input_tokens: int | None
    output_tokens: int | None
