# -*- coding: utf-8 -*-
"""MCP 工具清单：**从 D3 的 `describe()` 派生**，⛔ 第二份说明书。

这一层存在的意义是做一个可检验的主张：
**"自研契约三柱"与"MCP 标准工具面"解决的是同一类问题，且能力可以互译**——
`tools/list` ↔ `cards.json` 菜单自吐，`inputSchema` ↔ `--describe` 代码派生。

差别也要说清（这是面试里"那你们是不是就是 MCP"的标准答案）：
MCP 只声明 schema，**⛔ 定义"非法参数怎么办"**。本仓库把 `BAD_INTENT` +
回带合法枚举 + 钳位语义一并带过来说明层（见 `errorSchema`/`notes`），
所以两边跑同一份卡片、同一个校验器，行为逐字一致。
"""
from __future__ import annotations

from typing import Any

from forge.contract import cards as C
from forge.contract import intent as I

#: MCP 工具名 = 分支名 + 固定前缀，⛔ 手抄第二份名单
TOOL_PREFIX = "forge_"


def json_type(prop: dict[str, Any]) -> str:
    return str(prop.get("type") or "string")


def tool_schema(branch: str, card: dict[str, Any]) -> dict[str, Any]:
    """把 D3 卡片翻成 MCP `inputSchema`（JSON Schema 形状）。"""
    props: dict[str, Any] = {}
    required: list[str] = []
    for pname, spec in sorted(card["params"].items()):
        entry: dict[str, Any] = {"type": json_type(spec), "description": spec.get("description", "")}
        if "min" in spec:
            entry["minimum"] = spec["min"]
        if "max" in spec:
            entry["maximum"] = spec["max"]
        if "default" in spec:
            entry["default"] = spec["default"]
        if pname == "metric":
            entry["enum"] = card["enums"]["metric"]        # 合法枚一举直带进 schema
        props[pname] = entry
        if spec.get("required"):
            required.append(pname)
    return {
        "name": TOOL_PREFIX + branch,
        "description": card["summary"] + "｜非法参数会被点名并回带合法枚举（BAD_INTENT 自愈）",
        "inputSchema": {"type": "object", "properties": props, "required": sorted(required),
                        "additionalProperties": False},
        # 自研契约层比 MCP 多出来的部分，显式带出来，⛔ 假装是协议自带
        "_forge_extra": {"host_injected": card["host_injected"], "enums": card["enums"],
                         "derived_from": card["derived_from"]},
    }


def manifest() -> list[dict[str, Any]]:
    cards = C.describe()
    return [tool_schema(b, cards[b]) for b in sorted(cards)]


def call(tool_name: str, arguments: dict[str, Any], *, user_key: str = "u0000",
         as_of: str = "2026-10-30") -> dict[str, Any]:
    """MCP `tools/call` 的后端：直接落到 D3 的 `intent.run`，⛔ 另写一套校验。"""
    if not tool_name.startswith(TOOL_PREFIX):
        return I.run({"metric": "__unknown_tool__"})       # 未知工具也走同一条拒答路径
    branch = tool_name[len(TOOL_PREFIX):]
    metric = arguments.get("metric")
    if metric and branch:
        from forge.contract import metrics as M

        try:
            if M.branch_of(metric) != branch:      # 工具名与指标不符 ⇒ 交校验器点名，⛔ 自行纠正
                metric = f"{metric}__mismatch__{branch}"
        except KeyError:
            pass                                    # 指标本身非法，交给 intent 报错
    return I.run({"metric": metric or "",
                  **({"project": arguments["project"]} if arguments.get("project") else {}),
                  **({"topK": arguments["topK"]} if "topK" in arguments else {})},
                 user_key=user_key, as_of=as_of)
