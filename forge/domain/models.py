"""领域模型：把"项目全生命周期横跨多套系统"的真实形状写成可校验的对象。

设计原则（每条都有现实理由，不是风格偏好）：
1. **金额一律整数分（cents）**，阈值比率用基点（bps）。浮点存钱会让"同一 seed 两次生成逐字节一致"
   这件事变成运气而不是保证。
2. **不提供"实际工期"字段**。真实数据里实际开工日 == 实际完工日（完工时回填），
   任何基于实际工期的指标都无意义，所以模型层直接不给这个字段，⛔ 让下游算出假的。
3. **"无数据"与 0 是两种状态**：可缺失金额用 ``Optional``，⛔ 不许拿 0 代替"没录"。
4. **故意保留稀疏性**（见 ``forge.synth.generate``）：payment_plan 只有少数节点带计划金额、
   ECN 成本大量为 0、交付物极少交付、权限配置远少于用户数。
"""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrEnum(str, Enum):
    """稳定的字符串枚举：值即序列化结果，⛔ 依赖 Enum 的 repr。"""

    def __str__(self) -> str:  # pragma: no cover - 便于 f-string
        return self.value


class ProjectStatus(StrEnum):
    DRAFT = "draft"
    PREPARING = "preparing"
    ACTIVE = "active"
    PAUSED = "paused"
    DELIVERED = "delivered"
    CLOSED = "closed"


class TaskKind(StrEnum):
    """一张任务表同时承载四类对象，靠下面的谓词区分，⛔ 拆成四张表（真实系统就是这么存的）。"""

    PHASE = "phase"
    OPERATION = "operation"
    MILESTONE = "milestone"
    TASK = "task"


class TaskState(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class BudgetSubject(StrEnum):
    MATERIAL = "material"
    LABOR = "labor"
    MANUFACTURING = "manufacturing"
    MISC = "misc"


class CostDocKind(StrEnum):
    """六类成本单据。⚠️ shipment（出库）只展示、不参与成本加总——真实口径如此。"""

    PURCHASE = "purchase"
    SHIPMENT = "shipment"
    RETURN = "return"
    FORM_CONVERSION = "form_conversion"
    EXPENSE = "expense"
    LABOR_REPORT = "labor_report"


class PaymentStage(StrEnum):
    PREPAYMENT = "prepayment"
    ARRIVAL = "arrival"
    ACCEPTANCE = "acceptance"
    WARRANTY = "warranty"


class PaymentStatus(StrEnum):
    UNPAID = "unpaid"
    PAID = "paid"
    OVERDUE_PAID = "overdue_paid"
    OVERDUE_UNPAID = "overdue_unpaid"


class IssueStatus(StrEnum):
    RECEIVED = "received"
    PENDING_AUTH = "pending_auth"
    AUTHENTICATED = "authenticated"
    TRACKING = "tracking"
    PENDING_CLOSE = "pending_close"
    CLOSED = "closed"


class DeliverableKind(StrEnum):
    DESIGN = "design"
    PURCHASE = "purchase"
    ASSEMBLY = "assembly"
    COMMISSIONING = "commissioning"
    ACCEPTANCE = "acceptance"
    ARCHIVE = "archive"


class ScopeTier(StrEnum):
    """四档可见范围。⚠️ 顺序是 company > bu > subline > member —— subline 在 bu 之下，
    写反会让子产品线管理层看到整个 BU。"""

    COMPANY = "company"
    BU = "bu"
    SUBLINE = "subline"
    MEMBER = "member"


TIER_WEIGHT = {
    ScopeTier.COMPANY: 3,
    ScopeTier.BU: 2,
    ScopeTier.SUBLINE: 1,
    ScopeTier.MEMBER: 0,
}


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Project(_Base):
    code: str
    name: str
    customer: str
    business_unit: Optional[str] = None  # 稀疏：允许"未填"，⛔ 用空串伪装成有值
    category: str = "delivery"
    status: ProjectStatus = ProjectStatus.PREPARING
    plan_start: date
    plan_end: date
    # ⚠️ 没有 actual_start / 没有"实际工期"。只保留 actual_end，且大量为 None。
    actual_end: Optional[date] = None

    @model_validator(mode="after")
    def _plan_window(self) -> "Project":
        if self.plan_end < self.plan_start:
            raise ValueError("plan_end earlier than plan_start")
        return self


class Task(_Base):
    task_id: str
    project_code: str
    name: str
    plan_type: TaskKind
    state: TaskState
    wbs_level: int = 1
    parent_id: Optional[str] = None
    is_milestone: bool = False
    owner: Optional[str] = None
    plan_start: date
    plan_end: date
    actual_end: Optional[date] = None  # 真实数据里大量为空
    estimated_hours: int = 0  # 整数小时

    @model_validator(mode="after")
    def _shape(self) -> "Task":
        if self.plan_end < self.plan_start:
            raise ValueError("plan_end earlier than plan_start")
        if self.plan_type is TaskKind.MILESTONE and not self.is_milestone:
            raise ValueError("milestone task must set is_milestone")
        return self


class TaskDep(_Base):
    predecessor_id: str
    successor_id: str
    kind: str = "finish_to_start"  # finish_to_start | parallel | start_to_start
    source: str = "inferred"  # ⚠️ doc | inferred —— 依赖边必须能说清从哪来


class BudgetVersion(_Base):
    project_code: str
    version: int
    is_current: bool = False
    approved_on: Optional[date] = None


class BudgetLine(_Base):
    project_code: str
    version: int
    subject: BudgetSubject
    budget_cents: int
    incurred_cents: int
    warning_ratio_bps: int = 7000  # 照系统配置逐科目取，缺省 0.70；⛔ 拍一个全局 80%

    @model_validator(mode="after")
    def _nonneg(self) -> "BudgetLine":
        if self.budget_cents < 0 or self.incurred_cents < 0:
            raise ValueError("money must be non-negative cents")
        if not 0 < self.warning_ratio_bps <= 20000:
            raise ValueError("warning_ratio_bps out of range")
        return self


class CostDoc(_Base):
    doc_no: str
    project_code: str
    kind: CostDocKind
    amount_cents: int
    occurred_on: date


class PaymentNode(_Base):
    """回款节点。⚠️ plan_amount_cents 大量为 None（真实稀疏性），
    所以"合同金额"一律以计划表为主源，⛔ 把 None 当 0 求和。"""

    node_id: str
    project_code: str
    stage: PaymentStage
    due_on: date
    status: PaymentStatus = PaymentStatus.UNPAID
    plan_amount_cents: Optional[int] = None
    actual_amount_cents: Optional[int] = None
    paid_on: Optional[date] = None


class Contract(_Base):
    """合同与项目的关联极稀疏（真实数据里几乎没填），
    因此 ⛔ 通过合同表反查项目金额。"""

    contract_no: str
    customer: str
    amount_cents: int
    project_code: Optional[str] = None


class EcnChange(_Base):
    ecn_id: str
    project_code: str
    reason: str
    method: str = "rework"
    responsible_dept: str = "design"
    cost_cents: int = 0  # 90% 为 0 —— 真实录入状况，⛔ 为了好看填满
    raised_on: date


class Issue(_Base):
    issue_id: str
    project_code: str
    status: IssueStatus
    impact_level: str = "yellow"  # blue | yellow | orange | red
    delay_days: int = 0
    amount_cents: int = 0
    pushed_from: str = "issue_system"  # 问题管理系统推送，PMS 侧只做接收/鉴权/关闭校验


class Deliverable(_Base):
    item_id: str
    project_code: str
    kind: DeliverableKind
    required: bool = True
    delivered: bool = False  # 每项目 10 项里通常只有 0–3 项已交付


class AppUser(_Base):
    user_key: str
    display_name: str
    role_keys: list[str] = Field(default_factory=list)


class PermissionProfile(_Base):
    """只有极少数用户有显式配置 ⇒ 默认放行策略必须显式定义，⛔ 靠"查不到就全放行"。"""

    user_key: str
    tier: ScopeTier
    business_unit: Optional[str] = None  # 收窄值
    project_type: Optional[str] = None  # 收窄值


class Dataset(_Base):
    """一次生成的全量数据，用于测试与下游加载。"""

    projects: list[Project]
    tasks: list[Task]
    task_deps: list[TaskDep]
    budget_versions: list[BudgetVersion]
    budget_lines: list[BudgetLine]
    cost_docs: list[CostDoc]
    payment_nodes: list[PaymentNode]
    contracts: list[Contract]
    ecns: list[EcnChange]
    issues: list[Issue]
    deliverables: list[Deliverable]
    users: list[AppUser]
    permissions: list[PermissionProfile]
    seed: int
    as_of: date
