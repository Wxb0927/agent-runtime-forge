"""公式语义测试。

⚠️ 带「能失败点」标记的用例，是 ``tools/mutation_check.py`` 的靶子：
把对应实现改错后，这些用例必须转红。测试通不过不是目的，**测不出错才是事故**。
"""
from __future__ import annotations

from datetime import date

import pytest

from forge.domain import formulas as F
from forge.domain.models import (
    BudgetLine,
    BudgetSubject,
    PaymentNode,
    PaymentStage,
    PaymentStatus,
    Project,
    Task,
    TaskKind,
    TaskState,
)

AS_OF = date(2026, 6, 30)


def mk_task(tid: str, plan_start: date, plan_end: date, state: TaskState,
            actual_end: date | None = None, hours: int = 8) -> Task:
    return Task(task_id=tid, project_code="PRJ-1000", name=tid, plan_type=TaskKind.TASK,
                state=state, plan_start=plan_start, plan_end=plan_end,
                actual_end=actual_end, estimated_hours=hours)


def test_overdue_rate_denominator_is_only_expected_tasks() -> None:
    """【能失败点 1】分母改成 len(tasks) ⇒ 本用例必须红。"""
    tasks = [
        mk_task("t1", date(2026, 5, 1), date(2026, 6, 1), TaskState.NOT_STARTED),  # 应完成·逾期
        mk_task("t2", date(2026, 5, 1), date(2026, 6, 20), TaskState.DONE),  # 应完成·未逾期
        mk_task("t3", date(2026, 6, 1), date(2026, 8, 1), TaskState.IN_PROGRESS),  # 尚未到期
    ]
    r = F.overdue_rate(tasks, AS_OF)
    assert r is not None
    assert (r.numerator, r.denominator) == (1, 2), "逾期率分母必须是应完成任务数，⛔ 全部任务"
    assert r.value == 0.5  # 若写成 1/3 就是系统性低估


def test_overdue_rate_returns_none_when_nothing_is_due() -> None:
    tasks = [mk_task("t1", date(2026, 7, 1), date(2026, 9, 1), TaskState.NOT_STARTED)]
    assert F.overdue_rate(tasks, AS_OF) is None  # ⛔ 0/0 或抛错：无数据就是无数据


def test_deviation_days_falls_back_to_as_of_and_can_be_negative() -> None:
    late = mk_task("t", date(2026, 6, 1), date(2026, 6, 10), TaskState.IN_PROGRESS)
    assert F.deviation_days(late, AS_OF) == 20
    early = mk_task("d", date(2026, 6, 1), date(2026, 6, 10), TaskState.DONE, actual_end=date(2026, 6, 5))
    assert F.deviation_days(early, AS_OF) == -5  # 提前完成必须是负数，⛔ 夹到 0


def test_elapsed_plan_ratio_is_not_clamped_when_overdue() -> None:
    """【能失败点 2】加 min(1.0, …) ⇒ 本用例必须红。"""
    t = mk_task("t", date(2026, 4, 1), date(2026, 4, 11), TaskState.IN_PROGRESS)  # 计划 10 天
    r = F.elapsed_plan_ratio(t, AS_OF)
    assert r is not None
    assert (r.numerator, r.denominator) == (90, 10)
    assert r.value == 9.0  # 延期 3 个月和刚好到期是两件事，夹紧就把它们变成同一个数


def test_elapsed_plan_ratio_zero_length_window_is_no_data() -> None:
    t = mk_task("t", date(2026, 6, 30), date(2026, 6, 30), TaskState.IN_PROGRESS)
    assert F.elapsed_plan_ratio(t, AS_OF) is None


def test_zero_budget_yields_no_data_not_zero() -> None:
    """【能失败点 3】去掉 budget_cents == 0 判空 ⇒ 本用例必须红（除零）。"""
    line = BudgetLine(project_code="PRJ-1000", version=1, subject=BudgetSubject.MATERIAL,
                      budget_cents=0, incurred_cents=0)
    assert F.budget_execution_rate(line, AS_OF) is None
    assert F.cpi_cents(0, 0, AS_OF) is None


def test_eac_requires_nonzero_incurred() -> None:
    assert F.eac_cents(100_00, 0) is None  # AC=0 外推 = 假数
    assert F.eac_cents(100_00, 50_00) == 200_00
    assert F.eac_cents(0, 10_00) is None


def test_cpi_is_budget_comparison_not_earned_value() -> None:
    """口径纪律：本项目 CPI = 已发生 ÷ 预算总额，⛔ 挣值 PV/EV/AC。"""
    r = F.cpi_cents(200_00, 150_00, AS_OF)
    assert r is not None and r.value == 0.75
    assert "挣值" in (F.cpi_cents.__doc__ or ""), "CPI 注释必须写明它是预算对比口径、非挣值"


def test_ratio_rejects_zero_denominator() -> None:
    with pytest.raises(ValueError):
        F.Ratio(1, 0, AS_OF)


def test_planned_amount_total_is_none_when_all_nodes_lack_amount() -> None:
    nodes = [PaymentNode(node_id=f"n{i}", project_code="PRJ-1000", stage=PaymentStage.PREPAYMENT,
                         due_on=date(2026, 1, 1)) for i in range(5)]
    assert F.planned_amount_total(nodes) is None  # ⛔ 0：那会伪装成"计划回款 0 元"


def test_aging_days_uses_paid_on_when_paid_else_as_of() -> None:
    unpaid = PaymentNode(node_id="a", project_code="P", stage=PaymentStage.ARRIVAL,
                         due_on=date(2026, 6, 1), status=PaymentStatus.OVERDUE_UNPAID)
    assert F.aging_days(unpaid, AS_OF) == 29
    paid = PaymentNode(node_id="b", project_code="P", stage=PaymentStage.ARRIVAL, due_on=date(2026, 6, 1),
                       status=PaymentStatus.OVERDUE_PAID, paid_on=date(2026, 6, 6))
    assert F.aging_days(paid, AS_OF) == 5


def test_actual_cost_excludes_shipment_and_form_conversion() -> None:
    """出库物料成本仅展示、不参与加总（真实口径）。"""
    total = F.actual_cost_cents(purchase=100_00, labor_internal=50_00, labor_external=20_00,
                                expense=10_00, returns=30_00)
    assert total == 150_00


def test_completion_rate_prefers_none_over_zero_denominator() -> None:
    assert F.completion_rate([], AS_OF) is None
    tasks = [mk_task("t1", date(2026, 1, 1), date(2026, 2, 1), TaskState.DONE, hours=10),
             mk_task("t2", date(2026, 1, 1), date(2026, 2, 1), TaskState.NOT_STARTED, hours=30)]
    assert F.completion_rate(tasks, AS_OF, weighted_by_hours=True).value == 0.25


def test_no_actual_duration_field_exists_on_project() -> None:
    """口径否决的落点是模型层：没有字段，下游就⛔ 算出假的"实际工期"。"""
    p = Project(code="PRJ-1000", name="n", customer="c", plan_start=date(2026, 1, 1), plan_end=date(2026, 2, 1))
    assert "actual_start" not in type(p).model_fields
    assert "actual_duration_days" not in type(p).model_fields


def test_tier_order_is_company_bu_subline_member() -> None:
    from forge.domain.models import ScopeTier, TIER_WEIGHT

    assert TIER_WEIGHT[ScopeTier.COMPANY] > TIER_WEIGHT[ScopeTier.BU] > TIER_WEIGHT[ScopeTier.SUBLINE] > TIER_WEIGHT[
        ScopeTier.MEMBER]


def test_milestone_requires_flag() -> None:
    with pytest.raises(ValueError):
        Task(task_id="x", project_code="P", name="x", plan_type=TaskKind.MILESTONE, is_milestone=False,
             state=TaskState.NOT_STARTED, plan_start=date(2026, 1, 1), plan_end=date(2026, 1, 2))
