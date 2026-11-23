"""检索层：BM25 + dense 双路召回、RRF 融合、可选重排，**结果带出处**。

⚠️ 三件必须说清的事：

1. **embedder 可插拔**，是为了让"哪些分数是真模型跑出来的"永远可追溯：
   * `HashEmbedder` —— 字符 n-gram 哈希向量，确定性、零下载，**只用于结构测试**。
     它⛔ 有语义能力，拿它的分数说"dense 召回提升了多少"就是造假。
   * `BGEEmbedder` —— 真语义模型（`bge-small-zh-v1.5`）。加载前先 `env.guard_or_fail()`
     把缓存钉在非系统盘。
2. **实现自报**：`Retriever.describe()` 会打印 BM25 用的是 `rank_bm25` 还是内置纯
   python 版、dense 后端是 faiss/numpy/纯 python、分词器是 jieba 还是回退。
   ⇒ 报告里每个数字都知道是哪个实现跑出来的，⛔ "装了依赖"被当成默认事实。
3. **纯 python 回退是刻意的**：本仓库的测试与评测⛔ 该被一个下载卡死
   （写这份时这台机器的网络连 15 MB 的 numpy 都下不动）。代价是多一份实现，
   ⇒ 所以有一条测试专门比对两份 BM25 的分数排序，装了包就自动核对。
"""
from __future__ import annotations

import hashlib
import importlib.util as _ilu
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Sequence

from forge.rag.chunk import Chunk

CJK_RE = re.compile(r"[一-鿿]")
LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*")
NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def tokenizer_name() -> str:
    return "jieba" if _ilu.find_spec("jieba") else "fallback(汉字单字+二字+latin/num)"


# ---------------------------------------------------------------- tokenizer


def tokenize_zh(text: str) -> list[str]:
    """jieba 分词；没装就退回「汉字单字 + 相邻二字 + 拉丁/数字」，⛔ 整段当一个词。"""
    if _ilu.find_spec("jieba"):
        try:
            import jieba

            return [t for t in (x.strip() for x in jieba.lcut(text)) if t]
        except Exception:  # noqa: BLE001
            pass
    out: list[str] = []
    chars = [c for c in text if CJK_RE.match(c)]
    out.extend(chars)
    out.extend(a + b for a, b in zip(chars, chars[1:]))
    out.extend(LATIN_RE.findall(text))
    out.extend(NUM_RE.findall(text))
    return out


# ---------------------------------------------------------------- embedders


class HashEmbedder:
    """确定性哈希向量（测试与离线档）。⛔ 语义能力，⛔ 用它报召回提升。"""

    name = "hash-ngram-256"
    dim = 256
    semantic = False

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in tokenize_zh(text):
            h = int(hashlib.blake2b(tok.encode("utf-8"), digest_size=8).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class BGEEmbedder:
    """真语义模型。第一次用才加载，加载前必须已经把缓存钉到非系统盘。"""

    name = "bge-small-zh-v1.5"
    semantic = True

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5") -> None:
        from forge import env

        env.guard_or_fail()
        self.model_name = model_name
        self.name = model_name
        self._m: Any = None
        self.dim = 0

    def _model(self) -> Any:
        if self._m is None:
            from sentence_transformers import SentenceTransformer

            self._m = SentenceTransformer(self.model_name)
            # 新版 sentence-transformers 把 get_sentence_embedding_dimension 改名了
            # （实测 FutureWarning）⇒ 两个名字都兼容，⛔ 锁死某一个版本
            getter = getattr(self._m, "get_embedding_dimension", None)
            if getter is None:
                getter = self._m.get_sentence_embedding_dimension
            self.dim = int(getter())
        return self._m

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        vecs = self._model().encode(list(texts), normalize_embeddings=True,
                                    show_progress_bar=False)
        return [[float(x) for x in v] for v in vecs]


# ---------------------------------------------------------------- BM25


class PyBM25:
    """Okapi BM25 的纯 python 实现，**与 rank_bm25 同参数同公式**（等价性有测试钉住）。

    为什么自带一份：这台机器写这份代码时网络慢到 15 MB 的 numpy 都要 15 分钟，
    检索线⛔ 该被一个下载卡死。代价是多一份实现 ⇒ 所以：
    ① k1/b **显式写出**（实测库默认是 k1=1.5,b=0.75；我第一版凭记忆写 1.2，反而制造了分叉 ⇒ 默认值也要查，⛔ 背）；
    ② idf 负值走库同样的 epsilon 兜底（df 超过一半时 log 会变负，
       库把它替换成 `epsilon * avg_idf`，我第一版⛔ 处理这一条，
       于是"两份实现等价"其实是运气——fresh-clone 验证把它抓了出来）。
    """

    name = "py-bm25(k1=1.5,b=0.75,eps=0.25)"

    def __init__(self, corpus_tokens: Sequence[Sequence[str]], k1: float = 1.5,
                 b: float = 0.75, epsilon: float = 0.25) -> None:
        self.k1, self.b, self.epsilon = k1, b, epsilon
        self.docs = [Counter(t) for t in corpus_tokens]
        self.doc_len = [sum(c.values()) for c in self.docs]
        n_docs = len(self.docs) or 1
        self.avgdl = (sum(self.doc_len) / n_docs) or 1.0

        df: Counter = Counter()
        for c in self.docs:
            df.update(set(c))   # ⚠️ 文档频次（该词出现在几篇里），⛔ 词频累加
        #    第一版写成 df.update(c)，词频会让 df 超过 N ⇒ log(N-df+0.5) 进负数域直接炸
        idf: dict[str, float] = {}
        for term, freq in df.items():
            idf[term] = math.log(n_docs - freq + 0.5) - math.log(freq + 0.5)
        positive = [v for v in idf.values() if v > 0]
        avg_idf = (sum(positive) / len(positive)) if positive else 1.0
        for term, v in list(idf.items()):
            if v <= 0:
                idf[term] = self.epsilon * avg_idf
        self.idf = idf

    def scores(self, query: Sequence[str]) -> list[float]:
        out = [0.0] * len(self.docs)
        for term in query:
            idf = self.idf.get(term)
            if idf is None:
                continue
            for i, doc in enumerate(self.docs):
                f = doc.get(term, 0)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self.doc_len[i] / self.avgdl)
                out[i] += idf * f * (self.k1 + 1.0) / denom
        return out


class RankBm25Adapter:
    name = "rank_bm25.BM25Okapi"

    def __init__(self, corpus_tokens: Sequence[Sequence[str]], k1: float = 1.5,
                 b: float = 0.75, epsilon: float = 0.25) -> None:
        from rank_bm25 import BM25Okapi

        # 参数**显式传两边**，⛔ 依赖库默认值哪天变了
        self._bm = BM25Okapi([list(t) for t in corpus_tokens], k1=k1, b=b, epsilon=epsilon)

    def scores(self, query: Sequence[str]) -> list[float]:
        return [float(x) for x in self._bm.get_scores(list(query))]


@dataclass
class Hit:
    chunk: Chunk
    score: float
    via: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"chunk_id": self.chunk.chunk_id, "doc_id": self.chunk.doc_id,
                "score": round(self.score, 6), "via": self.via, "source": self.chunk.source,
                "text": self.chunk.text[:120]}


class BM25Index:
    def __init__(self, chunks: Sequence[Chunk]) -> None:
        self.chunks = list(chunks)
        toks = [tokenize_zh(c.text) for c in self.chunks]
        try:
            self.bm: Any = RankBm25Adapter(toks)
        except Exception:  # noqa: BLE001
            self.bm = PyBM25(toks)

    def search(self, query: str, k: int = 5) -> list[Hit]:
        scores = self.bm.scores(tokenize_zh(query))
        order = sorted(range(len(self.chunks)), key=lambda i: -scores[i])[:k]
        return [Hit(self.chunks[i], float(scores[i]), ["bm25"])
                for i in order if scores[i] > 0]


class DenseIndex:
    """归一化向量 + 内积 = 余弦。后端依次尝试 faiss → numpy → 纯 python，**并报告用了哪个**。"""

    def __init__(self, chunks: Sequence[Chunk], embedder: Any) -> None:
        self.chunks = list(chunks)
        self.embedder = embedder
        self.vectors = [[float(x) for x in v] for v in embedder.encode([c.text for c in self.chunks])]
        self._np: Any = None
        self._index: Any = None
        self.backend = "python-cosine"
        try:
            import numpy as np

            self._np = np
            self.matrix = np.asarray(self.vectors, dtype="float32")
            self.backend = "numpy"
            try:
                import faiss

                self._index = faiss.IndexFlatIP(int(self.matrix.shape[1]))
                self._index.add(self.matrix)
                self.backend = "faiss"
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    def search(self, query: str, k: int = 5) -> list[Hit]:
        qv = [float(x) for x in self.embedder.encode([query])[0]]
        if self._index is not None:
            scores, idxs = self._index.search(self._np.asarray([qv], dtype="float32"), k)
            pairs = [(int(i), float(s)) for i, s in zip(idxs[0], scores[0]) if i >= 0]
        elif self._np is not None:
            sims = self._np.asarray(self.vectors, dtype="float32") @ self._np.asarray(qv, dtype="float32")
            order = self._np.argsort(-sims)[:k]
            pairs = [(int(i), float(sims[i])) for i in order]
        else:
            scored = sorted(((self._cos(qv, v), i) for i, v in enumerate(self.vectors)), reverse=True)
            pairs = [(i, s) for s, i in scored[:k]]
        return [Hit(self.chunks[i], s, ["dense"]) for i, s in pairs]

    @staticmethod
    def _cos(a: Sequence[float], b: Sequence[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        nb = math.sqrt(sum(y * y for y in b)) or 1.0
        return dot / (na * nb)


# ---------------------------------------------------------------- fusion


def rrf(lists: Sequence[Sequence[Hit]], k: float = 60.0) -> list[Hit]:
    """Reciprocal Rank Fusion：只用名次，⛔ 混不同量纲的分数。"""
    pool: dict[str, Hit] = {}
    acc: dict[str, float] = {}
    vias: dict[str, set[str]] = {}
    for lst in lists:
        for rank, hit in enumerate(lst):
            cid = hit.chunk.chunk_id
            pool.setdefault(cid, hit)
            acc[cid] = acc.get(cid, 0.0) + 1.0 / (k + rank + 1)
            vias.setdefault(cid, set()).update(hit.via)
    return [Hit(pool[cid].chunk, score, sorted(vias[cid]))
            for cid, score in sorted(acc.items(), key=lambda kv: -kv[1])]


class Retriever:
    """统一入口：mode ∈ bm25 | dense | hybrid | hybrid+rerank。"""

    def __init__(self, chunks: Sequence[Chunk], *, embedder: Any = None,
                 reranker: Any = None) -> None:
        self.chunks = list(chunks)
        self.bm25 = BM25Index(self.chunks)
        self.embedder = embedder or HashEmbedder()
        self.dense = DenseIndex(self.chunks, self.embedder)
        self.reranker = reranker

    def search(self, query: str, k: int = 5, mode: str = "hybrid") -> list[Hit]:
        if mode == "bm25":
            return self.bm25.search(query, k)
        if mode == "dense":
            return self.dense.search(query, k)
        if mode not in ("hybrid", "hybrid+rerank"):
            raise ValueError(f"未知 mode：{mode}")
        pool = rrf([self.bm25.search(query, k * 3), self.dense.search(query, k * 3)])
        if mode == "hybrid":
            return pool[:k]
        if self.reranker is None:
            # ⛔ 假装重排过：没有 reranker 就整档失败，
            #    否则报告里的 "hybrid+rerank" 其实是 hybrid 的成绩
            raise RuntimeError("mode=hybrid+rerank 需要 reranker；本次未加载")
        cand = pool[:k * 3]
        # 有批量接口就用批量（实测单对 predict 1.6 万次能把评测拖成分钟级）
        if hasattr(self.reranker, "rank_batch"):
            scores = self.reranker.rank_batch(query, [h.chunk.text for h in cand])
        else:
            scores = [self.reranker.score(query, h.chunk.text) for h in cand]
        scored = sorted(zip(scores, cand), key=lambda x: -x[0])
        return [Hit(h.chunk, s, h.via + ["rerank"]) for s, h in scored[:k]]

    def describe(self) -> dict[str, Any]:
        return {"chunks": len(self.chunks),
                "embedder": getattr(self.embedder, "name", "?"),
                "embedder_is_semantic": bool(getattr(self.embedder, "semantic", False)),
                "bm25_impl": getattr(self.bm25.bm, "name", "?"),
                "dense_backend": self.dense.backend,
                "tokenizer": tokenizer_name(),
                "reranker": getattr(self.reranker, "name", None)}
