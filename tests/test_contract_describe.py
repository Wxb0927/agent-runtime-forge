"""``describe()`` 必须是**代码派生**的——本文件逐条证明它没有第二份手抄清单。

判据思路：⛔ 只测"卡片长什么样"（那手抄也能抄对），而是测
**"卡片与代码同源"**——换一个来源就必须跟着变。
"""
from __future__ import annotations

import inspect
import json

import pytest

from forge.contract import cards as C
from forge.contract import intent as I
from forge.contract import metrics as M
from forge.graph.compute import BRANCH_COMPUTE, stats_branch


def test_params_derive_from_the_pydantic_model():
    """参数名（含 alias）与必填性都来自 ``SkillIntent``，⛔ 另有一份清单。"""
    card = C.describe()["stats"]
    expected = {C.alias_of(k) for k in C.SkillIntent.model_fields}
    assert set(card["params"]) == expected
    required = {C.alias_of(k) for k, f in C.SkillIntent.model_fields.items() if f.is_required()}
    assert {p for p, d in card["params"].items() if d["required"]} == required


def test_caps_come_from_field_metadata():
    """上限只有一个来源：pydantic 的 ``le``。⛔ 说明书写 50、代码钳到 20。"""
    card = C.describe()["stats"]["params"]["topK"]
    bounds = C._bounds(C.SkillIntent.model_fields["top_k"])
    assert card["max"] == bounds["max"] == C.TOP_K_MAX
    assert card["min"] == bounds["min"]


def test_summary_is_the_compute_docstring_first_line():
    """``summary`` 直接取 compute 函数 docstring 第一行——改了注释，说明书跟着变。"""
    assert C.describe()["stats"]["summary"] == (stats_branch.__doc__ or "").strip().splitlines()[0]


def test_metric_enum_is_branch_filtered_and_ast_derived():
    reg = M.parsed_registry()
    for branch in sorted(BRANCH_COMPUTE):
        enum = C.describe()[branch]["enums"]["metric"]
        assert enum == reg[branch], f"{branch} 的枚举与 AST 解析结果不一致"
        assert all(M.branch_of(m) == branch for m in enum), "枚举串台了"


def test_host_injected_derives_from_run_signature():
    """宿主注入项（身份 / 基准日 / seed）由 ``run()`` 签名派生，⛔ 手抄，也⛔ 混进意图面。"""
    injected = C.describe()["stats"]["host_injected"]
    sig_params = {p for p in inspect.signature(I.run).parameters if p != "intent"}
    assert set(injected) == sig_params
    assert set(C.describe()["stats"]["params"]).isdisjoint(injected), "意图面与宿主注入混了"


def test_cards_json_matches_live_derivation():
    """发给宿主的菜单是生成物：与当下代码逐字相等（G3 的同一判据，双向都测）。"""
    committed = json.loads(C.CARDS_PATH.read_text(encoding="utf-8"))
    assert committed == C.describe()


def test_describe_is_byte_stable():
    """同一份代码两次生成必须逐字节相同，⛔ 靠 dict 顺序碰运气。"""
    assert C.cards_json() == C.cards_json()
    assert json.loads(C.cards_json()) == json.loads(C.cards_json())


def test_cards_declare_no_handwritten_metric_list():
    """源码级自检：cards.py 里⛔ 出现指标名字面量（枚举必须来自 metrics.declared_metrics）。"""
    src = inspect.getsource(C)
    for name in ("completion_rate", "overdue_rate", "milestone_offschedule_rate", "cpi", "eac"):
        assert f'"{name}"' not in src, f"cards.py 里手抄了指标名 {name}"


@pytest.mark.parametrize("branch", sorted(BRANCH_COMPUTE))
def test_every_branch_has_a_card(branch):
    card = C.describe(branch)[branch]
    assert card["name"] == branch and card["enums"]["metric"]
