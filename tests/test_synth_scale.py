"""§3.5 规模与稀疏性断言。

⚠️ 这些数字是**精确值**而不是近似值——生成器用同余计数保证（见 generate.py 模块 docstring）。
所以这里的断言写成 `==`，⛔ 写成 `abs(x - y) < tol`：一旦有人为了"数据好看"把稀疏性抹平，
这些用例会立刻红，而不是悄悄漂走。
"""
from __future__ import annotations

from collections import Counter

import pytest

from forge.domain.models import CostDocKind, PaymentStage, TaskKind, TaskState
from forge.synth.generate import AS_OF, build_dataset, to_jsonl


@pytest.fixture(scope="module")
def ds():
    return build_dataset(42)


def test_project_scale_and_bu_distribution(ds):
    assert len(ds.projects) == 300
    c = Counter(p.business_unit for p in ds.projects)
    assert c["bu-alpha"] == 180
    assert c["bu-beta"] == 90
    assert c["bu-gamma"] == 10
    assert c["bu-delta"] == 10
    assert c[None] == 10, "⛔ 把'未填'补成具体 BU —— 那正是真实系统里口径出错的来源"
    assert sum(c.values()) == 300


def test_task_total_is_twelve_thousand(ds):
    assert len(ds.tasks) == 12_000


def test_task_kind_mix_is_real_shapes(ds):
    """四类对象混在一张表里，靠谓词区分。"""
    c = Counter(t.plan_type for t in ds.tasks)
    assert c[TaskKind.PHASE] == 1_200
    assert c[TaskKind.MILESTONE] == 600
    assert c[TaskKind.OPERATION] == 2_400
    assert c[TaskKind.TASK] == 7_800
    assert sum(c.values()) == 12_000


def test_overdue_not_started_is_exactly_1400(ds):
    got = [t for t in ds.tasks if t.state is TaskState.NOT_STARTED and t.plan_end < AS_OF]
    assert len(got) == 1_400


def test_delayed_in_progress_is_exactly_60(ds):
    got = [t for t in ds.tasks if t.state is TaskState.IN_PROGRESS and t.plan_end < AS_OF]
    assert len(got) == 60


def test_milestone_flag_matches_kind(ds):
    for t in ds.tasks:
        assert t.is_milestone == (t.plan_type is TaskKind.MILESTONE)


def test_no_task_has_actual_start_because_the_field_does_not_exist(ds):
    """口径否决的硬落点：模型里没有 actual_start。"""
    assert "actual_start" not in type(ds.tasks[0]).model_fields


def test_payment_plan_sparsity_is_exactly_eight_percent(ds):
    assert len(ds.payment_nodes) == 2_300
    with_amt = sum(1 for n in ds.payment_nodes if n.plan_amount_cents is not None)
    assert with_amt == 184
    assert with_amt / len(ds.payment_nodes) == 0.08
    # ⚠️ 这条存在的原因：8% 的填充率逼出"金额一律以计划表为主源"的设计
    assert sum(1 for n in ds.payment_nodes if n.plan_amount_cents is None) == 2_116


def test_ecn_cost_is_ninety_percent_zero(ds):
    assert len(ds.ecns) == 120
    nonzero = sum(1 for e in ds.ecns if e.cost_cents != 0)
    assert nonzero == 12
    assert nonzero / len(ds.ecns) == 0.10, "变更成本 90% 未录入是真实状况，⛔ 填满"


def test_labor_report_sparsity_matches_real_reading(ds):
    """真实读数：615 条报工里只有 9 条有值 ⇒ 人工成本⛔ 默认 0。"""
    lr = [c for c in ds.cost_docs if c.kind is CostDocKind.LABOR_REPORT]
    assert len(lr) == 615
    assert sum(1 for c in lr if c.amount_cents != 0) == 9


def test_cost_doc_counts_match_spec(ds):
    c = Counter(x.kind for x in ds.cost_docs)
    assert c[CostDocKind.PURCHASE] == 700
    assert c[CostDocKind.SHIPMENT] == 700
    assert c[CostDocKind.RETURN] == 12
    assert c[CostDocKind.EXPENSE] == 40


def test_contract_link_is_three_of_ninety_four(ds):
    assert len(ds.contracts) == 94
    linked = sum(1 for x in ds.contracts if x.project_code is not None)
    assert linked == 3, "合同↔项目几乎没关联；想按合同反查项目金额就是错的路"


def test_deliverables_ten_each_with_zero_to_three_delivered(ds):
    assert len(ds.deliverables) == 3_000
    per = Counter()
    for d in ds.deliverables:
        if d.delivered:
            per[d.project_code] += 1
    counts = [per.get(p.code, 0) for p in ds.projects]
    assert max(counts) <= 3, "已交付数超出 0–3 ⇒ 稀疏性被填平了"
    assert min(counts) >= 0
    assert len(set(counts)) >= 3, "每项目数量必须真的有浮动，⛔ 恒定值假装多样"


def test_permission_configs_are_far_below_user_count(ds):
    """750 用户 / 18 条显式配置 ⇒ 默认放行策略必须显式定义。"""
    assert len(ds.users) == 750
    assert len(ds.permissions) == 18
    assert len({p.user_key for p in ds.permissions}) <= 18


def test_budget_lines_four_subjects_and_some_zero_budget(ds):
    assert len(ds.budget_lines) == 1_200
    zero = [l for l in ds.budget_lines if l.budget_cents == 0]
    assert zero, "保留零预算科目，否则永远测不到'执行率无数据'那条分支"


def test_dependency_edges_declare_their_source(ds):
    """依赖边必须说清来源：doc = 规格给定，inferred = 从真实调用链反推。"""
    assert ds.task_deps
    assert {e.source for e in ds.task_deps} <= {"doc", "inferred"}
    assert any(e.source == "inferred" for e in ds.task_deps), "完整图谱主要靠反推，只留 doc 就是假装有规格依据"


def test_payment_stages_cover_four(ds):
    assert {n.stage for n in ds.payment_nodes} == set(PaymentStage)


def test_serialization_is_stable_and_sized(ds):
    body = to_jsonl(ds)
    assert body.endswith("\n")
    expected = sum(len(x) for x in (
        ds.projects, ds.tasks, ds.task_deps, ds.budget_versions, ds.budget_lines, ds.cost_docs,
        ds.payment_nodes, ds.contracts, ds.ecns, ds.issues, ds.deliverables, ds.users, ds.permissions))
    assert body.count("\n") == expected
