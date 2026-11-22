# -*- coding: utf-8 -*-
"""MCP Server：把 D3 契约层的三张卡片以标准协议暴露出去。

⚠️ 这一层**只做协议翻译**，校验一律走 `manifest.call` → `intent.run`，
所以"MCP 面"与"自研 `--intent` 面"的行为**必然逐字一致**——这正是 D5 要验的等价性，
如果两边各写一套校验，等价性就成了口号。

跑法：`uv run python -m forge.mcp.server`（stdio）
缺 `mcp` 包时直接报错退出，⛔ 假装跑通（`tools/d5_mcp_smoke.py` 同理会显式 skip）。
"""
from __future__ import annotations

import sys
from typing import Any

from forge.mcp import manifest as M


def build_server() -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"缺 mcp 包（{type(exc).__name__}）：先 `uv add mcp`，⛔ 用自造协议冒充 MCP") from exc

    mcp = FastMCP("agent-runtime-forge")

    for tool in M.manifest():
        name, desc = tool["name"], tool["description"]
        schema = tool["inputSchema"]

        def make(tool_name: str, required: list[str]):
            def handler(**kwargs: Any) -> dict[str, Any]:
                missing = [k for k in required if k not in kwargs]
                args = dict(kwargs)
                if missing:                       # 缺必填也走同一条 BAD_INTENT 路径
                    args.setdefault("metric", "")
                return M.call(tool_name, args)
            handler.__name__ = tool_name.replace("-", "_")
            handler.__doc__ = desc
            return handler

        mcp.add_tool(make(name, schema.get("required", [])), name=name, description=desc)
    return mcp


def main(argv: list[str] | None = None) -> int:
    build_server().run()          # 默认 stdio 传输
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
