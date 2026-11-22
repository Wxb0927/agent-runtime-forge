"""D4 评估流水线总入口：``python -m eval.run``（等价于 ``make eval``）。

一次跑完 8 项：

1. 契约层三道门禁（G1/G2/G3，复用 ``forge.contract.gates``）
2. ① 测试集 → ② 规则评分 → ③ judge → ⑥ 失败归类
3. ⑤ 覆盖率矩阵（**由同一批用例的结果汇总，⛔ 再跑一遍**）
4. ④ 回归差集（与 ``baseline.jsonl`` 逐句比，未登记即 ``new_red``）
5. 写 ``eval/report.md`` + ``eval/report.json``
6. ⑦ ``--check``：``new_red`` 或非豁免红 ⇒ ``exit 1``（PR 前自动检查）

首跑要先建基线：``--write-cases`` 生成语料，``--rebase`` 建立基线。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from eval import classify, dataset, diff, judge, matrix, rules, system
from forge.contract import gates as contract_gates
from forge.graph import contract as env_contract

REPORT_MD = Path("eval") / "report.md"
REPORT_JSON = Path("eval") / "report.json"


def run_cases(cases: list[dict[str, Any]], *, llm_judge: Any = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for case in cases:
        run = system.run_case(case)
        findings = rules.score(run)
        verdicts = judge.judge_case(case, run, llm=llm_judge)
        label = classify.classify_case(case, run, [f.as_dict() for f in findings])
        expect = case.get("expect") or {}
        mismatches: list[str] = []
        if expect.get("code") and run.envelope.get("code") != expect["code"]:
            mismatches.append(f"期望 code={expect['code']}，实得 {run.envelope.get('code')}")
        if expect.get("metric_present") and run.metric(expect["metric_present"]) is None:
            mismatches.append(f"答案里没有目标指标 {expect['metric_present']}")
        if expect.get("digest_empty") and run.digest:
            mismatches.append("期望不取数，digest 却非空")
        for frag in expect.get("note_has") or []:
            if not any(frag in str(n) for n in (run.envelope.get("notes") or [])):
                mismatches.append(f"notes 里缺 {frag}")
        reds = [f.as_dict() for f in findings if f.severity == "red"]
        out.append({
            "id": case["id"], "domain": case.get("domain"), "phrasing": case.get("phrasing"),
            "query": run.query, "code": run.envelope.get("code"), "ok": run.envelope.get("ok"),
            "skills": run.skills, "needed": run.needed_skills,
            "answer": run.envelope.get("answer") or "",
            "digest": run.digest,
            "hit": bool(run.metric(expect["metric_present"])) if expect.get("metric_present") else None,
            "expect_mismatch": mismatches,
            "rule_findings": reds + [f.as_dict() for f in findings if f.severity == "warn"],
            "red_count": len(reds),
            "judge": verdicts,
            "label": label,
        })
    return out


def to_baseline_row(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": r["id"], "code": r["code"], "answer": r["answer"],
            "digest": [{"name": m.get("name"), "value": m.get("value")} for m in r["digest"]]}


def is_fail(r: dict[str, Any]) -> bool:
    return bool(r["expect_mismatch"]) or r["red_count"] > 0 \
        or (r.get("judge") or {}).get("rule_verdict") == "fail"


def build_report(gate_res: dict[str, Any], gate_errs: list[str], results: list[dict[str, Any]],
                 diff_res: dict[str, Any], known: dict[str, str], judge_mode: str) -> dict[str, Any]:
    cells = [r for r in results if r.get("domain")]
    return {
        "generated_on": date.today().isoformat(),
        "counts": {
            "cases": len(results), "matrix_cells": len(cells),
            "failing": sum(1 for r in results if is_fail(r)),
            "clean": sum(1 for r in results if not is_fail(r)),
        },
        "gates": {"ok": not gate_errs, "detail": gate_res, "failures": gate_errs},
        "matrix": matrix.summarize(cells),
        "classes": classify.summarize([r["label"] for r in results]),
        "judge": {
            "mode": judge_mode,
            "rule_fails": [r["id"] for r in results
                           if (r.get("judge") or {}).get("rule_verdict") == "fail"],
            "not_evaluated": (results[0]["judge"]["not_evaluated"] if results else []),
        },
        "regression": {k: v for k, v in diff_res.items() if k != "changed"} | {
            # 变化明细要留在报告里（前 10 条，够定位），⛔ 只报个数量——
            # "哪里变了"正是差集报告唯一有价值的部分
            "changed_count": len(diff_res.get("changed") or []),
            "changed_sample": (diff_res.get("changed") or [])[:10],
        },
        "known_red": known,
        "forbidden_words": list(env_contract.BANNED_PHRASES),
        "envelope_keys": list(env_contract.ENVELOPE_KEYS),
    }


def render_md(rep: dict[str, Any], results: list[dict[str, Any]]) -> str:
    c, mx = rep["counts"], rep["matrix"]
    lines = [
        f"# 评估报告（{rep['generated_on']}）", "",
        f"- 用例 **{c['cases']}** 条（其中矩阵格 {c['matrix_cells']}），"
        f"失败 **{c['failing']}**，全绿 {c['clean']}",
        f"- ⚠️ {mx['hit_rate_caveat']}",
        f"- 契约门禁 G1/G2/G3：**{'全绿' if rep['gates']['ok'] else '有红'}**"
        + ("" if rep["gates"]["ok"] else f" → {rep['gates']['failures']}"),
        f"- 覆盖率矩阵：**{mx['cells']} 格 = {mx['shape'][0]} 域 × {mx['shape'][1]} 问法**"
        f"（自动枚举，⛔ 人挑题）⇒ 命中 {mx['hits']}／命中率 **{mx['hit_rate']:.2%}**",
        f"- **多余技能率 {mx['extra_skill_rate']}**（每格平均多拉起 {mx['extra_skill_total'] / max(mx['cells'],1):.2f} "
        f"个用不到的技能 ⇒ 上下文成本，⛔ 只看命中率）",
        f"- judge 模式 **{rep['judge']['mode']}**；未评项：{('、'.join(rep['judge']['not_evaluated'])) if rep['judge']['not_evaluated'] else '无'}",
        f"- 回归差集：基线已建立={rep['regression']['baseline_established']}，"
        f"变化 {rep['regression']['changed_count']}，**未登记新红 {len(rep['regression']['new_red'])}**", "",
        "## 失败归类", "", "| 类别 | 数量 | 例子 |", "|---|---|---|",
    ]
    for cls, n in rep["classes"]["counts"].items():
        lines.append(f"| {cls} | {n} | {', '.join(rep['classes']['examples'].get(cls, [])) or '—'} |")
    lines += ["", "## 最差问法（先看不好的）", "", "| 问法 | 命中/格 |", "|---|---|"]
    for w in mx["worst_phrasings"]:
        lines.append(f"| {w['phrasing']} | {w['hits']}/{w['cells']} |")
    fails = [r for r in results if is_fail(r)]
    lines += ["", "## 失败用例明细", ""]
    if not fails:
        lines.append("（无）")
    for r in fails[:25]:
        why = r["expect_mismatch"] or []
        why += [f["rule"] + ": " + f["message"] for f in r["rule_findings"] if f["severity"] == "red"]
        why += [f"judge: 自造数字 {n['value']}" for n in (r["judge"].get("numbers") or [])
                if n.get("fabricated")]
        lines.append(f"- **{r['id']}**（{r['code']}）")
        for w in dict.fromkeys(why):
            lines.append(f"  - {w}")
    lines += ["", "> 本报告由 `python -m eval.run` 生成；数字全部现算自计算层，"
              "⛔ 手工填写。差集与已知红规则见 `eval/diff.py`。"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="eval.run", description="D4 评估流水线")
    ap.add_argument("--write-cases", action="store_true", help="从注册表×问法生成语料")
    ap.add_argument("--rebase", action="store_true", help="用本次结果更新基线")
    ap.add_argument("--live-judge", action="store_true", help="启用真 judge 模型（会花额度）")
    ap.add_argument("--check", action="store_true", help="PR 前检查模式：有新红就 exit 1")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试用）")
    a = ap.parse_args(argv)

    if a.write_cases:
        print(f"已生成语料 {dataset.write_cases()}（{len(dataset.build_cases())} 条）")

    try:
        gate_res = contract_gates.run_all()
        gate_errs: list[str] = []
    except Exception as exc:  # noqa: BLE001
        gate_res, gate_errs = {}, [f"{type(exc).__name__}: {exc}"]

    cases = dataset.load_cases()
    if a.limit:
        cases = cases[:a.limit]
    llm = judge.build_live_judge() if a.live_judge else None
    results = run_cases(cases, llm_judge=llm)

    known = dataset.load_known_red()
    known_problems = diff.validate_known_red(known, {c["id"] for c in cases})
    if known_problems:
        gate_errs += [f"known_red 配置有问题：{p}" for p in known_problems]

    diff_res = diff.run_diff([to_baseline_row(r) for r in results],
                             dataset.load_baseline(), known)
    if a.rebase:
        dataset.write_baseline([to_baseline_row(r) for r in results])
        print(f"基线已更新：{dataset.BASELINE_PATH}（{len(results)} 条）")
        diff_res = diff.run_diff([to_baseline_row(r) for r in results],
                                 {r["id"]: to_baseline_row(r) for r in results}, known)

    rep = build_report(gate_res, gate_errs, results, diff_res, known,
                       "rule+llm" if llm else "rule-only")
    REPORT_MD.write_text(render_md(rep, results), encoding="utf-8", newline="")
    REPORT_JSON.write_text(json.dumps({"summary": rep,
                                       "results": [{k: v for k, v in r.items() if k != "answer"}
                                                   for r in results]},
                                      ensure_ascii=False, indent=1, default=str),
                           encoding="utf-8", newline="")
    print(f"报告已写出：{REPORT_MD} / {REPORT_JSON}")
    print(f"用例 {rep['counts']['cases']} · 失败 {rep['counts']['failing']} · "
          f"矩阵 {rep['matrix']['cells']} 格 命中率 {rep['matrix']['hit_rate']} · "
          f"新红 {len(diff_res['new_red'])} · 门禁 {'OK' if not gate_errs else gate_errs}")

    if a.check:
        blockers = list(diff_res["new_red"]) + list(gate_errs)
        blockers += [r["id"] for r in results if is_fail(r) and r["id"] not in known]
        if blockers:
            print(f"[PR CHECK FAIL] {len(blockers)} 项未登记的红：{blockers[:8]}")
            return 1
        print("[PR CHECK OK] 无未登记的新红")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
