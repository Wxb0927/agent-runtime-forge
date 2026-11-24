"""300 条业务 QA 评估集：**模板 × 语料确定性生成**，⛔ 手抄、⛔ 事后挑题。

三条纪律：

1. **每题带 gold 文档 id** ⇒ `Recall@5` 与"正确引用率"才可判定；
   ⛔ "人工看一遍觉得能答" 那种评估集（人挑验收集必然偏向"我已经想到的说法"，生产上付过代价）。
2. **三类问题按类计数**（定义 / 流程 / 异常处理），报告里必须单独给出
   **最差的那一类**——平均值会掩盖异常处理类答不上来这件事。
3. 条数以实际生成为准并写进报告，⛔ 为了对齐"300"这个数字偷偷删题或凑题。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from forge.rag.corpus import Doc, corpus

OUT_PATH = Path("eval") / "qa_set.jsonl"

# 三类问法。⚠️ 模板里的每个占位符都必须能由语料自身填上，⛔ 引入语料外的知识。
DEFINITION_TPL = (
    "{t}的口径是什么",
    "{t}具体指什么",
    "{t}怎么定义",
    "{t}包含哪些内容",
)
PROCESS_TPL = (
    "{t}要走哪些步骤",
    "{t}的模板长什么样",
    "{t}按什么顺序执行",
    "{t}需要哪些前置条件",
)
EXCEPTION_TPL = (
    "{t}时该怎么处理",
    "遇到{t}要怎么办",
    "{t}的情况下结果应该是什么",
    "为什么{t}不能直接算成 0",
)


#: 枚举值 → 中文可读说法。⚠️ 第一版直接把 `material`、`cpi` 这类原始枚举塞进中文问句
#: （生成出 "material时该怎么处理" 这种不像人话的题），题目本身就退化，
#: 再拿它去算 Recall 就是在评一个不存在的场景。
LABELS: dict[str, str] = {
    "material": "材料费", "labor": "人工费", "manufacturing": "制造费用", "misc": "其他费用",
    "cpi": "成本绩效指数", "eac": "完工成本估算", "phase": "阶段", "milestone": "里程碑",
    "operation": "工序", "task": "任务", "advance": "预付款", "delivery": "到货款",
    "acceptance": "验收款", "retention": "质保金", "bu-alpha": "甲业务线",
    "bu-beta": "乙业务线", "bu-gamma": "丙业务线", "bu-delta": "丁业务线",
}


def _zh(term: str) -> str:
    """⛔ 纯 ASCII 枚举值出现在中文问句里（先翻译，翻不了就弃用该词）。"""
    if term in LABELS:
        return LABELS[term]
    return term if re.search(r"[一-鿿]", term) else ""


def _terms(d: Doc) -> list[str]:
    """题目用词全部来自语料自身：标题去掉前缀 + 标签（一律过一遍 `_zh`）。"""
    head = d.title.split(" · ")[-1].split("：")[-1].strip()
    raw = [head] + [t for t in d.tags if t and t not in ("口径", "模板", "异常处理")]
    out = [t for t in (_zh(x) for x in raw) if t]
    seen: list[str] = []
    for t in out:
        if t not in seen:
            seen.append(t)
    return seen


def build(docs: list[Doc] | None = None) -> list[dict[str, Any]]:
    docs = docs or corpus()
    items: list[dict[str, Any]] = []
    for d in docs:
        classes = (("定义类", DEFINITION_TPL) if d.kind == "rule" else ("流程类", PROCESS_TPL))
        # 每条文档都出三类题：规则库考"口径定义 + 异常处理"，模板库考"流程 + 异常处理"
        for cls_name, tpls in (classes, ("异常处理类", EXCEPTION_TPL)):
            for tpl in tpls:
                for term in _terms(d)[:2]:          # 每条文档每题类取 2 个代表词，控制规模
                    items.append({
                        "id": f"qa-{len(items) + 1:04d}",
                        "class": cls_name,
                        "question": tpl.format(t=term),
                        "gold_docs": [d.id],
                        "gold_chunks_prefix": f"{d.id}#",
                        "kind": d.kind,
                    })
        if d.kind == "rule":                        # 规则库额外补流程类（口径怎么落到操作上）
            for tpl in PROCESS_TPL[:1]:
                term = _terms(d)[0]
                items.append({"id": f"qa-{len(items) + 1:04d}", "class": "流程类",
                              "question": tpl.format(t=term), "gold_docs": [d.id],
                              "gold_chunks_prefix": f"{d.id}#", "kind": d.kind})
    # 去重：同问句只留一条（模板交叉会撞车，留着会让"300 条"里掺重复题）
    uniq: dict[str, dict[str, Any]] = {}
    for it in items:
        uniq.setdefault(it["question"], it)
    out = list(uniq.values())
    for n, it in enumerate(out, 1):
        it["id"] = f"qa-{n:04d}"
    return out


def counts(items: list[dict[str, Any]]) -> dict[str, int]:
    c: dict[str, int] = {}
    for it in items:
        c[it["class"]] = c.get(it["class"], 0) + 1
    c["合计"] = len(items)
    return c


def write_set(path: Path = OUT_PATH) -> Path:
    items = build()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items),
                    encoding="utf-8", newline="")
    return path


def load_set(path: Path = OUT_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        write_set(path)
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    gold = {d.id for d in corpus()}
    bad = [r["id"] for r in rows if not set(r["gold_docs"]) <= gold]
    if bad:
        raise AssertionError(f"这些 QA 的 gold 文档不在语料里（题目悬空）：{bad[:5]}")
    dup = len(rows) - len({r["question"] for r in rows})
    if dup:
        raise AssertionError(f"评估集里有 {dup} 条重复问句")
    return rows


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):   # 中文 Windows 控制台默认 cp936：
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")   # 打 ⛔/✅ 会 UnicodeEncodeError
    p = write_set()
    items = load_set(p)
    print(f"已写出 {p}：{counts(items)}")
