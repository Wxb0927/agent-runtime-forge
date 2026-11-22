"""D4 评测流水线自身的测试：**每个评测器都要证明它抓得住坏样本**。

⚠️ 这些用例喂的是**构造出来的坏数据**（用假 Run/假信封，⛔ 改磁盘源码），
目的是证明评测器不是摆设。真实源码级的"改歪→红"另见 ``tools/mutation_check.py``
的 D4 组（M11–M15）——两层都需要：这层证明逻辑对，那层证明测试真的连着实现。

为什么这么较真：一条从没红过的评测规则等于⛔ 存在。生产上我吃过这个亏——
自挑验收集报 100%，换矩阵枚举后大幅回落。
"""
from __future__ import annotations

import json
import re
from typing import Any

import pytest

from eval import classify, dataset, diff, judge, matrix, rules, system


def make_run(envelope: dict[str, Any], *, query: str = "q", skills: list[str] | None = None,
             scope: list[str] | None = None) -> system.Run:
    return system.Run(query=query, user_key="u0000", as_of="2026-10-30", envelope=envelope,
                      skills=skills or ["stats"], scope_projects=scope or ["PRJ-1180"])


def good_envelope() -> dict[str, Any]:
    digest = [{"name": "completion_rate", "numerator": 33, "denominator": 38,
               "value": "86.8%", "as_of": "2026-10-30", "from_": "task_table", "note": "",
               "branch": "stats"}]
    answer = ("【范围】company｜收窄=无｜可见 1 个项目｜基准日 2026-10-30\n"
              "【进度】completion_rate 86.8%（33/38）\n"
              "【来源】stats←task_table(第 1 步)")
    return {"ok": True, "code": "OK", "message": "", "digest": digest, "notes": [], "answer": answer}


# ------------------------------------------------------------------ ② 规则


def test_rules_pass_on_a_good_answer() -> None:
    assert rules.score(make_run(good_envelope())) == []


def test_rules_catch_a_fabricated_number() -> None:
    env = good_envelope()
    env["answer"] += "\n【进度】另有估算值 42.0%（9/21）"       # digest 里没有 ⇒ 凭空数字
    hits = [f.rule for f in rules.score(make_run(env))]
    assert "fabricated-number" in hits, hits


def test_rules_catch_banned_words() -> None:
    env = good_envelope()
    env["answer"] = env["answer"].replace("86.8%（33/38）", "大概 86.8%（33/38）")
    assert "banned-words" in [f.rule for f in rules.score(make_run(env))]


def test_rules_catch_missing_no_data_disclosure() -> None:
    """"无数据 ≠ 0"：value=None 又⛔ 在 answer 点名 ⇒ 必须红。"""
    env = good_envelope()
    env["digest"].append({"name": "eac", "numerator": None, "denominator": None,
                          "value": None, "as_of": "2026-10-30", "from_": "cost_docs",
                          "note": "无数据", "branch": "cost"})
    hits = [f.rule for f in rules.score(make_run(env), check_oracle=False)]
    assert "no-data-named" in hits, hits


def test_rules_catch_unregistered_section_and_bad_order() -> None:
    env = good_envelope()
    env["answer"] = "【结论】先给结论\n【进度】completion_rate 86.8%（33/38）\n【瞎写】x"
    found = rules.score(make_run(env), check_oracle=False)
    assert {"section-order", "section-unknown"} <= {f.rule for f in found}


def test_rules_catch_lost_traceability() -> None:
    """丢分子/分母（D4 验收用的就是这一手篡改）⇒ traceable 规则必须红。"""
    env = good_envelope()
    env["answer"] = "【范围】company｜基准日 2026-10-30\n【进度】completion_rate 86.8%"
    assert "traceable" in [f.rule for f in rules.score(make_run(env), check_oracle=False)]


# ------------------------------------------------------------------ ③ judge


def test_judge_requires_a_citation_line_for_every_number() -> None:
    lines = judge.numbered_digest(good_envelope()["digest"], host="user_key=u0000 | as_of=2026-10-30")
    cites = judge.cite_numbers(good_envelope()["answer"], lines)
    assert cites and all(c["line"] for c in cites), cites
    assert not any(c["fabricated"] for c in cites)


def test_judge_flags_numbers_with_no_source_line() -> None:
    lines = judge.numbered_digest(good_envelope()["digest"], host="user_key=u0000 | as_of=2026-10-30")
    cites = judge.cite_numbers("完成率 99.9%（999/1000）", lines)
    assert any(c["fabricated"] for c in cites), cites


def test_judge_reports_what_it_cannot_evaluate_offline() -> None:
    """⛔ 让打桩冒充真模型：未跑的语义项必须显式列出来。"""
    res = judge.judge_case({"id": "x"}, make_run(good_envelope()))
    assert res["mode"] == "rule" and res["llm"] == "skipped"
    assert any("因果" in s or "复述" in s for s in res["not_evaluated"]), res["not_evaluated"]


def test_judge_uses_the_live_model_when_given_one() -> None:
    class FakeJudge:
        def invoke(self, _msgs):
            class R:
                content = json.dumps({"verdict": "fail", "numbers": [],
                                      "self_computed": ["把两行相加"], "fabricated": ["71%"],
                                      "reasons": ["答案里有 71% 但工具没给"]})
            return R()

    env = good_envelope()
    res = judge.judge_case({"id": "x"}, make_run(env), llm=FakeJudge())
    assert res["mode"] == "rule+llm" and res["rule_verdict"] == "fail"
    assert res["llm_fabricated"] == ["71%"], res


# ------------------------------------------------------------------ ④ 差集


def test_diff_flags_unregistered_change_as_new_red() -> None:
    base = {"id": "a", "code": "OK", "answer": "【进度】x 86.8%（33/38）", "digest": []}
    cur = {"id": "a", "code": "OK", "answer": "【进度】x 86.8%", "digest": []}
    out = diff.run_diff([cur], {"a": base}, {})
    assert len(out["new_red"]) == 1
    ch = out["changed"][0]["changes"]["answer"]
    assert ch["removed"] == ["【进度】x 86.8%（33/38）"] and ch["added"] == ["【进度】x 86.8%"]


def test_diff_respects_registered_known_red() -> None:
    base = {"id": "a", "code": "OK", "answer": "旧", "digest": []}
    cur = {"id": "a", "code": "OK", "answer": "新", "digest": []}
    out = diff.run_diff([cur], {"a": base}, {"a": "口径已确认变更，待补文档"})
    assert out["new_red"] == [] and len(out["registered_changes"]) == 1


def test_diff_detects_removed_case() -> None:
    row = {"id": "gone", "code": "OK", "answer": "x", "digest": []}
    out = diff.run_diff([], {"gone": row}, {})      # 基线是 {id: row} 形状
    assert out["new_red"] and out["new_red"][0]["id"] == "gone"


def test_first_run_without_baseline_does_not_fake_red() -> None:
    """基线未建立时"新增"是正常状态，⛔ 报成 82 条红逼人先 rebase。"""
    out = diff.run_diff([{"id": "a", "code": "OK", "answer": "x", "digest": []}], {}, {})
    assert out["baseline_established"] is False and out["new_red"] == []


def test_known_red_validation_rejects_placeholder_and_zombie() -> None:
    problems = diff.validate_known_red({"a": "todo", "b": "真理由够长了吧", "zz": "用例已删除"},
                                       {"a", "b"})
    assert any("占位符" in p for p in problems) and any("僵尸豁免" in p for p in problems)


# ------------------------------------------------------------------ ⑤ 矩阵


def test_matrix_counts_extra_skills_and_worst_phrasing() -> None:
    cells = [
        {"id": "1", "domain": "completion_rate", "phrasing": "plain", "hit": True,
         "skills": ["stats"], "red_count": 0},
        {"id": "2", "domain": "cpi", "phrasing": "ask_why", "hit": False,
         "skills": ["cost", "deviation", "stats"], "red_count": 1},
    ]
    out = matrix.summarize(cells)
    assert out["cells"] == 2 and out["hits"] == 1
    assert out["extra_skill_rate"] == 1.0, out                # (0 + 2) / 2 格：只数用不到的技能
    assert out["red_cells"] == 1
    assert out["worst_phrasings"][0]["phrasing"] == "ask_why"  # 最差的排最前


def test_matrix_empty_does_not_crash() -> None:
    out = matrix.summarize([])
    assert out["cells"] == 0 and out["hit_rate"] is None and out["extra_skill_rate"] is None


def test_matrix_admits_hit_rate_is_structurally_inflated() -> None:
    """报告里必须自带这句：命中率因路由回退全技能而**结构性偏高**。"""
    assert "回退" in matrix.HIT_RATE_CAVEAT and "100%" in matrix.HIT_RATE_CAVEAT


def test_matrix_domains_come_from_the_registry() -> None:
    """域轴与注册表同源：加一个指标就多一行格，⛔ 手抄。"""
    from forge.contract import metrics as M

    from eval import phrasings
    assert phrasings.domains() == M.declared_metrics()
    rows = phrasings.grid_shape()
    assert rows[0] == len(M.declared_metrics()) and rows[0] * rows[1] >= 56, "任务书要求 ≥56 格"


# ------------------------------------------------------------------ ①⑥


def test_dataset_rejects_duplicate_ids(tmp_path) -> None:
    p = tmp_path / "cases.jsonl"
    p.write_text(json.dumps({"id": "dup", "mode": "nl", "input": {}, "expect": {}}) + "\n"
                 + json.dumps({"id": "dup", "mode": "nl", "input": {}, "expect": {}}) + "\n",
                 encoding="utf-8")
    with pytest.raises(AssertionError, match="重复"):
        dataset.load_cases(p)


def test_dataset_cases_carry_no_hardcoded_numbers() -> None:
    """语料里⛔ 存期望数字（期望值必须现算自计算层，否则公式一改集体过期）。"""
    for case in dataset.build_cases():
        assert "numerator" not in json.dumps(case), case["id"]
        assert not re.search(r"\d\.\d%", json.dumps(case, ensure_ascii=False)), case["id"]


def test_classify_routes_findings_into_named_buckets() -> None:
    env = good_envelope()
    env["answer"] += "\n【进度】另有 42.0%（9/21）"
    run = make_run(env)
    label = classify.classify_case({"id": "x"}, run, [{"rule": "fabricated-number", "message": "",
                                                       "severity": "red"}])
    assert label["primary"] == "上下文污染", label
    env2 = dict(good_envelope(), code="NOT_IN_SCOPE", ok=False, digest=[], answer="")
    assert classify.classify_case({"id": "y"}, make_run(env2), [])["primary"] == "弃权边界"


def test_classify_prioritizes_pollution_over_abstain() -> None:
    """同时命中"污染"和"弃权"时要报污染——⛔ 把真问题归成良性弃权。"""
    run = make_run(dict(good_envelope(), code="NOT_IN_SCOPE"))
    label = classify.classify_case({"id": "z"}, run, [{"rule": "banned-words", "message": "",
                                                       "severity": "red"}])
    assert label["primary"] == "上下文污染", label
