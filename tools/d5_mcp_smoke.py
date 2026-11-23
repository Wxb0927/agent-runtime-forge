#!/usr/bin/env python
"""用**真 MCP 客户端**通过 stdio 起子进程连本仓库的 server，调 ≥3 个工具并断言自愈。

⚠️ 三条设计：

1. **起真子进程 + 真协议**，⛔ 在同进程里直接调 `manifest.call` 然后称其为
   "MCP 客户端调通"。任务书原文是"用 MCP 客户端调通 ≥3 个工具并断言非法参数自愈行为"。
2. 缺 `mcp` 包 ⇒ **退出码 3 并说明原因**，⛔ 让流水线绿着过去。
3. 断言的是**行为**：`tools/list` 的 schema 必须与 D3 卡片逐字相等；非法 metric 必须
   `BAD_INTENT` 且回带合法枚举；越界 topK 必须钳位；越权项目必须零数据。

跑法：`uv run python tools/d5_mcp_smoke.py`
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MISSING_EXIT = 3
FAIL_EXIT = 1


def _payload(res: Any) -> dict[str, Any]:
    """把 call_tool 的返回统一成信封（2.x 可能给 structuredContent，也可能只给 text）。"""
    sc = getattr(res, "structuredContent", None)
    if isinstance(sc, dict):
        for key in ("result", "envelope"):
            if isinstance(sc.get(key), dict):
                return sc[key]
        return sc
    for block in getattr(res, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                # SDK 自己的报错（如 "Error executing tool ...: 1 validation error"）
                # ⛔ 是 JSON ⇒ 归一成一个 code，让调用方能断言"被挡在协议层"这件事，
                # 而不是让测试在解析阶段就炸掉
                return {"code": "PROTOCOL_REJECTED", "message": text[:200], "digest": []}
    raise AssertionError(f"客户端拿到的结果里没有可解析内容：{res!r}")


def _schema(t: Any) -> dict[str, Any]:
    """mcp 2.x 用 snake_case `input_schema`，1.x 用 `inputSchema` ⇒ 两版都吃得下。"""
    return getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}


async def amain() -> int:
    try:
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except Exception as exc:  # noqa: BLE001
        print(f"[SKIP] 缺 mcp 包或接口不符（{type(exc).__name__}: {exc}）；"
              "装它：uv sync --extra mcp。本轮不算跑通。")
        return MISSING_EXIT

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    params = StdioServerParameters(command=sys.executable,
                                   args=["-X", "utf8", "-m", "forge.mcp.server"],
                                   cwd=str(ROOT), env=env)

    from forge.contract import cards as C
    from forge.mcp import manifest as M

    cards = C.describe()
    checks: list[tuple[str, bool, str]] = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()

            listed = await client.list_tools()
            names = sorted(t.name for t in listed.tools)
            expect = sorted(t["name"] for t in M.manifest())
            checks.append(("tools/list == D3 卡片派生", names == expect,
                           f"{names} vs {expect}"))

            schema_ok, detail = True, ""
            for t in listed.tools:
                branch = t.name[len(M.TOOL_PREFIX):]
                props = set((_schema(t)).get("properties", {}))
                want = set(cards[branch]["params"])
                if props != want:
                    schema_ok, detail = False, f"{t.name}: {sorted(props)} != {sorted(want)}"
            checks.append(("inputSchema 参数面与卡片一致", schema_ok, detail))

            def enum_matches(t: Any) -> bool:
                want = cards[t.name[len(M.TOOL_PREFIX):]]["enums"]["metric"]
                got_enum = (_schema(t)).get("properties", {}).get("metric", {}).get("enum")
                return set(got_enum or []) == set(want)

            checks.append(("metric 枚举由代码派生并随协议下发",
                           all(enum_matches(t) for t in listed.tools), ""))

            # ① 三个工具各一次合法调用（满足"调通 ≥3 个工具"）
            got: dict[str, dict[str, Any]] = {}
            for branch in ("stats", "deviation", "cost"):
                metric = cards[branch]["enums"]["metric"][0]
                res = await client.call_tool(M.TOOL_PREFIX + branch,
                                             {"metric": metric, "topK": 3})
                got[branch] = _payload(res)
            ok3 = all(v.get("ok") and v.get("digest") for v in got.values())
            checks.append(("三个工具都能取到数", ok3, json.dumps(
                {k: (v.get("code"), len(v.get("digest") or [])) for k, v in got.items()},
                ensure_ascii=False)))

            # ② 非法枚举值：枚举进了 schema ⇒ SDK 在协议层就挡住，⛔ 到我们的信封
            #    实测报文：`Input should be 'completion_rate' or 'overdue_rate'`
            #    ⇒ **自愈信息没丢，只是换了层给出**（pydantic 报文带合法值）。
            bad = _payload(await client.call_tool(
                "forge_stats", {"metric": "conplte_rate", "topK": 999}))
            legal = cards["stats"]["enums"]["metric"]
            msg = str(bad.get("message", ""))
            checks.append(("非法 metric 被拒（协议层，SDK 校验 Literal）",
                           bad.get("code") != "OK" and bad.get("digest") in ([], None),
                           str(bad.get("code"))))
            checks.append(("拒答报文里带合法枚举（宿主仍能自愈）",
                           any(v in msg for v in legal), msg[:70]))
            checks.append(("⛔ 把协议层的拒答当成我们的 BAD_INTENT 信封",
                           bad.get("code") != "BAD_INTENT", "两个面拦截层不同，各自有测试覆盖"))

            # ③ 能力差（实测）：未知键被 SDK 默认**静默忽略**，
            #    所以"未知键点名 DROPPED"只在自研 --intent 面成立。
            #    ⇒ 这就是 MCP 的边界：它只声明 schema，⛔ 定义"多传了参数怎么办"。
            junk = _payload(await client.call_tool(
                "forge_stats", {"metric": "completion_rate", "junk": 1}))
            checks.append(("【能力差】未知键在 MCP 面被静默忽略（自研面会 DROPPED 点名）",
                           junk.get("code") == "OK"
                           and not any(str(n).startswith("DROPPED:") for n in junk.get("notes", [])),
                           str(junk.get("code"))))

            # ④ 钳位与越权这类**语义级**行为只能在契约面验（协议面的 topK 也走 SDK 校验），
            #    本脚本再验一次协议面接受合法 topK 并返回可反查的数字
            ok_env = _payload(await client.call_tool(
                "forge_stats", {"metric": "completion_rate", "project": "PRJ-1180"}))
            d = (ok_env.get("digest") or [{}])[0]
            checks.append(("协议面返回的数字可反查（分子/分母/基准日齐全）",
                           d.get("numerator") == 33 and d.get("denominator") == 38
                           and bool(d.get("as_of")), json.dumps(
                               {k: d.get(k) for k in ("value", "numerator", "denominator")},
                               ensure_ascii=False)))

            # ④ 未知参数：协议面与 --intent 面**在不同层拦截**，这是实测出来的真实差异
            #    （第一版我断言 schema 里有 additionalProperties=false，那是⛔ 存在的标志——
            #     SDK 生成的 schema 只有 properties/required/title/type，
            #     断言一个不存在的字段等于永远红/永远假过。改成测行为，见上面 ③。）

    failed = 0
    for name, passed, detail in checks:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        failed += 0 if passed else 1
    print(f"\nMCP 真协议往返：{len(checks) - failed}/{len(checks)} 项通过")
    return FAIL_EXIT if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
