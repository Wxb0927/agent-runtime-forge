"""D5 测试：RAG 检索线 + MCP 等价性。

⚠️ 这些用例默认用 **HashEmbedder**（确定性、零下载），所以它们证明的是
**管线正确**（分块可回溯、融合去重、引用可判定、非法参数自愈），
⛔ 证明"语义检索有多准"——那是 `eval/rag_eval.py --semantic` 跑出来写在报告里的数。

还有两条是专门防"自欺"的：
* 分块对照必须真的切出**不同块数**（同一个语料两种策略切出一样多 = 空转对照）；
* `hybrid+rerank` 在重排器缺失时必须**整档失败**，⛔ 拿 hybrid 的分数冒充重排后的分数。
"""
from __future__ import annotations

import pytest

from forge.contract import cards as C
from forge.mcp import manifest as MF
from forge.rag import evalset
from forge.rag.chunk import chunk_all
from forge.rag.corpus import chapters, corpus, doc_to_chapter
from forge.rag.retrieve import (BM25Index, DenseIndex, HashEmbedder, Retriever,
                                tokenize_zh, rrf)


@pytest.fixture(scope="module")
def ch() -> list:
    return chapters()


@pytest.fixture(scope="module")
def chunks(ch):
    return chunk_all(ch, size=160, overlap=40, max_len=240)


# ------------------------------------------------------------- 语料与分块


def test_chapters_cover_every_rule_without_loss(ch) -> None:
    members = {m for d in ch for m in (d.members or (d.id,))}
    assert members >= {d.id for d in corpus()}, "有条口径没归进章，等于从语料里消失了"


def test_two_chunking_strategies_actually_differ(chunks) -> None:
    assert chunks["fixed"] and chunks["section"]
    assert len(chunks["fixed"]) != len(chunks["section"]), \
        f"两种分块切出同样 {len(chunks['fixed'])} 块 ⇒ 对照是空转的，比了等于⛔ 比"


def test_every_chunk_is_traceable(chunks) -> None:
    ids = {d.id for d in corpus()} | {d.id for d in chapters()}
    for name, cs in chunks.items():
        for c in cs:
            assert c.doc_id in ids, (name, c.chunk_id)
            assert c.source, f"{c.chunk_id} 没有出处，引用率就无法判定"
            assert c.chunk_id.startswith(f"{c.doc_id}#"), c.chunk_id


def test_fixed_chunks_overlap_never_loses_text(ch, chunks) -> None:
    """定长切允许重叠，⛔ 丢字：每章原文里的每个长句都必须能在某个块里找到。"""
    for d in ch:
        joined = " ".join(c.text for c in chunks["fixed"] if c.doc_id == d.id)
        for clause in (x.strip() for x in d.body.split("；")):
            head = clause[:12]
            if len(head) >= 8:
                assert head in joined.replace(" ", "").replace(clause[:0], "") or head in joined, \
                    f"{d.id} 的片段在定长分块里丢了：{head!r}"


# ------------------------------------------------------------- 召回与融合


def test_tokenizer_falls_back_without_jieba() -> None:
    toks = tokenize_zh("预算执行率 70% budget")
    assert any("预" == t for t in toks) or "预算" in toks
    assert "70%" in toks or "70" in toks or any(t.startswith("70") for t in toks)
    assert "budget" in toks


def test_bm25_finds_the_exact_term(chunks) -> None:
    hits = BM25Index(chunks["section"]).search("EAC 零发生额 弃权", k=3)
    assert hits and any("EAC" in h.chunk.text or "弃权" in h.chunk.text for h in hits), hits


def test_dense_index_reports_its_backend_and_scores_are_cosines(chunks) -> None:
    """断言**性质**（后端已声明、分数是余弦量纲），⛔ 断言机器上装了 numpy/faiss。

    第一版写的是  —— 那等于把这台机器装了什么
    写进测试，换台机器（或像现在这样网络装不上）就红，且红了也⛔ 说明代码有问题。
    """
    emb = HashEmbedder()
    idx = DenseIndex(chunks['section'], emb)
    hits = idx.search('逾期率分母', k=3)
    assert hits and all(-0.0001 <= h.score <= 1.0001 for h in hits), hits
    assert idx.backend in ('python-cosine', 'numpy', 'faiss'), idx.backend
    assert idx.backend == Retriever(chunks['section'], embedder=emb).describe()['dense_backend']


def test_all_dense_backends_agree(chunks) -> None:
    """装了 numpy/faiss 时，三种后端必须给出同样的 top-3（⛔ 回退版悄悄分叉）。"""
    import importlib.util as ilu

    if not ilu.find_spec('numpy'):
        pytest.skip('本机未装 numpy（网络原因），无法比对后端一致性')
    emb = HashEmbedder()
    cs = chunks['section']
    py = DenseIndex(cs, emb)
    py.backend, py._index, py._np = 'python-cosine', None, None      # 强制走纯 python 路
    want = [h.chunk.chunk_id for h in py.search('预算超支判据 逐科目', k=3)]
    got = [h.chunk.chunk_id for h in DenseIndex(cs, emb).search('预算超支判据 逐科目', k=3)]
    assert want == got, (want, got)


def test_pure_python_bm25_agrees_with_rank_bm25(chunks) -> None:
    """两份 BM25 实现的 top-k 排序必须一致（装了包就自动核对，⛔ 悄悄分叉）。"""
    import importlib.util as ilu

    from forge.rag.retrieve import PyBM25, RankBm25Adapter, tokenize_zh as tk

    cs = chunks['section']
    toks = [tk(c.text) for c in cs]
    mine = PyBM25(toks)
    if not ilu.find_spec('rank_bm25'):
        pytest.skip('本机未装 rank_bm25（网络原因）；纯 python 版单独可用')
    theirs = RankBm25Adapter(toks)
    q = tk('合同关联稀疏能不能反查金额')
    top_mine = sorted(range(len(cs)), key=lambda i: -mine.scores(q)[i])[:3]
    top_theirs = sorted(range(len(cs)), key=lambda i: -theirs.scores(q)[i])[:3]
    assert top_mine == top_theirs, (top_mine, top_theirs)


def _fake_hits(cs, scores):
    class H:
        def __init__(self, chunk, score):
            self.chunk, self.score, self.via = chunk, score, ["x"]
    return [H(c, sc) for c, sc in zip(cs, scores)]


def test_rrf_ignores_raw_scores_and_uses_ranks_only() -> None:
    """名次相同 ⇒ 融合分必须相同，⛔ 把两路的原始分数混进比较。"""
    cs = chunk_all(chapters(), size=160, overlap=40, max_len=240)["section"][:3]
    x = rrf([_fake_hits(cs, [10.0, 9.0, 8.0])])
    y = rrf([_fake_hits(cs, [0.001, 0.0, -5.0])])          # 名次一样、分数天差地别
    assert [h.score for h in x] == [h.score for h in y]
    assert [h.chunk.chunk_id for h in x] == [c.chunk_id for c in cs]


def test_rrf_reorders_when_ranks_differ() -> None:
    """名次不同必须排出不同顺序——否则融合其实没起作用。"""
    cs = chunk_all(chapters(), size=160, overlap=40, max_len=240)["section"][:3]
    a = rrf([_fake_hits(cs, [1, 1, 1])], k=1.0)
    b = rrf([_fake_hits(list(reversed(cs)), [1, 1, 1])], k=1.0)
    assert a[0].chunk.chunk_id != b[0].chunk.chunk_id


def test_hybrid_dedupes_and_marks_provenance(chunks) -> None:
    r = Retriever(chunks["section"], embedder=HashEmbedder())
    hits = r.search("合同关联稀疏能不能反查金额", k=5, mode="hybrid")
    ids = [h.chunk.chunk_id for h in hits]
    assert len(ids) == len(set(ids)), "融合后没去重"
    assert all(h.via for h in hits)
    assert any(len(h.via) > 1 for h in hits), "没有任何块被两路同时召回？融合可能没生效"
    # ⚠️ 下面两条是补的：只断言"没重复 + 有 via"根本抓不到"每路只取第 1 名"这种退化
    #    （截断后池子里只剩 2 个块，前两条照样通过）。M18 变异就是靠这两条才转红。
    assert len(hits) == 5, f"融合后候选池只有 {len(hits)} 个 ⇒ 两路的第 2 名以后全被丢了"
    # 融合池是两路各自的 top-(3k)（见 Retriever.search），比对基准必须跟着取 3k，
    # ⛔ 拿 top-k 当基准——那会把正确实现判成"凭空造块"（我第一版就这么错了一次）
    bm = {h.chunk.chunk_id for h in r.bm25.search('预算超支判据', k=15)}
    dn = {h.chunk.chunk_id for h in r.dense.search('预算超支判据', k=15)}
    hyb = set(ids)
    assert hyb <= (bm | dn), "融合结果里出现了两路都没给的块"
    assert (bm - dn) & hyb or (dn - bm) & hyb, "只属于单路的块一个都没进融合"


def test_unknown_mode_raises(chunks) -> None:
    r = Retriever(chunks["section"], embedder=HashEmbedder())
    with pytest.raises(ValueError):
        r.search("x", mode="magic")


def test_rerank_mode_refuses_to_lie(chunks) -> None:
    """重排器不在 ⇒ 必须抛，⛔ 静默退化成 hybrid 并把分数标成 hybrid+rerank。"""
    r = Retriever(chunks["section"], embedder=HashEmbedder(), reranker=None)
    with pytest.raises(RuntimeError, match="reranker"):
        r.search("预算超支判据", k=3, mode="hybrid+rerank")


# ------------------------------------------------------------- 评估集


def test_qa_set_is_wellformed() -> None:
    items = evalset.load_set()
    assert len(items) >= 300, f"任务书要求 300 条，实际 {len(items)}"
    gold_ids = {d.id for d in chapters()}
    mapping = doc_to_chapter()
    assert all(mapping[i["gold_docs"][0]] in gold_ids for i in items)
    c = evalset.counts(items)
    assert c["合计"] == len(items) == sum(v for k, v in c.items() if k != "合计")
    assert set(c) >= {"定义类", "流程类", "异常处理类"}


def test_gold_chapter_is_retrievable_in_principle() -> None:
    """每题的 gold 章必须能被 BM25 以正分召回（⛔ 题目悬空：问的东西语料里根本没有）。

    ⚠️ 这条曾经写成 `... or True` ——永真断言，看起来有测试、实际零保护。
    "假能失败"最容易长在测试自己的身上。
    """
    mapping = doc_to_chapter()
    by_id = {d.id: d for d in chapters()}
    cs = chunk_all(chapters(), size=110, overlap=30, max_len=170)["section"]
    idx = BM25Index(cs)
    items = [i for i in evalset.load_set() if i["class"] == "定义类"][:20]
    # ① 映射完整性：M22 变异（把 doc_to_chapter 掏空）必须在这里红，⛔ 只断言"召得回"
    assert set(mapping) >= {i["gold_docs"][0] for i in items}, "gold→章 映射缺项（题目悬空）"
    assert {mapping[i["gold_docs"][0]] for i in items} <= set(by_id)
    # ② 召回率阈值：问句已改成中文说法（"材料费"），而语料原文含 material 这类枚举值，
    #    **词面不匹配是预期行为** ⇒ ⛔ 要求 100%（那等于要求语料长得像问句）。
    #    漏掉的那些由第二路（dense/融合）补位，实测见 eval/rag_report_semantic.md。
    hit = sum(1 for it in items
              if mapping[it["gold_docs"][0]]
              in {h.chunk.doc_id for h in idx.search(it["question"], k=5)})
    assert hit / len(items) >= 0.6, f"BM25 章节召回 {hit}/{len(items)} 低到不可用"


# ------------------------------------------------------------- MCP 等价性


def test_mcp_manifest_is_derived_from_d3_cards() -> None:
    tools = MF.manifest()
    cards = C.describe()
    assert [t["name"] for t in tools] == [f"forge_{b}" for b in sorted(cards)]
    for t in tools:
        branch = t["name"][len(MF.TOOL_PREFIX):]
        assert set(t["inputSchema"]["properties"]) == set(cards[branch]["params"])
        assert t["inputSchema"]["properties"]["metric"]["enum"] == cards[branch]["enums"]["metric"]
        assert t["inputSchema"]["additionalProperties"] is False


def test_mcp_surface_shares_the_validator(chunks) -> None:
    """非法参数自愈必须在 MCP 面上同样成立（同一校验器，⛔ 两套实现）。"""
    env = MF.call("forge_stats", {"metric": "conplte_rate", "topK": 999})
    assert env["code"] == "BAD_INTENT" and env["digest"] == []
    assert any(n.startswith("CLAMPED topK 999→50") for n in env["notes"])
    heal = [n for n in env["notes"] if n.startswith("heal:")]
    assert heal and "completion_rate" in heal[0]


def test_mcp_valid_call_returns_traceable_number() -> None:
    env = MF.call("forge_stats", {"metric": "completion_rate", "project": "PRJ-1180"},
                  user_key="u0041")
    assert env["ok"] is True and env["code"] == "OK"
    m = env["digest"][0]
    assert m["numerator"] == 33 and m["denominator"] == 38 and m["as_of"] == "2026-10-30"


def test_mcp_unknown_tool_uses_the_same_refusal_path() -> None:
    env = MF.call("nope", {"metric": "completion_rate"})
    assert env["code"] in ("BAD_INTENT",), env
