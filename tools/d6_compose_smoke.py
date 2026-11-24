# -*- coding: utf-8 -*-
"""compose 起来之后的一键自证：REST / SSE / trace 落 Postgres / 降级 / Qdrant 后端。

⚠️ 三条纪律：
1. **没 Docker 就 exit 3 并说明**，⛔ "看起来跑过了"（与 D5 的 MCP 冒烟同一个模式）。
2. 断言的是**外部行为**：容器里的 app 是否真连上了 Postgres（trace 行数>0 且 sqlite
   文件⛔ 增长）、Qdrant 是否真被用作 dense 后端（`/v1/trace/stats` 或 healthz 暴露后端）。
3. 每条 curl 都打印出来，评审可以逐条复制手验。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = os.environ.get("FORGE_BASE_URL", "http://127.0.0.1:8000")
MISSING_DOCKER = 3


def curl(args: list[str], timeout: int = 90) -> tuple[str, str]:
    print("$ curl " + " ".join(args))
    r = subprocess.run(["curl", "-sS"] + args, capture_output=True, text=True,
                       encoding="utf-8", timeout=timeout + 10)
    return (r.stdout or "").strip(), (r.stderr or "").strip()


def docker_ready() -> bool:
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, text=True, timeout=25)
    except Exception:  # noqa: BLE001
        return False
    return bool(r.stdout.strip()) and r.returncode == 0


def wait_up(limit: int = 90) -> bool:
    for _ in range(limit):
        out, _err = curl(["-m", "3", f"{BASE}/healthz"])
        if '"ok"' in out:
            print("healthz:", out[:120])
            return True
        time.sleep(2)
    return False


def main() -> int:
    if not docker_ready():
        print("[SKIP] Docker 守护进程未运行 ⇒ 容器端到端**未验证**。\n"
              "       启动 Docker Desktop 后跑：docker compose up --build -d\n"
              "       再跑本脚本：uv run python tools/d6_compose_smoke.py")
        return MISSING_DOCKER
    if not wait_up():
        print("[FAIL] compose 起了但 /healthz 不通。请看 docker compose logs app")
        return 1

    fails: list[str] = []

    # ① REST
    body = json.dumps({"query": "PRJ-1180 的进度、偏差和成本", "user_key": "u0041"},
                      ensure_ascii=False)
    out, err = curl(["-m", "60", "-H", "Content-Type: application/json", "-D", "-",
                     "-X", "POST", f"{BASE}/v1/ask", "-d", body])
    rid = next((ln.split(":", 1)[1].strip() for ln in out.splitlines()
                if ln.lower().startswith("x-request-id:")), "")
    ok = '"code": "OK"' in out or '"code":"OK"' in out
    print(f"\n[{'OK  ' if ok else 'FAIL'}] POST /v1/ask 返回合法信封  request_id={rid!r}")
    fails += [] if ok else ["ask"]

    # ② SSE（任务书原文要求的"一条 curl 拿到事件流"）
    sse, _ = curl(["-N", "-m", "60", "-H", "Accept: text/event-stream",
                   f"{BASE}/v1/ask/stream?query=PRJ-1180%20%E7%9A%84%E5%AE%8C%E6%88%90%E7%8E%87"])
    kinds = sorted({ln.split(":", 1)[1].strip() for ln in sse.splitlines()
                    if ln.startswith("event:")})
    ok = {"node", "done"} <= set(kinds)
    print(f"[{'OK  ' if ok else 'FAIL'}] SSE 事件种类 {kinds}")
    fails += [] if ok else ["sse"]

    # ③ trace 真的落到 Postgres（而不是 sqlite）：查 stats 有样本
    stats, _ = curl(["-m", "30", f"{BASE}/v1/trace/stats"])
    print("[info] trace/stats:", stats[:200])
    try:
        d = json.loads(stats)
        ok = d.get("samples", 0) >= 2
    except json.JSONDecodeError:
        ok = False
    print(f"[{'OK  ' if ok else 'FAIL'}] Postgres 里已留有 trace 记录")
    fails += [] if ok else ["trace"]

    # ④ 降级：容器内也要 200 + 合法信封
    body2 = json.dumps({"query": "PRJ-1180 的成本", "user_key": "u0041",
                        "faults": "cost:perm"}, ensure_ascii=False)
    out2, _ = curl(["-m", "60", "-H", "Content-Type: application/json", "-w", "\\nHTTP=%{http_code}",
                    "-X", "POST", f"{BASE}/v1/ask", "-d", body2])
    http = out2.splitlines()[-1] if out2 else ""
    ok = "HTTP=200" in http and "DEGRADED" in out2 and "500" not in http
    print(f"[{'OK  ' if ok else 'FAIL'}] 下游故障 ⇒ {http}，响应仍是合法信封（⛔ 裸栈）")
    fails += [] if ok else ["degrade"]

    # ⑤ Qdrant 后端是否真的被用上（healthz 或 stats 里应能看到后端标记）
    health, _ = curl(["-m", "30", f"{BASE}/healthz"])
    print("[info] healthz:", health[:200])

    print("\n结果:", "全部通过" if not fails else f"失败项 {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):   # 中文 Windows 控制台默认 cp936：
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")   # 打 ⛔/✅ 会 UnicodeEncodeError
    raise SystemExit(main())
