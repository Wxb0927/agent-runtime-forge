# -*- coding: utf-8 -*-
"""D7 验收项之一：README 的数字必须与 `eval/` 报告**一致**（双向校验）。

任务书原话：「README 指标数字与 `eval/` 报告一致（⛔ 不许手写不同步的数字）」。
靠自觉一定会分叉（本项目今天就出现过 `361` 与 `327` 并存的残留），所以做成检查器：

* **正向**：从真源（`eval/latency.md`、`eval/rag_report_semantic.md`、`eval/qa_set.jsonl`、
  `eval/report.json`、`mutation_check.py` 的清单、pytest 收集数）**读出**当前值；
* **反向**：断言 README 里**出现的就是这些值**，并额外扫一遍"曾经出现过、现已作废"的旧值；
* 任一不符 ⇒ `exit 1`（可直接挂进 PR 前检查）。

跑法：uv run python tools/d7_metrics_sync.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"

# 曾经出现、现已作废的数字：残留即说明有地方没同步（今天真发生过）
STALE = ["361", "0.9391", "0.7784", "0.8333", "78.2 ms", "171 passed", "153 passed", "34/34"]


def truth() -> dict[str, str]:
    """从各真源读当前值（⛔ 在 README 里抄一份当事实）。"""
    out: dict[str, str] = {}

    # eval/latency.md 是一张三列对照表，按行解析（⛔ 用整篇正则赌格式——
    # 上一版就是格式没对上，把"读不到"当成了"README 不一致"）
    for ln in (REPO / "eval/latency.md").read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) == 6 and cells[0].startswith("compose"):
            nums = [re.sub(r"[^\d.]", "", c) for c in cells[2:6]]
            out["p50"], out["p95"], out["p99"], out["cold_p95"] = nums
        elif len(cells) == 6 and cells[0].startswith("本机"):
            out["p95_local"] = re.sub(r"[^\d.]", "", cells[3])

    sem = (REPO / "eval/rag_report_semantic.md").read_text(encoding="utf-8")

    def cell(strategy: str, mode: str, col: int) -> str | None:
        for ln in sem.splitlines():
            parts = [p.strip() for p in ln.strip("|").split("|")]
            if len(parts) >= 5 and parts[0] == strategy and parts[1] == mode:
                return parts[col]
        return None

    for name, key in ((("section", "bm25", 3), "top1_bm25"), (("section", "dense", 3), "top1_dense"),
                      (("section", "hybrid+rerank", 2), "recall_rerank"),
                      (("section", "hybrid+rerank", 3), "top1_rerank"),
                      (("section", "bm25", 2), "recall_bm25"),
                      (("section", "hybrid", 2), "recall_hybrid")):
        v = cell(*name)
        if v:
            out[key] = v

    out["qa_total"] = str(len([l for l in
                                (REPO / "eval/qa_set.jsonl").read_text(encoding="utf-8").splitlines()
                                if l.strip()]))

    report = json.loads((REPO / "eval/report.json").read_text(encoding="utf-8"))
    out["matrix_cells"] = str(report["summary"]["matrix"]["cells"])

    sys.path.insert(0, str(REPO))
    from tools import mutation_check as MC  # noqa: E402

    out["mutations"] = f"{len(MC.ALL_MUTATIONS)}/{len(MC.ALL_MUTATIONS)}"
    return out


def test_count() -> int:
    r = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q",
                        "-o", "addopts=--basetemp=.tmp/pytest"],
                       cwd=REPO, capture_output=True, text=True, timeout=900)
    m = re.findall(r"(\d+) tests? collected", (r.stdout or "") + (r.stderr or ""))
    return int(m[-1]) if m else -1


def main() -> int:
    t = truth()
    readme = README.read_text(encoding="utf-8")
    problems: list[str] = []

    required = {
        "P95 容器": t.get("p95"), "P50 容器": t.get("p50"), "P99 容器": t.get("p99"),
        "冷启动 P95": t.get("cold_p95"),
        "top1 bm25": t.get("top1_bm25"), "top1 dense": t.get("top1_dense"),
        "rerank Recall": t.get("recall_rerank"), "rerank top1": t.get("top1_rerank"),
        "本机 P95": t.get("p95_local"),
        "QA 条数": t.get("qa_total"), "矩阵格数": t.get("matrix_cells"),
        "变异条数": t.get("mutations"),
    }
    for label, value in required.items():
        if not value:
            problems.append(f"{label}：真源里读不到值（报告结构变了？检查器要同步改）")
            continue
        if value not in readme:
            problems.append(f"{label}：README 缺少真源值 {value}")

    n_tests = test_count()
    if n_tests > 0 and f"{n_tests} passed" not in readme:
        problems.append(f"测试数：README 没写 {n_tests} passed（当前收集到 {n_tests}）")

    HISTORY_WORDS = ("作废", "旧", "曾", "第一版", "已改", "上一轮", "曾经", "当时")
    for stale in STALE:
        for lineno, line in enumerate(readme.splitlines(), 1):
            if stale in line and not any(w in line for w in HISTORY_WORDS):
                problems.append(f"残留已作废的数字 {stale}（README:{lineno}，且不在历史语境里）")
                break

    print("真源读到的当前值：")
    for k, v in sorted(t.items()):
        print(f"  {k:14s} {v}")
    print(f"  pytest collected {n_tests}")
    if problems:
        print("\n[FAIL] README 与 eval/ 报告不一致：")
        for p in problems:
            print("  - " + p)
        return 1
    print("\n[OK] README 指标与 eval/ 报告一致，且无作废数字残留")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
