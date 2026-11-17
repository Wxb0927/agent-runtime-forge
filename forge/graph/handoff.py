"""上游交接文件（handoff）的两种写法：事故原形 vs 修复后。

这个模块存在的唯一目的：让"共享可变状态导致的静默覆盖"在本仓库里**可复现、可断言、可变异**。

- ``SharedHandoff.update_branch()`` —— 公司当年的形状：所有分支读同一份文档、
  改写同一组 ``from``/``step`` 字段、整体写回。读-改-写不原子 ⇒ 后写覆盖先写，
  **数据是对的、来源标签漂了**（这正是当时观察到的症状）。
- ``NamespacedHandoff.write_branch()`` —— 修复后的形状：单一写者（fan-in 节点）
  按分支命名空间写入，分支自己⛔ 碰外部状态，只写自己的图通道。

``interleave_hook`` 是给测试用的**确定性交错**手段：两路都在写之前先对齐栅栏，
保证读-改-写真的重叠，⛔ 靠 sleep 碰运气（那样测出来的红是抖动，不是证据）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

Hook = Optional[Callable[[str], None]]


class _Store:
    def __init__(self, path: Path, interleave_hook: Hook = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")
        self._hook = interleave_hook

    def read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8") or "{}")

    def _write(self, doc: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    def snapshot(self) -> dict[str, Any]:
        return self.read()


class SharedHandoff(_Store):
    """⛔ 事故复现用，⛔ 出现在默认路径里。"""

    def update_branch(self, branch: str, from_: str, step: int,
                      row_counts: dict[str, int]) -> None:
        """读-改-写同一个 key：``from``/``step`` 会被后写者覆盖。

        ⚠️ 这里**故意不加锁**——原事故就是不原子的 read-modify-write。
        加锁只能把交错概率压下去，⛔ 证明不了"设计安全"，所以修复方案⛔ 是加锁。
        """
        doc = self.read()
        if self._hook:  # 两路在此对齐 ⇒ 读-改-写必然交错
            self._hook(branch)
        doc["from"] = from_          # ← 互相覆盖的那一行
        doc["step"] = step           # ← 互相覆盖的那一行
        doc.setdefault("branches", [])
        if branch not in doc["branches"]:
            doc["branches"].append(branch)
        doc.setdefault("row_counts", {})[branch] = dict(row_counts)
        self._write(doc)


class NamespacedHandoff(_Store):
    """✅ 默认形状：分支⛔ 直接写这里，只有 fan-in 节点单点写，且按命名空间写。"""

    def write_merged(self, branches: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """单写者一次写入全部分支产物 ⇒ 不存在互相覆盖的窗口。"""
        doc = self.read()
        for branch, payload in sorted(branches.items()):
            doc[branch] = dict(payload)
        self._write(doc)
        return doc
