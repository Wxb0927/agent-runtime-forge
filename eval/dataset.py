"""① 测试集管理：语料、基线、已知红，三份文件各管一件事。

⚠️ **语料里⛔ 存数字**。cases 只存"这条问句应当命中哪个指标、出现哪些段名、
绝不能出现的值与禁词"；**期望数值一律在评测时由计算层现算**（见 ``system.oracle``），
否则公式一改，几百条语料集体过期，而那种过期⛔ 报错、只默默变红。

* ``cases.jsonl`` —— 语料（生成 + 少量手写负例，负例也⛔ 带数字）
* ``baseline.jsonl`` —— 上一次**人工批准**的输出，供 ④ 回归差集比对
* ``known_red.json`` —— 已登记的暂时性失败（"已知红"入册才算数，⛔ 沉默容忍）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

EVAL_DIR = Path(__file__).resolve().parent
CASES_PATH = EVAL_DIR / "cases.jsonl"
BASELINE_PATH = EVAL_DIR / "baseline.jsonl"
KNOWN_RED_PATH = EVAL_DIR / "known_red.json"

# 手写负例：每条只声明"应当发生什么"，⛔ 声明数字
EDGE_CASES: tuple[dict[str, Any], ...] = (
    {"id": "edge-vague-all-skills", "mode": "nl",
     "input": {"query": "PRJ-1180 现在到底什么情况"},
     "expect": {"code": "OK", "metric_present": "completion_rate"}},
    {"id": "edge-out-of-scope", "mode": "nl",
     "input": {"query": "PRJ-1007 的完成率", "user_key": "u0041"},
     "expect": {"code": "NOT_IN_SCOPE", "digest_empty": True}},
    {"id": "edge-no-permission-floor", "mode": "nl",
     "input": {"query": "我的项目完成率", "user_key": "u9999"},
     "expect": {"code": "OK"}},
    {"id": "edge-intent-typo", "mode": "intent",
     "input": {"intent": {"metric": "conplte_rate", "topK": 999}},
     "expect": {"code": "BAD_INTENT", "digest_empty": True,
                "note_has": ["CLAMPED", "heal:"]}},
    {"id": "edge-intent-unknown-key", "mode": "intent",
     "input": {"intent": {"metric": "completion_rate", "engine": "openai"}},
     "expect": {"code": "OK", "note_has": ["DROPPED:engine"]}},
    {"id": "edge-intent-bad-date", "mode": "intent",
     "input": {"intent": {"metric": "completion_rate"}, "host": {"as_of": "20261301"}},
     "expect": {"code": "BAD_HOST_ARG", "digest_empty": True}},
)


def _case_from_matrix(row: dict[str, str]) -> dict[str, Any]:
    return {
        "id": f"{row['domain']}__{row['phrasing']}",
        "mode": "nl",
        "input": {"query": row["query"]},
        "expect": {"code": "OK", "metric_present": row["domain"]},
        "domain": row["domain"], "phrasing": row["phrasing"],
    }


def build_cases() -> list[dict[str, Any]]:
    from eval import phrasings

    return [_case_from_matrix(r) for r in phrasings.build_queries()] + [dict(c) for c in EDGE_CASES]


def write_cases() -> Path:
    CASES_PATH.write_text("".join(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n"
                                  for c in build_cases()), encoding="utf-8", newline="")
    return CASES_PATH


def load_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"缺 {path}：先跑 `python -m eval.run --write-cases`")
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    seen: set[str] = set()
    for r in rows:
        if r["id"] in seen:
            raise AssertionError(f"语料 id 重复：{r['id']}（重复 id 会让差集比对张冠李戴）")
        seen.add(r["id"])
        for k in ("id", "mode", "input", "expect"):
            if k not in r:
                raise AssertionError(f"语料 {r.get('id')} 缺字段 {k}")
    return rows


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    return {r["id"]: r for r in (json.loads(l) for l in
                                 path.read_text(encoding="utf-8").splitlines() if l.strip())}


def write_baseline(rows: Iterable[dict[str, Any]], path: Path = BASELINE_PATH) -> Path:
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                            for r in sorted(rows, key=lambda x: x["id"])),
                    encoding="utf-8", newline="")
    return path


def load_known_red(path: Path = KNOWN_RED_PATH) -> dict[str, str]:
    """已登记的暂时失败：``{case_id: 为什么暂时容忍}``。⛔ 未登记的红可以沉默存在。"""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))
