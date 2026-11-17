"""guard / router / 信封：fail-closed 与"无数据 ≠ 0"是这里唯一的重点。

⚠️ 这些测试的取值全部来自 D1 合成数据的**真实形状**（750 用户只有 18 条权限配置、
BU 分布 180/90/10/10 + 10 个未填），数字可用 ``forge.synth.generate`` 复算，⛔ 手填。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from forge.domain.models import ScopeTier
from forge.graph import compute, contract
from forge.graph.guard import decide, visible_projects
from forge.graph.plan_graph import build_plan_graph
from forge.graph.router import named_projects, route

SEED = 42


@pytest.fixture(scope="module")
def ds():
    return compute.dataset_for(SEED)


def _perm(ds, user_key: str):
    return next((p for p in ds.permissions if p.user_key == user_key), None)


# ------------------------------------------------------------------ router


def test_router_maps_keywords_to_existing_branches_only(ds) -> None:
    r = route("PRJ-1180 的成本超支了吗", ds)
    assert r["skills"] == ["cost"], r
    assert set(r["skills"]) <= set(compute.BRANCH_COMPUTE), "路由吐出了不存在的技能名"


def test_router_falls_back_to_full_set_without_inventing_names(ds) -> None:
    r = route("随便看看", ds)
    assert sorted(r["skills"]) == sorted(compute.BRANCH_COMPUTE)


def test_router_extracts_named_projects_and_validates_them(ds) -> None:
    assert named_projects("对比 PRJ-1180 与 PRJ-1007", ds) == ["PRJ-1007", "PRJ-1180"]
    assert named_projects("PRJ-9999 呢", ds) == []  # 不存在的项目码 ⛔ 进入点名清单
    assert route("PRJ-1180 进度", ds)["write_intent"] is False
    assert route("帮我立项", ds)["write_intent"] is True


# ------------------------------------------------------------------- guard


def test_no_profile_falls_to_member_floor(ds) -> None:
    """750 用户只有 18 条配置 ⇒ "查不到"是常态，默认策略必须显式且收紧。"""
    unconfigured = next(u.user_key for u in ds.users if _perm(ds, u.user_key) is None)
    scope = decide(ds, unconfigured)
    assert scope["tier"] == ScopeTier.MEMBER.value
    assert scope["effective_tier"] == ScopeTier.MEMBER.value
    assert scope["downgraded"] is True
    assert all(any(t.owner == unconfigured and t.project_code == p
                   for t in ds.tasks) for p in scope["visible_projects"])


def test_bu_without_narrowing_value_downgrades_instead_of_sharing_all(ds) -> None:
    """⛔「档位够就全放行」：bu 档但算不出 business_unit ⇒ 降级到本人相关。

    ⚠️ 这个形状在 D1 合成数据里**不存在**（BU 档 18 条配置全部带 business_unit，实测 0 例外），
    所以只能直接打函数边界造它——真实系统里"有档无收窄值"恰恰是最常见的一类脏配置。
    """
    withbu = next(p for p in ds.permissions if p.tier is ScopeTier.BU)
    projects, why, downgraded = visible_projects(ds, ScopeTier.BU, None, None, withbu.user_key)
    assert downgraded is True and "降级" in why, why
    assert projects == sorted({t.project_code for t in ds.tasks if t.owner == withbu.user_key})
    full_bu, _, _ = visible_projects(ds, ScopeTier.BU, withbu.business_unit, None, withbu.user_key)
    assert len(projects) < len(full_bu), "降级后必须严格更窄，⛔ 等于原档"


def test_subline_is_narrower_than_bu(ds) -> None:
    """顺序 company > bu > subline > member。写反会让子产品线管理层看到整个 BU。"""
    sub = next(p for p in ds.permissions if p.tier is ScopeTier.SUBLINE
               and p.business_unit and p.project_type)
    bu_projects, _, _ = visible_projects(ds, ScopeTier.BU, sub.business_unit,
                                         sub.project_type, sub.user_key)
    sub_projects, _, downgraded = visible_projects(ds, sub.tier, sub.business_unit,
                                                   sub.project_type, sub.user_key)
    assert downgraded is False
    assert set(sub_projects) <= set(bu_projects)
    assert len(sub_projects) < len(bu_projects), "subline 没收窄 ⇒ 等于 bu 档，顺序写反了"


def test_company_tier_sees_everything_and_member_sees_least(ds) -> None:
    comp = next(p for p in ds.permissions if p.tier is ScopeTier.COMPANY)
    all_p, _, d1 = visible_projects(ds, comp.tier, None, None, comp.user_key)
    mem_p, _, d2 = visible_projects(ds, ScopeTier.MEMBER, None, None, comp.user_key)
    assert d1 is False and len(all_p) == len(ds.projects)
    assert len(mem_p) < len(all_p)


def test_named_out_of_scope_short_circuits_without_data(tmp_path: Path, ds) -> None:
    """u0041 是 bu-beta；PRJ-1007 属 bu-alpha ⇒ 必须拒答，⛔ 任何数字外泄。"""
    app = build_plan_graph(scratch_dir=tmp_path).compile()
    out = app.invoke({"query": "PRJ-1007 的进度怎么样", "user_key": "u0041", "seed": SEED,
                      "as_of": "2026-10-30", "thread_id": "guard-1"})
    env = out["envelope"]
    assert env["ok"] is False and env["code"] == "NOT_IN_SCOPE"
    assert env["digest"] == [], "拒答还带 digest ⇒ 已经在下游取过数了"
    assert not any(f.name.startswith("guard-1") for f in Path(tmp_path).glob("*.json")), \
        "越权请求不该产生交接文件"
    # 拒答⛔ 顺带泄露"你其实能看哪些"：可见项目码一个都不许出现在答案里
    scope = decide(ds, "u0041", ["PRJ-1007"])
    assert scope["named_outside"] == ["PRJ-1007"]
    visible_in_answer = [c for c in scope["visible_projects"] if c in env["answer"]]
    assert visible_in_answer == [], f"拒答里泄露了可见项目 {visible_in_answer}"


# ----------------------------------------------------------------- 信封


def test_envelope_shape_and_banned_words(tmp_path: Path) -> None:
    app = build_plan_graph(scratch_dir=tmp_path).compile()
    out = app.invoke({"query": "PRJ-1180 进度、偏差和成本", "user_key": "u0000", "seed": SEED,
                      "as_of": "2026-10-30", "thread_id": "env-1"})
    env = out["envelope"]
    assert list(env) == list(contract.ENVELOPE_KEYS), tuple(env)
    contract.assert_no_banned(env["answer"])
    for word in contract.BANNED_PHRASES:
        assert word not in " ".join(env["notes"])


def test_numbers_in_answer_come_verbatim_from_digest(tmp_path: Path) -> None:
    """渲染层⛔ 做任何算术：answer 里每个形如 x/y 与百分比都必须能在 digest 里反查到。"""
    app = build_plan_graph(scratch_dir=tmp_path).compile()
    out = app.invoke({"query": "PRJ-1180 进度、偏差和成本", "user_key": "u0000", "seed": SEED,
                      "as_of": "2026-10-30", "thread_id": "env-2"})
    answer, digest = out["envelope"]["answer"], out["envelope"]["digest"]
    assert digest, "全量三条分支应当有指标"
    for m in digest:
        if m["value"] is None:
            assert m["name"] in answer, "无数据项必须在 answer 里点名"
            assert f"{m['name']} 0" not in answer
        else:
            assert m["value"] in answer, f"{m['name']}={m['value']} 没进 answer ⇒ 渲染层改了数"
        if m["numerator"] is not None and m["denominator"] is not None:
            assert f"{m['numerator']:,}/{m['denominator']:,}" in answer
    assert all(m["as_of"] == "2026-10-30" for m in digest), "基准日必须逐条可查"


def test_no_data_at_compute_layer_is_none_not_zero(ds) -> None:
    """计算层：可见集为空 ⇒ 返回 None，⛔ 返回 0（这一条⛔ 靠 UI 遮）。"""
    as_of = compute.parse_as_of("2026-10-30")
    metrics, _ = compute.stats_branch(ds, [], as_of)
    assert all(m["value"] is None and m["numerator"] is None for m in metrics), metrics
    assert all(m["note"] for m in metrics), "无数据必须带原因，⛔ 静默空值"


def test_no_data_is_not_zero(tmp_path: Path) -> None:
    """图层面：一个**既无权限配置也不承担任何任务**的账号（真实系统里就是刚开号/挂空的账号）
    ⇒ answer 必须写"无数据"，⛔ 出现 0% / ¥0.00。

    ⚠️ ⛔ 从 ds.users 里挑"无任务用户"——实测 750 个用户**每人都至少承担一条任务**，
    那样写必然 StopIteration；用一个不在用户表里的账号才是这个形状的正确构造法。
    """
    app = build_plan_graph(scratch_dir=tmp_path).compile()
    out = app.invoke({"query": "进度完成率、里程碑偏差和成本", "user_key": "u9999", "seed": SEED,
                      "as_of": "2026-10-30", "thread_id": "env-3"})
    env = out["envelope"]
    assert env["code"] == "OK", env
    assert "无数据" in env["answer"], env["answer"]
    assert "【无数据】" in env["answer"]
    for name in ("completion_rate", "overdue_rate", "cpi", "eac"):
        assert f"{name} 0%" not in env["answer"], f"{name} 被渲染成 0% ⇒ 无数据当 0 用了"
        assert f"{name} ¥0.00" not in env["answer"]
