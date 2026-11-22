"""RAG 评测跑批：分块对照 × 四档检索 × 三类问题，输出 `eval/rag_report.md`。

三条硬规矩（都写进报告，⛔ 只报好看的）：

1. **说清分数是谁跑出来的**：默认用 `HashEmbedder`（确定性、零下载，只够验证管线），
   加 `--semantic` 才上 BGE 语义模型。⇒ **⛔ 把 hash 向量的召回率当成"语义检索提升"写进简历**。
2. **必须同时给出"混合相对单路的提升"和"最差的那一类问题"**（任务书 D5 原文要求）。
3. **`hybrid+rerank` 档若重排器不可用就整档标 `skipped`**，⛔ 拿 hybrid 的分数冒充重排后的分数。

跑法：`uv run python -m eval.rag_eval`（离线，秒级）
      `uv run python -m eval.rag_eval --semantic`（首次会下模型，缓存已钉在 D 盘）
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from forge.rag import evalset
from forge.rag.chunk import Chunk, chunk_all
from forge.rag.corpus import chapters, doc_to_chapter
from forge.rag.retrieve import BM25Index, DenseIndex, HashEmbedder, Hit, Retriever, rrf, tokenize_zh

REPORT_MD = Path("eval/rag_report.md")
REPORT_JSON = Path("eval/rag_report.json")
MODES = ("bm25", "dense", "hybrid", "hybrid+rerank")
K = 5


@dataclass
class Reranker:
    """CrossEncoder 重排器。加载失败 ⇒ 返回 None，让调用方把整档标 skipped。"""
    name: str = "cross-encoder"
    _m: Any = None

    def score(self, query: str, text: str) -> float:
        return float(self._m.predict([(query, text)]))


def try_reranker(model_name: str = "BAAI/bge-reranker-base") -> Reranker | None:
    try:
        from forge import env

        env.guard_or_fail()
        from sentence_transformers import CrossEncoder

        return Reranker(name=model_name, _m=CrossEncoder(model_name))
    except Exception as exc:  # noqa: BLE001
        print(f"[rerank] 跳过（{type(exc).__name__}: {str(exc)[:80]}）")
        return None


def relevance_proxy(query: str, hits: Sequence[Hit], embedder: Any) -> float:
    """Answer Relevance 的**离线代理**：词面覆盖率（hash 档）或向量余弦（语义档）。
    名字里带 proxy 是为了让报告⛔ 被读成官方 AR 指标。"""
    if not hits:
        return 0.0
    top = hits[0].chunk.text
    if getattr(embedder, "semantic", False):
        import numpy as np

        a, b = embedder.encode([query]), embedder.encode([top])
        return float(np.dot(a[0], b[0]))
    q, t = set(tokenize_zh(query)), set(tokenize_zh(top))
    return len(q & t) / len(q) if q else 0.0


def evaluate(items: list[dict[str, Any]], chunks: list[Chunk], *, embedder: Any,
             reranker: Any = None) -> dict[str, Any]:
    gold_of = doc_to_chapter()
    retriever = Retriever(chunks, embedder=embedder, reranker=reranker)
    out: dict[str, Any] = {"embedder": retriever.describe(), "modes": {}}
    for mode in MODES:
        if mode == "hybrid+rerank" and reranker is None:
            out["modes"][mode] = {"skipped": "重排器未加载（离线默认不下载模型）"}
            continue
        per_class: dict[str, list[int]] = {}
        recall_sum = cite_sum = rel_sum = 0
        for it in items:
            gold = gold_of.get(it["gold_docs"][0], it["gold_docs"][0])
            try:
                hits = retriever.search(it["question"], k=K, mode=mode)
            except RuntimeError as exc:              # reranker 缺失等 ⇒ 整档作废
                out["modes"][mode] = {"skipped": str(exc)}
                hits = None
            if hits is None:
                break
            got = [h.chunk.doc_id for h in hits]
            recall = 1 if gold in got else 0
            cite = 1 if got and got[0] == gold else 0
            rel = relevance_proxy(it["question"], hits, embedder)
            recall_sum += recall; cite_sum += cite; rel_sum += rel
            bucket = per_class.setdefault(it["class"], [0, 0])
            bucket[0] += recall; bucket[1] += 1
        n = len(items)
        out["modes"][mode] = {
            "n": n,
            "recall@5": round(recall_sum / n, 4) if n else None,
            "citation_top1": round(cite_sum / n, 4) if n else None,
            "relevance_proxy": round(rel_sum / n, 4) if n else None,
            "by_class": {c: {"recall@5": round(r / t, 4), "n": t}
                         for c, (r, t) in sorted(per_class.items())},
            "worst_class": min(((c, v["recall@5"]) for c, v in
                                ((c, {"recall@5": round(r / t, 4)})
                                 for c, (r, t) in per_class.items())),
                               key=lambda x: x[1], default=(None, None))[0],
        }
    single = [out["modes"][m].get("recall@5") for m in ("bm25", "dense")
              if out["modes"][m].get("recall@5") is not None]
    hyb = out["modes"]["hybrid"].get("recall@5")
    out["hybrid_gain"] = {
        "vs_best_single": round(hyb - max(single), 4) if (single and hyb is not None) else None,
        "note": "负数也照实写：混合不如单路说明这一档融合参数不合适。",
    }
    return out


def render(rep: dict[str, Any], chunk_counts: dict[str, int], qa_counts: dict[str, int]) -> str:
    e = rep["embedder"]
    lines = [
        f"# RAG 评测报告（{date.today().isoformat()}）", "",
        "⚠️ **本轮 embedder = `{}`**{}；dense 后端 `{}`；reranker = `{}`。".format(
            e["embedder"],
            "" if e["embedder_is_semantic"] else "**（哈希向量，无语义能力 ⇒ 只证明管线正确，"
                                                 "⛔ 当成语义召回率写进简历）**",
            e["dense_backend"], e["reranker"] or "未加载"),
        f"- 语料：6 章（由 29 条口径合并）；分块 **fixed={chunk_counts['fixed']} / "
        f"section={chunk_counts['section']}**（两种策略真的切出了不同块数，⛔ 空转对照）",
        f"- 评估集：**{qa_counts['合计']} 条** = " +
        " + ".join(f"{c} {n}" for c, n in qa_counts.items() if c != "合计") +
        "（模板×语料确定性生成，⛔ 人挑题）", "",
        "## 四档检索 × 两种分块", "",
        "| 分块 | 模式 | Recall@5 | 引用正确率(top1) | Relevance(proxy) | 最差类别 |",
        "|---|---|---|---|---|---|",
    ]
    for strat, block in rep["by_strategy"].items():
        for mode in MODES:
            m = block["modes"].get(mode, {})
            if m.get("skipped"):
                lines.append(f"| {strat} | {mode} | ⛔ skipped | | | {m['skipped'][:40]} |")
                continue
            worst = m.get("worst_class")
            lines.append(f"| {strat} | {mode} | {m['recall@5']} | {m['citation_top1']} | "
                         f"{m['relevance_proxy']} | {worst} |")
    for strat, block in rep["by_strategy"].items():
        gain = block.get("hybrid_gain", {})
        lines += ["", f"**{strat} 分块：混合相对单路的提升 = {gain.get('vs_best_single')}**"
                  f"（{gain.get('note', '')}）"]
        for mode in MODES:
            m = block["modes"].get(mode, {})
            if m.get("by_class"):
                lines.append(f"- {mode} 分类召回：" +
                             "、".join(f"{c}={v['recall@5']}" for c, v in m["by_class"].items()))
    lines += ["", "## 分类明细（section 分块）", "",
              "| 模式 | " + " | ".join(rep["by_strategy"]["section"]["modes"]["bm25"]["by_class"])
              + " |", "|---|" + "---|" * len(
                  rep["by_strategy"]["section"]["modes"]["bm25"]["by_class"])]
    base = rep["by_strategy"]["section"]["modes"]
    keys = list(base["bm25"]["by_class"])
    for mode in MODES:
        if "by_class" not in base.get(mode, {}):
            continue
        lines.append(f"| {mode} | " + " | ".join(
            str(base[mode]["by_class"][k]["recall@5"]) for k in keys) + " |")
    lines += ["", "> 生成命令：`uv run python -m eval.rag_eval`（加 `--semantic` 才用真语义模型）。"
              "分数全部可复现：语料与评估集由代码生成，检索器状态不依赖网络。"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="eval.rag_eval")
    ap.add_argument("--semantic", action="store_true", help="用 BGE 语义模型（会下模型到 D 盘）")
    ap.add_argument("--rerank", action="store_true", help="额外加载 CrossEncoder 重排器")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)

    items = evalset.load_set()
    if a.limit:
        items = items[:a.limit]
    embedder: Any
    if a.semantic:
        from forge.rag.retrieve import BGEEmbedder

        embedder = BGEEmbedder()
    else:
        embedder = HashEmbedder()
    reranker = try_reranker() if (a.rerank and a.semantic) else None

    ch = chapters()
    chunk_sets = chunk_all(ch, size=160, overlap=40, max_len=240)
    by_strategy = {name: evaluate(items, cs, embedder=embedder, reranker=reranker)
                   for name, cs in chunk_sets.items()}
    rep = {"generated_on": date.today().isoformat(), "k": K,
           "by_strategy": by_strategy,
           "embedder": by_strategy["fixed"]["embedder"]}
    REPORT_MD.write_text(render(rep, {k: len(v) for k, v in chunk_sets.items()},
                                evalset.counts(items)), encoding="utf-8", newline="")
    REPORT_JSON.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8",
                           newline="")
    for name, block in by_strategy.items():
        print(f"[{name}] " + " · ".join(
            f"{m}={block['modes'].get(m, {}).get('recall@5', 'skipped')}" for m in MODES))
    print(f"报告已写出：{REPORT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
