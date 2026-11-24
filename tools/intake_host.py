#!/usr/bin/env python
"""立项准备图的一次**进程生命周期**：跑一次 → 若停在闸上就退出/挂住 → 换进程再续。

存在的唯一理由：**跨进程续跑必须用真进程验证**。同进程里换个 ``thread_id`` 再 invoke
证明不了 checkpoint 能活过进程死亡（对象还挂在内存里，怎么测都过）。

    uv run python -X utf8 tools/intake_host.py --scratch data/intake-demo --thread t1
    uv run python -X utf8 tools/intake_host.py --scratch data/intake-demo --thread t1 \
        --resume '{"type":"approve"}'

⚠️ ⛔ --resume 时再传问句：恢复的语义是"从最后一个 checkpoint 接着走"，
   输入是人的决定，⛔ 新的用户消息。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forge.graph import checkpoint                      # noqa: E402
from forge.graph.intake_graph import build_intake_graph, intake_input  # noqa: E402
from langgraph.types import Command                     # noqa: E402


def counts(scratch: Path) -> dict[str, int]:
    out = {}
    for name in ("collect", "verify"):
        p = scratch / f"{name}.count"
        out[name] = int(p.read_text(encoding="utf-8")) if p.exists() else 0
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scratch", required=True)
    ap.add_argument("--thread", required=True)
    ap.add_argument("--resume", default=None, help='JSON: {"type":"approve|edit|reject", ...}')
    ap.add_argument("--continue", dest="cont", action="store_true",
                    help="不带新输入地续跑：invoke(None)，让挂起的节点自己重放"
                         "（⚠️ 传新 input 不是续跑，那是**开新一轮**，已完成的节点会重跑）")
    ap.add_argument("--verify-seconds", type=float, default=0.0)
    ap.add_argument("--hold", action="store_true", help="停在闸上不退（供硬杀测试）")
    ap.add_argument("--crash-in-verify", type=float, default=-1.0,
                    help="在 verify 节点内活这么久之后 os._exit(9)：决定性死点")
    ap.add_argument("--user", default="u0041")
    a = ap.parse_args(argv)

    scratch = Path(a.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    with checkpoint.saver(scratch / "ckpt.sqlite3") as saver:
        # ⚠️ checkpointer 在 compile 时绑定，⛔ 事后 attach
        graph = build_intake_graph(verify_seconds=a.verify_seconds,
                                crash_in_verify=a.crash_in_verify).compile(checkpointer=saver)
        cfg = checkpoint.thread_config(a.thread)
        if a.resume is not None:
            payload: object = Command(resume=json.loads(a.resume))
        elif a.cont:
            payload = None            # ← 真·断点续跑：从最后一个 checkpoint 的待执行任务继续
        else:
            payload = intake_input(thread_id=a.thread, user_key=a.user,
                                   scratch_dir=str(scratch))
        out = graph.invoke(payload, cfg)
        # ⚠️ 必须在 with 块内读状态：退出上下文后 sqlite 连接已关，get_state 会炸
        pending = tuple(graph.get_state(cfg).next or ())

    interrupted = "__interrupt__" in out
    print("STATUS " + json.dumps({
        "pid": __import__("os").getpid(),
        "interrupted": interrupted,
        "pending_node": list(pending),
        "steps_seen": list(out.get("steps_seen") or []),
        "decisions": list(out.get("decisions") or []),
        "outcome": out.get("outcome"),
        "code": out.get("code"),
        "counts": counts(scratch),
        "draft_written": (scratch / f"draft-{a.thread}.json").exists(),
    }, ensure_ascii=False), flush=True)
    # ⚠️ flush=True 是**必需**的：stdout 被重定向成管道/文件时 Python 用块缓冲（8 KB），
    #    --hold 模式下面还要 while True 睡下去，STATUS 那行会永远卡在缓冲区里，
    #    父进程只能等到超时。第一版整个套件"挂死"就是这个原因，⛔ 不是框架的问题。
    if interrupted and a.hold:
        while True:  # 故意挂住：测试会 kill() 掉这个进程，验证 checkpoint 活得过进程死亡
            time.sleep(0.2)
    return 0


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):   # 中文 Windows 控制台默认 cp936：
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")   # 打 ⛔/✅ 会 UnicodeEncodeError
    raise SystemExit(main())
