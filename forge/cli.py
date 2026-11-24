"""命令行入口：三条编排线跑同一个问句，输出同一个信封。

    # 无环范式（公司范式重画）
    uv run python -m forge.cli "PRJ-1180 的进度、里程碑偏差和成本怎么样" --line plan

    # 主流 ReAct（离线打桩模型，⛔ 联网、⛔ 不烧钱）
    uv run python -m forge.cli "PRJ-1180 进度" --line agent

    # 真模型（只有 --live 才走网络；key 只从 .env/环境变量读，⛔ 会被打印）
    uv run python -m forge.cli "PRJ-1180 进度" --line agent --live

    # 人工闸：先跑停住，再批准
    uv run python -m forge.cli "帮我给 bu-beta 立项一个新线体" --line plan
    uv run python -m forge.cli "帮我给 bu-beta 立项一个新线体" --line plan --approve

⚠️ ``--line agent`` 不带 ``--live`` 时用的是**脚本模型**：它只会照脚本调工具，
没有任何推理能力。这一模式下"最终措辞"不是模型给的，输出里会明确标出来，
⛔ 让人误以为离线模式也在做自然语言生成。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from forge.graph import checkpoint, contract, router as router_mod
from forge.graph.compute import AS_OF, dataset_for, parse_as_of
from forge.graph.plan_graph import build_plan_graph
from forge.graph.react_agent import build_react_agent
from forge.graph.react_min import build_react_min
from forge.graph.tools import all_tools
from forge.model.scripted import ScriptedChatModel

SCRATCH = Path("data/cli")
NL = chr(10)  # ⛔ 在源码字面量里写换行转义：脚本改文件时容易被展开成真实换行（实测翻车两次）


def _show(env: dict[str, Any]) -> None:
    print(json.dumps({k: env[k] for k in contract.ENVELOPE_KEYS}, ensure_ascii=False, indent=1))


def run_plan(query: str, *, user: str, seed: int, as_of: str, thread: str,
             approve: bool) -> int:
    """写意图 ⇒ 走"立项准备"长流程（有断点、有闸）；只读 ⇒ 走指标图。

    ⚠️ 早先版本这里⛔ 分流：`--line plan` 问立项也照样回一叠指标，
    README 里"停在人工闸上"就成了假话——**要么把行为补上，⛔ 把话改小**。
    """
    from forge.graph.intake_graph import build_intake_graph, intake_input

    if router_mod.route(query, dataset_for(seed))["write_intent"] or approve:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with checkpoint.saver(SCRATCH / "intake-ckpt.sqlite3") as saver:
            app = build_intake_graph().compile(checkpointer=saver)
            cfg = checkpoint.thread_config(thread)
            payload: Any = ({"decisions": [{"type": "approve"}]} if approve else None)
            if approve:
                from langgraph.types import Command

                payload = Command(resume={"type": "approve"})
            else:
                payload = intake_input(thread_id=thread, user_key=user,
                                       scratch_dir=str(SCRATCH / "intake"))
            out = app.invoke(payload, cfg)
            pending = tuple(app.get_state(cfg).next or ())
        if "__interrupt__" in out:
            gate = out["__interrupt__"][0].value
            print(json.dumps({"stopped_at": list(pending), "gate": gate},
                             ensure_ascii=False, indent=1))
            print("\n[plan] 写操作已暂停，等人工决定：加 --approve 续跑（edit/reject 见 intake_host）")
            return 3
        print(json.dumps({"code": out.get("code"), "outcome": out.get("outcome"),
                          "steps_seen": list(out.get("steps_seen") or []),
                          "notes": list(out.get("notes") or [])}, ensure_ascii=False, indent=1))
        return 0 if out.get("code") == "WRITTEN" else 2

    app = build_plan_graph(scratch_dir=SCRATCH / "plan").compile()
    out = app.invoke({"query": query, "user_key": user, "seed": seed,
                      "as_of": as_of, "thread_id": thread})
    _show(out["envelope"])
    print(f"\n[plan] 节点执行：{' → '.join(out['steps_seen'])}")
    return 0 if out["envelope"]["ok"] else 2


def run_react(which: str, query: str, *, user: str, seed: int, as_of: str,
              thread: str, live: bool, approve: bool, args_model: str | None = None) -> int:
    ds = dataset_for(seed)
    plan = router_mod.route(query, ds)
    skills = plan["skills"]
    if live:
        from forge.model.factory import build_live_model

        model = build_live_model(model=args_model)
        mode = "live"
    else:
        args = {"user_key": user, "as_of": as_of}
        if plan["projects"]:
            args["project"] = plan["projects"][0]
        model = ScriptedChatModel(
            script=[{"tool_calls": [(s, dict(args)) for s in skills]},
                    {"content": "（离线脚本模式：以下指标逐字来自工具返回值，最终措辞应由真模型给出）"}])
        mode = "scripted"
    scratch = SCRATCH / which
    if which == "agent":
        agent, meta = build_react_agent(model=model, skills=skills, scratch_dir=scratch)
        app = agent
    else:
        app, names = build_react_min(model=model, skills=skills, scratch_dir=scratch)
        meta = {"tools": names}

    cfg = checkpoint.thread_config(thread)
    # ⚠️ 宿主注入身份与基准日：这两个值模型⛔ 能知道，也⛔ 该猜（schema 里写的就是
    #    "never inferred"）。实测真模型（qwen3.8-max）在缺这两个值时会**反问用户**而
    #    ⛔ 编一个 u0041 —— 契约设计的"必填不注入就不许猜"在真模型上成立。
    injected = (f"{query}{NL}{NL}（宿主注入，直接用于工具参数，⛔ 自行推断："
                f"user_key={user}，as_of={as_of}）")
    payload: Any = {"messages": [{"role": "user", "content": injected}]}
    if approve:
        payload = __import__("langgraph.types", fromlist=["Command"]).Command(
            resume={"decisions": [{"type": "approve"}]})
    out = app.invoke(payload, cfg)
    msgs = out.get("messages", [])
    rounds = getattr(model, "calls", None)
    if rounds is None:  # 真模型没有计数器，用消息里的 AI 轮次近似
        rounds = sum(1 for m in out.get("messages", []) if type(m).__name__ == "AIMessage")
        rounds = f"{rounds}(按 AIMessage 计)"
    print(f"[{which}/{mode}] 绑定工具={meta['tools']}  模型轮次={rounds}")
    for m in msgs:
        tcs = getattr(m, "tool_calls", None)
        if tcs:
            print(f"  {type(m).__name__}: 调用 {[(c['name'], c['args']) for c in tcs]}")
        elif type(m).__name__ == "ToolMessage":
            print(f"  ToolMessage: {str(m.content)[:180]}")
        else:
            print(f"  {type(m).__name__}: {str(m.content)[:180]}")
    if "__interrupt__" in out:
        print("  ⛔ 停在人工闸上：加 --approve 续跑")
        return 3
    return 0


def run_contract(argv: list[str]) -> int:
    """契约层自查入口：``--describe`` 看菜单、``--intent '{...}'`` 跑一次校验。

    对齐公司那套 CLI 的 ergonomics（技能自带 `--describe`、宿主传 `--intent`），
    这样"说明书由代码吐出来"是可以当场演示的，⛔ 只写在 README 里。
    """
    import json

    from forge.contract import cards as C
    from forge.contract import gates as G
    from forge.contract import intent as I

    if argv and argv[0] == "--describe":
        skill = argv[1] if len(argv) > 1 else None
        print(json.dumps(C.describe(skill), ensure_ascii=False, indent=1))
        return 0
    if argv and argv[0] == "--intent":
        raw = argv[1] if len(argv) > 1 else "{}"
        try:
            intent = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"意图 JSON 解析失败：{exc}")
            return 2
        host = {"user_key": "u0000", "as_of": AS_OF.isoformat(), "seed": 42}
        for pair in argv[2:]:
            k, _, v = pair.partition("=")
            host[k] = int(v) if v.isdigit() else v
        env = I.run(intent, **host)
        print(json.dumps(env, ensure_ascii=False, indent=1))
        return 0 if env["ok"] else 2
    if argv and argv[0] == "--gates":
        return G.main([])
    print("用法: python -m forge.cli --describe [skill] | --intent '{json}' [user_key=.. as_of=..] | --gates")
    return 2


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--describe", "--intent", "--gates"):
        return run_contract(argv)
    ap = argparse.ArgumentParser(prog="forge.cli", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query")
    ap.add_argument("--line", choices=["plan", "agent", "min"], default="plan")
    ap.add_argument("--user", default="u0000")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--as-of", default=AS_OF.isoformat())
    ap.add_argument("--thread", default="cli-1")
    ap.add_argument("--live", action="store_true", help="用真模型（读 FORGE_API_KEY）")
    ap.add_argument("--model", default=None,
                    help="覆盖 FORGE_MODEL（这把百炼子空间的 key 只授权了部分模型，"
                         "默认 qwen-plus 会 403，实测 qwen3.8-max 可用）")
    ap.add_argument("--approve", action="store_true", help="对停在闸上的线程回 approve")
    a = ap.parse_args(argv)
    parse_as_of(a.as_of)  # 早失败：⛔ 让坏日期一路跑到渲染层
    if a.line == "plan":
        return run_plan(a.query, user=a.user, seed=a.seed, as_of=a.as_of,
                        thread=a.thread, approve=a.approve)
    return run_react(a.line, a.query, user=a.user, seed=a.seed, as_of=a.as_of,
                     thread=a.thread, live=a.live, approve=a.approve, args_model=a.model)


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):   # 中文 Windows 控制台默认 cp936：
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")   # 打 ⛔/✅ 会 UnicodeEncodeError
    raise SystemExit(main())
