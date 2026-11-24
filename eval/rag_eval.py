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
from forge.rag.corpus import chapters, corpus, doc_to_chapter
from forge.rag.retrieve import BM25Index, DenseIndex, HashEmbedder, Hit, Retriever, rrf, tokenize_zh

REPORT_MD = Path("eval/rag_report.md")
REPORT_JSON = Path("eval/rag_report.json")
MODES = ("bm25", "dense", "hybrid", "hybrid+rerank")
K = 5


@dataclass
class Reranker:
    """CrossEncoder 重排器。

    ⚠️ `predict` 返回 **ndarray**（实测 `float(array([0.12]))` 抛
    "only 0-dimensional arrays can be converted to Python scalars"），
    所以取值要显式降成一维再取第 0 个；批量打分用 `rank_batch`，
    ⛔ 一条条 predict（327 题 × 15 候选 ≈ 5 千次单对推理，白慢一倍）。
    """
    name: str = "cross-encoder"
    _m: Any = None

    @staticmethod
    def _to_list(out: Any) -> list[float]:
        import numpy as np

        return [float(x) for x in np.asarray(out).reshape(-1)]

    def score(self, query: str, text: str) -> float:
        return self._to_list(self._m.predict([(query, text)]))[0]

    def rank_batch(self, query: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        return self._to_list(self._m.predict([(query, t) for t in texts]))


def try_reranker(model_name: str = "BAAI/bge-reranker-base") -> Reranker | None:
    """加载重排器。

    ⚠️ **只有"依赖真的没装"才允许当成 skipped**。第一版用一个 `except Exception`
    把 ImportError 与我自己的代码 bug 混成一类，结果 `float(ndarray)` 的 TypeError
    被报成"重排器未加载"、进程 rc=0 —— 那是评测流水线最危险的自欺：
    评审问"你怎么保证你的评测没在悄悄跳过东西"，我答不上来。
    """
    try:
        from forge import env

        env.guard_or_fail()
        from sentence_transformers import CrossEncoder
    except ImportError as exc:                     # 依赖缺失 ⇒ 合法跳过
        print(f"[rerank] skipped（依赖缺失：{type(exc).__name__}: {str(exc)[:80]}）")
        return None

    m = CrossEncoder(model_name)                   # ⛔ 兜住：模型下载/加载失败必须炸出来
    return Reranker(name=model_name, _m=m)


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
             reranker: Any = None, merged: bool = False) -> dict[str, Any]:
    gold_of = doc_to_chapter() if merged else {}
    retriever = Retriever(chunks, embedder=embedder, reranker=reranker)
    out: dict[str, Any] = {"embedder": retriever.describe(), "modes": {}}
    # ⚠️ 语料只有几章时 Recall@5 会**饱和**：随机选 5 块的命中率就有 chance_recall。
    #    不把基线印在报告里，"0.98" 看着像成绩，其实是分母太小。
    n_docs = len({c.doc_id for c in chunks})
    out["chance"] = {"n_docs": n_docs,
                     "recall@5": round(min(K / max(n_docs, 1), 1.0), 4),
                     "recall@1": round(1.0 / max(n_docs, 1), 4)}
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


def render(rep: dict[str, Any], chunk_counts: dict[str, int], qa_counts: dict[str, int],
         merged_note: bool = False) -> str:
    e = rep["embedder"]
    lines = [
        f"# RAG 评测报告（{date.today().isoformat()}）", "",
        "⚠️ **本轮配置**：embedder = `{}`{}；dense 后端 `{}`；BM25 实现 `{}`；"
        "**分词器 `{}`**；reranker = `{}`。".format(
            e["embedder"],
            "" if e["embedder_is_semantic"] else "**（哈希向量，无语义能力 ⇒ 只证明管线正确，"
                                                 "⛔ 当成语义召回率写进简历）**",
            e["dense_backend"], e.get("bm25_impl", "?"), e.get("tokenizer", "?"),
            e["reranker"] or "未加载"),
        "📌 **换分词器或换 embedder，分数就会变**（实测：装上 jieba 后 BM25 从 0.8698 → 0.9806）"
        "⇒ 每个数字都必须连着上面这行配置一起看，⛔ 裸报一个 Recall。",
        f"- 检索单位：{rep['by_strategy']['section']['chance']['n_docs']} 个"
        f"（{'合并后的章节档，⛔ 当主结论——候选太少指标会饱和' if merged_note else '原口径条目档，主结论用这档'}）；"
        f"分块 **fixed={chunk_counts['fixed']} / "
        f"section={chunk_counts['section']}**（两种策略真的切出了不同块数，⛔ 空转对照）",
        f"- 评估集：**{qa_counts['合计']} 条** = " +
        " + ".join(f"{c} {n}" for c, n in qa_counts.items() if c != "合计") +
        "（模板×语料确定性生成，⛔ 人挑题）", "",
        "## 四档检索 × 两种分块", "",
        f"📌 **随机基线**：语料只有 {rep['by_strategy']['section']['chance']['n_docs']} 章 ⇒ "
        f"瞎选的 Recall@5 ≈ **{rep['by_strategy']['section']['chance']['recall@5']}**、"
        f"Recall@1 ≈ **{rep['by_strategy']['section']['chance']['recall@1']}**。"
        "**所以 Recall@5 一看就高，⛔ 当成绩；有区分度的是「引用正确率(top1)」这一列。**", "",
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
    ap.add_argument("--merged", action="store_true",
                    help="用合并后的 6 章做检索单位（对照档；⛔ 当主结论——候选只有 6 个，指标会饱和）")
    ap.add_argument("--tag", default="",
                    help="报告后缀：'' = 默认档（哈希向量，只证明管线通）；"
                         "semantic = 语义档（才是能写进简历的质量结论）。两份并存，⛔ 互相覆盖")
    a = ap.parse_args(argv)
    from forge import env as _env

    _env.configure_caches()   # 必须在任何库(import jieba 等)落盘之前调，否则临时文件仍走 C 盘
    global REPORT_MD, REPORT_JSON
    suffix = f"_{a.tag}" if a.tag else ""
    REPORT_MD = Path(f"eval/rag_report{suffix}.md")
    REPORT_JSON = Path(f"eval/rag_report{suffix}.json")

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

    # ⚠️ 检索与打分单位用 **29 条口径**，⛔ 用合并后的 6 章：
    #    6 个候选时瞎猜 Recall@5 就有 0.833，指标饱和到看不出任何差异（今天就是这么被骗过一次，
    #    还差点把"重排器让效果下降"当成结论写进简历）。章节合并档留给 --merged 做对照。
    unit_docs = chapters() if a.merged else corpus()
    chunk_sets = chunk_all(unit_docs, size=110 if a.merged else 120, overlap=30, max_len=170)
    by_strategy = {name: evaluate(items, cs, embedder=embedder, reranker=reranker,
                                  merged=a.merged)
                   for name, cs in chunk_sets.items()}
    rep = {"generated_on": date.today().isoformat(), "k": K,
           "by_strategy": by_strategy,
           "embedder": by_strategy["fixed"]["embedder"]}
    REPORT_MD.write_text(render(rep, {k: len(v) for k, v in chunk_sets.items()},
                                evalset.counts(items), merged_note=a.merged),
                       encoding="utf-8", newline="")
    REPORT_JSON.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8",
                           newline="")
    for name, block in by_strategy.items():
        print(f"[{name}] " + " · ".join(
            f"{m}={block['modes'].get(m, {}).get('recall@5', 'skipped')}" for m in MODES))
    print(f"报告已写出：{REPORT_MD}")
    return 0


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):   # 中文 Windows 控制台默认 cp936：
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")   # 打 ⛔/✅ 会 UnicodeEncodeError
    raise SystemExit(main())
