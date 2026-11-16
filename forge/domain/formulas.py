"""确定性指标公式。

三条不可妥协的规矩（都是真实项目里付过代价换来的）：
1. **任何日期敏感计算必须显式传入 ``as_of``**，⛔ 读系统时钟。
   否则"改动前后跑同一批语料做差集"必然出现假差异——差额恰好等于没有实际结束日的那批行。
2. **无数据返回 ``None``，⛔ 返回 0**。业务方会把 0 读成"确实没花钱"。
3. **⛔ 让模型算数**：这些函数是 LLM 唯一 permitted 的数字来源，产出带
   ``numerator/denominator/as_of``，任何答案里的数字都能反查回这里。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from forge.domain.models import BudgetLine, CostDocKind, PaymentNode, PaymentStatus, Task, TaskKind, TaskState


@dataclass(frozen=True)
class Ratio:
    """派生量必须可反查：分子、分母、口径基准日。"""

    numerator: int
    denominator: int
    as_of: date

    def __post_init__(self) -> None:
        if self.denominator == 0:
            raise ValueError("denominator must not be zero; return None instead")

    @property
    def value(self) -> float:
        return self.numerator / self.denominator

    def percent(self, digits: int = 1) -> str:
        return f"{round(self.value * 100, digits)}%"


def expected_to_finish(tasks: Iterable[Task], as_of: date) -> list[Task]:
    """计划上到 as_of 应当已交付的任务。逾期率的分母只看这一批。"""
    return [t for t in tasks if t.plan_end <= as_of]


def completion_rate(tasks: Iterable[Task], as_of: date, *, weighted_by_hours: bool = False) -> Optional[Ratio]:
    tasks = list(tasks)
    if not tasks:
        return None
    done = [t for t in tasks if t.state is TaskState.DONE]
    if weighted_by_hours:
        total_h = sum(t.estimated_hours for t in tasks)
        if total_h == 0:
            return None
        done_h = sum(t.estimated_hours for t in done)
        return Ratio(done_h, total_h, as_of)
    return Ratio(len(done), len(tasks), as_of)


def overdue_rate(tasks: Iterable[Task], as_of: date) -> Optional[Ratio]:
    """逾期率 = 逾期任务数 ÷ **应完成**任务数。

    ⚠️ 分母是"应完成"（plan_end <= as_of），⛔ 全部任务数——后者会把还没到期的
    任务算进分母，系统性低估逾期。
    """
    expected = expected_to_finish(tasks, as_of)
    if not expected:
        return None
    late = [t for t in expected if t.state is not TaskState.DONE]
    return Ratio(len(late), len(expected), as_of)


def deviation_days(task: Task, as_of: date) -> int:
    """偏差天数 = 实际结束日（缺失则用 as_of）− 计划结束日。

    ⚠️ 真实数据里"实际开工日 == 实际完工日"，所以**没有"实际工期/工期偏差率"这两个口径**，
    只有这个可算的替代口径。
    """
    end = task.actual_end if task.actual_end is not None else as_of
    return (end - task.plan_end).days


def elapsed_plan_ratio(task: Task, as_of: date) -> Optional[Ratio]:
    """已过计划工期占比。⛔ 已超期不夹紧到 1.0——夹紧会让"延期 3 个月"和"刚好到期"
    变成同一个数，业务上完全两件事。"""
    span = (task.plan_end - task.plan_start).days
    if span <= 0:
        return None
    elapsed = (as_of - task.plan_start).days
    if elapsed < 0:
        return Ratio(0, span, as_of)
    return Ratio(elapsed, span, as_of)


def budget_execution_rate(line: BudgetLine, as_of: date) -> Optional[Ratio]:
    """预算执行率 = 已发生 ÷ 该科目预算。预算为 0 时无数据，⛔ 返回 0/除零。"""
    if line.budget_cents == 0:
        return None
    return Ratio(line.incurred_cents, line.budget_cents, as_of)


def cpi_cents(budget_total_cents: int, incurred_total_cents: int, as_of: date) -> Optional[Ratio]:
    """CPI —— ⚠️ **预算对比口径**（已发生 ÷ 预算总额），不是挣值管理里的 CPI。

    真正的 PV/EV/AC 三件套是另一个能力，未实现，⛔ 在这里冒充。
    """
    if budget_total_cents == 0:
        return None
    return Ratio(incurred_total_cents, budget_total_cents, as_of)


def eac_cents(budget_total_cents: int, incurred_total_cents: int) -> Optional[int]:
    """按当前执行率外推的完工成本估算。

    ⚠️ 已发生为 0 时必须返回 None：分母是 0，任何外推都是假数。
    真实项目里"AC=0 仍算出 EAC"是最容易被业务方当场推翻的一类错。
    """
    if incurred_total_cents == 0 or budget_total_cents == 0:
        return None
    return round(budget_total_cents * budget_total_cents / incurred_total_cents)


def actual_cost_cents(purchase: int, labor_internal: int, labor_external: int, expense: int, returns: int) -> int:
    """实际总成本 = 采购（−退货）+ 内部人工 + 外部人工 + 费用。

    ⚠️ 出库物料成本仅展示、不参与加总；形态转换也不计入。真实口径如此，⛔ 图省事全加起来。
    """
    return (purchase - returns) + labor_internal + labor_external + expense


def aging_days(node: PaymentNode, as_of: date) -> Optional[int]:
    """账龄：未到账 = as_of − 约定回款日；已到账 = 到账日 − 约定日（可为负，表示提前）。"""
    if node.paid_on is not None:
        return (node.paid_on - node.due_on).days
    if node.status in (PaymentStatus.UNPAID, PaymentStatus.OVERDUE_UNPAID):
        return (as_of - node.due_on).days
    return None


def planned_amount_total(nodes: Iterable[PaymentNode]) -> Optional[int]:
    """计划金额合计。⛔ 若所有节点都没有计划金额则返回 0——那会伪装成"计划回款 0 元"。

    真实数据里多数节点不带计划金额，所以金额必须走计划表为主源、缺数如实说明。
    """
    vals = [n.plan_amount_cents for n in nodes if n.plan_amount_cents is not None]
    if not vals:
        return None
    return sum(vals)


def payment_completion_rate(paid_cents: int, contract_amount_cents: int, as_of: date) -> Optional[Ratio]:
    if contract_amount_cents == 0:
        return None
    return Ratio(paid_cents, contract_amount_cents, as_of)


def is_overdue(task: Task, as_of: date) -> bool:
    return task.state is not TaskState.DONE and task.plan_end < as_of


def milestone_deviations(tasks: Iterable[Task], as_of: date) -> list[tuple[str, int]]:
    ms = [t for t in tasks if t.plan_type is TaskKind.MILESTONE]
    return sorted(((t.task_id, deviation_days(t, as_of)) for t in ms), key=lambda x: x[0])
