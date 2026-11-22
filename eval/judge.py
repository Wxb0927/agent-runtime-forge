"""③ LLM-as-Judge：判"答案是否只复述、有无自造数字"，并**要求给出引用行号**。

⚠️ 一个必须说清的分工（也是我把它写成两段的原因）：

* **"数字有没有出处"这条不需要 LLM** —— 把工具返回值编号成行，逐个数字回查即可，
  确定性、零成本、可进 CI。所以 ``cite_numbers()`` 是**规则实现**，报告里标 ``mode=rule``。
* **"是不是只复述""有没有编造因果"这类语义判断才交给 judge 模型**，
  且⛔ 没跑真模型时假装跑过：``--live-judge`` 缺省时，报告里明确写
  ``llm=skipped`` + 未评项清单。**"打桩结论冒充模型结论"是这类流水线最常见的自欺**。

judge 模型被要求输出 JSON：``{"verdict":"pass|fail","numbers":[{"value":..,"line":..}],
"reasons":[..]}``，且**必须给行号**——⛔ 行号"我觉得没问题"。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

NUM_RE = re.compile(r"\d+(?:\.\d+)?%|¥-?\d{1,3}(?:,\d{3})*\.\d{2}|\d{4}-\d{2}-\d{2}"
                    r"|\d+(?:\.\d+)?\s*天|\d+/\d+")

JUDGE_SYSTEM = """你是输出审计员。给你两段材料：
【工具返回值】（每行有序号）与【助手答案】。
你的任务只有两个：
1) 答案里出现的每一个数字，都必须能在【工具返回值】里找到同一行原文；给出该行序号。
   找不到 ⇒ fabricated=true。
2) 判断答案是否**只复述**：⛔ 自己加总、换算、外推、补因果、把无数据说成 0。
⛔ 输出评价性赞美，⛔ 输出没有行号依据的结论。只输出 JSON：
{"verdict":"pass|fail","numbers":[{"value":"...","line":3,"fabricated":false}],
 "self_computed":["..."],"fabricated":["..."],"reasons":["..."]}"""


def numbered_digest(digest: list[dict[str, Any]], *, host: str = "") -> list[str]:
    """把工具返回值编号成行，供引用（行号即出处）。

    第 1 行固定是**宿主注入项**：答案里的基准日与档位来自这里，⛔ 把它当"自造数字"，
    也⛔ 为了过关就把日期从核验里排除——那等于给核验开了个洞。
    """
    lines: list[str] = []
    if host:
        lines.append(f"HOST | {host}")
    for m in digest:
        parts = [m.get("name", "?"), str(m.get("value"))]
        if m.get("numerator") is not None and m.get("denominator") is not None:
            parts.append(f"{m['numerator']:,}/{m['denominator']:,}")
        if m.get("as_of"):
            parts.append(f"as_of={m['as_of']}")
        if m.get("note"):
            parts.append(f"note={m['note']}")
        lines.append(" | ".join(parts))
    return lines


def cite_numbers(answer: str, digest_lines: list[str]) -> list[dict[str, Any]]:
    """规则版引用核验：答案里的每个数字都要能在某一行原文里找到。"""
    out: list[dict[str, Any]] = []
    for token in NUM_RE.findall(answer):
        hits = [i + 1 for i, ln in enumerate(digest_lines) if token in ln]
        out.append({"value": token, "line": hits[0] if hits else None,
                    "candidates": hits, "fabricated": not hits})
    return out


def judge_case(case: dict[str, Any], run: Any, *, llm: Optional[Any] = None) -> dict[str, Any]:
    digest_lines = numbered_digest(run.envelope.get("digest") or [],
                                   host=f"user_key={run.user_key} | as_of={run.as_of}")
    numbers = cite_numbers(run.envelope.get("answer") or "", digest_lines)
    result: dict[str, Any] = {
        "id": case["id"],
        "mode": "rule",
        "rule_verdict": "fail" if any(n["fabricated"] for n in numbers) else "pass",
        "numbers": numbers,
        "llm": None,
        "not_evaluated": [],
    }
    if llm is None:
        result["llm"] = "skipped"
        result["not_evaluated"] = ["是否只复述（有无加总/换算/外推）", "是否编造因果", "是否答非所问"]
        return result

    numbered = "\n".join(f"{i + 1}. {ln}" for i, ln in enumerate(digest_lines)) or "(本次无工具返回值)"
    prompt = f"【工具返回值】\n{numbered}\n\n【助手答案】\n{run.envelope.get('answer','')}"
    raw = llm.invoke([{"role": "system", "content": JUDGE_SYSTEM},
                      {"role": "user", "content": prompt}]).content
    parsed = _parse_json(raw)
    result["mode"] = "rule+llm"
    result["llm"] = parsed if parsed is not None else {"error": "judge 未返回合法 JSON", "raw": str(raw)[:300]}
    if parsed and parsed.get("verdict") == "fail":
        result["rule_verdict"] = "fail"
        result["llm_fabricated"] = parsed.get("fabricated") or []
        result["llm_self_computed"] = parsed.get("self_computed") or []
    return result


def _parse_json(text: Any) -> Optional[dict[str, Any]]:
    if isinstance(text, dict):
        return text
    m = re.search(r"\{.*\}", str(text), re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def build_live_judge(model: Optional[str] = None) -> Any:
    from forge.model.factory import build_live_model

    return build_live_model(model=model)
