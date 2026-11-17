"""计算层入口：三条分支唯一的数字来源。

⚠️ 编排（Line A / A2 / B）只准**调用**这里，⛔ 在节点里再算一遍数——
"三方对照测试"断言三条线数字逐字相同，靠的就是这个单一来源。
所有函数都是 ``Dataset`` 上的纯函数：⛔ 读系统时钟（``as_of`` 必须显式传入）、
⛔ 把无数据算成 0（返回 ``None``，由渲染层如实点名）。
"""
from __future__ import annotations

import functools
from datetime import date
from typing import Any, Optional

from forge.domain import formulas as F
from forge.domain.models import BudgetSubject, CostDocKind, Dataset, TaskKind
from forge.graph.state import Metric
from forge.synth.generate import AS_OF, build_dataset


@functools.lru_cache(maxsize=8)
def dataset_for(seed: int) -> Dataset:
    """按 seed 重载数据集。⛔ 把 Dataset 塞进图 state——checkpoint 只放引用（主流做法）。"""
    return build_dataset(seed)


def parse_as_of(value: Optional[str]) -> date:
    if not value:
        return AS_OF
    return date.fromisoformat(value)


def cents_to_yuan(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}¥{cents // 100:,}.{cents % 100:02d}"


def _ratio_metric(name: str, ratio: Optional[F.Ratio], from_: str, as_of: date,
                  note_empty: str, digits: int = 1) -> Metric:
    """派生量必须带 numerator/denominator/as_of ⇒ 任何答案里的数字都能反查。"""
    if ratio is None:
        return {"name": name, "numerator": None, "denominator": None, "value": None,
                "as_of": as_of.isoformat(), "from_": from_, "note": note_empty}
    return {"name": name, "numerator": ratio.numerator, "denominator": ratio.denominator,
            "value": ratio.percent(digits), "as_of": ratio.as_of.isoformat(),
            "from_": from_, "note": ""}


def _visible_tasks(ds: Dataset, projects: list[str]) -> list[Any]:
    want = set(projects)
    return [t for t in ds.tasks if t.project_code in want]


# ---------------------------------------------------------------- branches


def stats_branch(ds: Dataset, projects: list[str], as_of: date) -> tuple[list[Metric], dict[str, int]]:
    """进度统计：完成率 + 逾期率（分母只算"计划上应完成"的那批）。"""
    tasks = [t for t in _visible_tasks(ds, projects) if t.plan_type is not TaskKind.MILESTONE]
    metrics = [
        _ratio_metric("completion_rate", F.completion_rate(tasks, as_of),
                      "task_table", as_of, "无数据：可见范围内没有任务行"),
        _ratio_metric("overdue_rate", F.overdue_rate(tasks, as_of),
                      "task_table", as_of, "无数据：截至基准日没有应完成任务"),
    ]
    return metrics, {"task_table": len(tasks), "projects": len(set(projects))}


def deviation_branch(ds: Dataset, projects: list[str], as_of: date) -> tuple[list[Metric], dict[str, int]]:
    """里程碑偏差：脱期里程碑占比 + 最大偏差天数（偏差天数是整数，⛔ 让模型做减法）。"""
    tasks = _visible_tasks(ds, projects)
    pairs = F.milestone_deviations(tasks, as_of)
    total = len(pairs)
    off = sum(1 for _, d in pairs if d != 0)
    metrics: list[Metric] = []
    if total:
        metrics.append(_ratio_metric("milestone_offschedule_rate",
                                     F.Ratio(off, total, as_of),
                                     "milestone_table", as_of, ""))
        worst = max(pairs, key=lambda p: abs(p[1]))
        metrics.append({"name": "max_milestone_deviation_days", "numerator": worst[1],
                        "denominator": None, "value": f"{worst[1]:+d} 天（{worst[0]}）",
                        "as_of": as_of.isoformat(), "from_": "milestone_table", "note": ""})
    else:
        metrics.append({"name": "milestone_offschedule_rate", "numerator": None,
                        "denominator": None, "value": None, "as_of": as_of.isoformat(),
                        "from_": "milestone_table", "note": "无数据：可见范围内没有里程碑行"})
    return metrics, {"milestone_table": total, "offschedule": off}


def cost_branch(ds: Dataset, projects: list[str], as_of: date) -> tuple[list[Metric], dict[str, int]]:
    """成本执行：预算执行率（逐科目）+ CPI + EAC。⚠️ shipment 单据不参与成本加总（真实口径）。"""
    want = set(projects)
    current = {v.project_code for v in ds.budget_versions if v.is_current}
    lines = [b for b in ds.budget_lines if b.project_code in want and b.project_code in current]
    metrics: list[Metric] = []
    for subject in BudgetSubject:
        subset = [b for b in lines if b.subject is subject]
        budget = sum(b.budget_cents for b in subset)
        incurred = sum(b.incurred_cents for b in subset)
        if not subset:
            continue
        ratio = F.budget_execution_rate(subset[0].model_copy(update={
            "budget_cents": budget, "incurred_cents": incurred}), as_of)
        metrics.append(_ratio_metric(f"budget_execution[{subject.value}]", ratio,
                                     "cost_docs", as_of, "无数据：该科目预算为 0"))
    docs = [c for c in ds.cost_docs if c.project_code in want and c.kind is not CostDocKind.SHIPMENT
            and c.occurred_on <= as_of]
    incurred_total = sum(c.amount_cents for c in docs)
    budget_total = sum(b.budget_cents for b in lines)
    cpi = F.cpi_cents(budget_total, incurred_total, as_of) if budget_total else None
    metrics.append(_ratio_metric("cpi", cpi, "cost_docs", as_of, "无数据：预算合计为 0，CPI 不可算"))
    if cpi is not None and cpi.numerator == 0:
        # 0% 是**真值**（分母有数、分子为 0），与"无数据"是两回事，但要点明免得被读成算错
        metrics[-1]["note"] = "已发生成本为 0，故 CPI=0%（⛔ 与「无数据」混同）"
    eac = F.eac_cents(budget_total, incurred_total)
    if eac is None:
        metrics.append({"name": "eac", "numerator": None, "denominator": None, "value": None,
                        "as_of": as_of.isoformat(), "from_": "cost_docs",
                        "note": "无数据：尚无实际成本发生额，EAC 不折算为 ¥0.00"})
    else:
        metrics.append({"name": "eac", "numerator": eac, "denominator": None,
                        "value": cents_to_yuan(eac), "as_of": as_of.isoformat(),
                        "from_": "cost_docs", "note": ""})
    return metrics, {"cost_docs": len(docs), "budget_lines": len(lines)}


BRANCH_COMPUTE = {
    "stats": stats_branch,
    "deviation": deviation_branch,
    "cost": cost_branch,
}


def run_branch(branch: str, seed: int, projects: list[str],
               as_of_s: Optional[str] = None) -> dict[str, Any]:
    """给工具层与图节点共用的唯一入口。"""
    if branch not in BRANCH_COMPUTE:
        raise KeyError(f"unknown branch: {branch}")
    ds = dataset_for(seed)
    as_of = parse_as_of(as_of_s)
    metrics, row_counts = BRANCH_COMPUTE[branch](ds, projects, as_of)
    return {"branch": branch, "metrics": [dict(m) for m in metrics],
            "row_counts": row_counts, "as_of": as_of.isoformat()}
