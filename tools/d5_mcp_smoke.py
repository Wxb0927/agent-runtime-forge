#!/usr/bin/env python
"""用**真 MCP 客户端**连本仓库的 server，调 ≥3 个工具并断言非法参数自愈（任务书 D5 验收）。

⚠️ 三条设计：

1. **缺 `mcp` 包 ⇒ 退出码 3 并说明原因**，⛔ 用"清单是我自己生成的"来冒充"客户端调通了"。
   等价性测试（`tests/test_rag_mcp.py`）不需要协议就能跑，那是另一回事。
2. 断言的是**行为**而不是"有响应"：`tools/list` 的 schema 必须与 D3 卡片逐字相等；
   非法 metric 必须 `BAD_INTENT` 且回带枚举；越界 topK 必须钳位；越权项目必须零数据。
3. 走 in-memory session（同进程），⛔ 起子进程后管 stdio 的时序问题——那是传输层测试，
   与"D5 验收：客户端调通 ≥3 个工具"无关。

跑法：`uv run python tools/d5_mcp_smoke.py`
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MISSING_EXIT = 3
FAIL_EXIT = 1


def _need(name: str) -> None:
    try:
        __import__(name)
    except Exception as exc:  # noqa: BLE001
        print(f"[SKIP] 缺 {name} 包（{type(exc).__name__}: {exc}）；"
              f"装它：uv sync --extra mcp。本轮不算跑通。")
        raise SystemExit(MISSING_EXIT)


async def amain() -> int:
    _need("mcp")
    from mcp.shared.memory import create_connected_server_and_client_session

    from forge.contract import cards as C
    from forge.mcp import manifest as M
    from forge.mcp.server import build_server

    checks: list[tuple[str, bool, str]] = []
    server = build_server()
    async with create_connected_server_and_client_session(server) as client:
        listed = await client.list_tools()
        names = sorted(t.name for t in listed.tools)
        expect = sorted(t["name"] for t in M.manifest())
        checks.append(("tools/list == D3 卡片派生", names == expect, f"{names} vs {expect}"))

        cards = C.describe()
        schema_ok = True
        for t in listed.tools:
            branch = t.name[len(M.TOOL_PREFIX):]
            props = set((t.inputSchema or {}).get("properties", {}))
            schema_ok &= props == set(cards[branch]["params"])
            schema_ok &= (t.inputSchema or {}).get("additionalProperties") is False
        checks.append(("inputSchema 与卡片参数面一致", schema_ok, ""))

        # ① 合法调用（三个工具各一次，满足"调通 ≥3 个工具"）
        calls = {}
        for branch in ("stats", "deviation", "cost"):
            metric = cards[branch]["enums"]["metric"][0]
            res = await client.call_tool(M.TOOL_PREFIX + branch, {"metric": metric, "topK": 3})
            payload = json.loads(res.content[0].text)
            calls[branch] = payload
        ok3 = all(p.get("ok") and p.get("digest") for p in calls.values())
        checks.append(("三个工具都能取到数", ok3, json.dumps(
            {k: (v.get("code"), len(v.get("digest") or [])) for k, v in calls.items()},
            ensure_ascii=False)))

        # ② 非法参数自愈
        bad = json.loads((await client.call_tool(
            "forge_stats", {"metric": "conplte_rate", "topK": 999})).content[0].text)
        checks.append(("非法 metric 报 BAD_INTENT", bad.get("code") == "BAD_INTENT", str(bad.get("message"))[:60]))
        checks.append(("回带合法枚举（自愈依据）",
                       bool([n for n in bad.get("notes", []) if str(n).startswith("heal:")]), ""))
        checks.append(("越界 topK 被钳位而非报错",
                       any(str(n).startswith("CLAMPED topK 999→50") for n in bad.get("notes", [])), ""))
        checks.append(("非法参数⛔ 被执行（digest 必须空）", bad.get("digest") == [], ""))

        # ③ 越权点名零数据
        denied = json.loads((await client.call_tool(
            "forge_stats", {"metric": "completion_rate", "project": "PRJ-1007"},
        )).content[0].text)
        checks.append(("越权项目拒答且零数据",
                       denied.get("code") in ("NOT_IN_SCOPE", "BAD_INTENT") and not denied.get("digest"),
                       str(denied.get("code"))))

    failed = 0
    for name, passed, detail in checks:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        failed += 0 if passed else 1
    print(f"\nMCP 客户端往返：{len(checks) - failed}/{len(checks)} 项通过")
    return FAIL_EXIT if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
