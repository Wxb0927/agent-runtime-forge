"""MCP Server：把 D3 契约层的三张卡片以标准协议暴露出去。

⚠️ 这一层**只做协议翻译**，校验一律走 `manifest.call` → `intent.run`，
所以"MCP 面"与"自研 `--intent` 面"的行为**必然逐字一致**——这正是 D5 要验的等价性；
两边各写一套校验，等价性就成了口号。

⚠️ API 版本：本机装的是 **mcp 2.x**，`FastMCP` 已改名 `MCPServer`
（`mcp.server.mcpserver`），第一版按记忆的 `from mcp.server.fastmcp import FastMCP` 写，
直接 `ModuleNotFoundError`。⇒ 工具参数面用**显式类型签名**声明（SDK 由它生成 schema），
⛔ `**kwargs`——那样生成的 schema 是空的，等价性断言会假过。

跑法：`uv run python -m forge.mcp.server`（stdio 传输）
"""
from __future__ import annotations

import inspect
import sys
from typing import Any, Optional

from forge.mcp import manifest as M


def _handler(tool_name: str, metric_enum: tuple[str, ...]):
    """给一个 MCP 工具造 handler。参数面必须与 D3 卡片一致：metric / project / topK。

    ⚠️ 枚举必须**写进类型**才会上线：实测 SDK 生成的 `input_schema` 只看函数签名，
    `metric: str` 出去就没有 enum 字段，"合法枚举随协议下发"这句就成了假话。
    ⇒ 用 `Literal[...]` 动态标注，卡片里的枚举才真的走协议。
    这条差别本身就是 D3 的价值：**MCP 只声明 schema，⛔ 定义"非法参数怎么办"**。
    """
    from typing import Literal

    def run(metric: Literal[...], project: Optional[str] = None, topK: int = 10) -> dict[str, Any]:
        return M.call(tool_name, {"metric": metric, "project": project, "topK": topK})

    run.__name__ = tool_name.replace("-", "_")
    run.__annotations__["metric"] = (
        Literal[tuple(metric_enum)] if metric_enum else str)   # type: ignore[misc]
    return run


def build_server() -> Any:
    try:
        from mcp.server.mcpserver import MCPServer      # mcp 2.x
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"mcp SDK 接口不符（{type(exc).__name__}: {exc}）。本文件按 mcp 2.x 的 MCPServer 写；"
            "若装的是 1.x 请改用 mcp.server.fastmcp.FastMCP，或 uv add 'mcp>=2'。⛔ 假装跑通。"
        ) from exc

    srv = MCPServer("agent-runtime-forge")
    cards = M.C.describe()
    for tool in M.manifest():
        branch = tool["name"][len(M.TOOL_PREFIX):]
        props = set(cards[branch]["params"])
        handler = _handler(tool["name"], tuple(cards[branch]["enums"]["metric"]))
        # 一致性自检：卡片声明的参数面必须与 handler 的真实签名一致，
        # 否则协议侧 schema 与 D3 卡片就悄悄分叉（这条比任何文档都可靠）
        sig = set(inspect.signature(handler).parameters)
        if sig != props:
            raise AssertionError(
                f"{tool['name']} 卡片参数 {sorted(props)} 与 handler {sorted(sig)} 不一致")
        srv.add_tool(handler, name=tool["name"], description=tool["description"])
    return srv


def main(argv: list[str] | None = None) -> int:
    build_server().run(transport="stdio")
    return 0


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):   # 中文 Windows 控制台默认 cp936：
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")   # 打 ⛔/✅ 会 UnicodeEncodeError
    raise SystemExit(main(sys.argv[1:]))
