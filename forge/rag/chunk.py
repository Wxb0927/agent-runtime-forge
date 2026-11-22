# -*- coding: utf-8 -*-
"""两种分块策略做**对照**，⛔ 只报好看的那一档。

* `fixed`：按字符数定长切（重叠若干字），实现简单，但会把一条口径从中间切断
  ——例如「预算为 0 时结果是无数据」被切到两块，召回其中一块往往答不全。
* `section`：按语料自身的结构切（标题 + 每条 `；` 子句），边界天然对齐语义。

对照的意义：分块策略⛔ 是玄学选择，它直接决定 Recall@5。报告里两档都印，
并把「最差的一类问题」一起印出来（任务书 D5 明确要求）。
"""
from __future__ import annotations

from dataclasses import dataclass

from forge.rag.corpus import Doc


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    text: str
    strategy: str
    ordinal: int
    source: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"chunk_id": self.chunk_id, "doc_id": self.doc_id, "text": self.text,
                "strategy": self.strategy, "ordinal": self.ordinal, "source": self.source}


def chunk_fixed(docs: list[Doc], size: int = 180, overlap: int = 40) -> list[Chunk]:
    """定长切：⛔ 看语义边界，只数字符。"""
    out: list[Chunk] = []
    step = max(size - overlap, 1)
    for d in docs:
        full = f"{d.title}。{d.body}"
        pieces = [full[i:i + size] for i in range(0, max(len(full) - overlap, 1), step)]
        for n, piece in enumerate(p for p in pieces if p.strip()):
            out.append(Chunk(f"{d.id}#f{n}", d.id, piece.strip(), "fixed", n, d.source))
    return out


def chunk_section(docs: list[Doc], max_len: int = 220) -> list[Chunk]:
    """章节级切：按标题与子句边界，超过 max_len 才继续合并。"""
    out: list[Chunk] = []
    for d in docs:
        buf = ""
        n = 0
        for line in d.sections:
            if buf and len(buf) + len(line) + 1 > max_len:
                out.append(Chunk(f"{d.id}#s{n}", d.id, buf.strip(), "section", n, d.source))
                buf, n = "", n + 1
            buf = f"{buf}；{line}" if buf else line
        if buf.strip():
            out.append(Chunk(f"{d.id}#s{n}", d.id, buf.strip(), "section", n, d.source))
    return out


def chunk_all(docs: list[Doc], size: int = 180, overlap: int = 40,
              max_len: int = 220) -> dict[str, list[Chunk]]:
    return {"fixed": chunk_fixed(docs, size, overlap), "section": chunk_section(docs, max_len)}
