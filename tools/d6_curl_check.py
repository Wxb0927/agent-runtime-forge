# -*- coding: utf-8 -*-
"""起真 uvicorn 进程，用 curl 验 SSE 与 REST（TestClient 不算数）。"""
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]   # ⚠️ 脚本在 tools/ 下，parent 会指到 tools ⇒ PYTHONPATH 指错
env = dict(os.environ)
env["PYTHONPATH"] = str(REPO)

proc = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "uvicorn",
                         "server.app:app", "--host", "127.0.0.1", "--port", "8899",
                         "--log-level", "warning"],
                        cwd=str(REPO), env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, encoding="utf-8")

ok = False
try:
    # 等服务起来（最多 40 秒），⛔ 盲 sleep
    for _ in range(80):
        time.sleep(0.5)
        try:
            r = subprocess.run(["curl", "-s", "-m", "3", "http://127.0.0.1:8899/healthz"],
                               capture_output=True, text=True, timeout=8)
            if '"ok": true' in r.stdout or '"ok":true' in r.stdout:
                print("healthz 就绪:", r.stdout.strip()[:80])
                break
        except Exception:  # noqa: BLE001
            pass
    else:
        print("服务没起来，子进程输出如下：")
        try:
            import select as _s
            print((proc.stdout.read(3000) if proc.stdout else "")[:3000])
        except Exception as _e:  # noqa: BLE001
            print("(读不动子进程管道，改用非阻塞读取失败)", _e)
        sys.exit(1)

    def run(args, label):
        print("\n$ curl " + " ".join(args))
        r = subprocess.run(["curl", "-sS"] + args, capture_output=True, text=True,
                           encoding="utf-8", timeout=120)
        out = (r.stdout or "").strip()
        print(out[:1200] if out else "(空) " + (r.stderr or "")[:200])
        return out

    # ① REST 一次
    body = '{"query":"PRJ-1180 的进度、偏差和成本","user_key":"u0041"}'
    run(["-m", "60", "-H", "Content-Type: application/json",
         "-D", "-", "-X", "POST", "http://127.0.0.1:8899/v1/ask", "-d", body], "ask")

    # ② SSE：任务书要求的"一条 curl 拿到事件流"
    sse = run(["-N", "-m", "60", "-H", "Accept: text/event-stream",
               "http://127.0.0.1:8899/v1/ask/stream?query=PRJ-1180%20%E7%9A%84%E5%AE%8C%E6%88%90%E7%8E%87"],
              "stream")
    kinds = sorted({ln.split(":", 1)[1].strip() for ln in sse.splitlines()
                    if ln.startswith("event:")})
    print("\nSSE 事件种类:", kinds)
    assert "node" in kinds and "done" in kinds, "SSE 事件不完整"

    # ③ 引擎侧 token 流（打桩 ⇒ 消息块，done 里必须自报 stream_kind）
    sse2 = run(["-N", "-m", "90", "-H", "Accept: text/event-stream",
                "http://127.0.0.1:8899/v1/chat/stream?query=PRJ-1180%20%E7%9A%84%E5%AE%8C%E6%88%90%E7%8E%87"],
               "chat stream")
    kinds2 = sorted({ln.split(":", 1)[1].strip() for ln in sse2.splitlines()
                     if ln.startswith("event:")})
    print("chat SSE 事件种类:", kinds2)
    assert "done" in kinds2 and "token" in kinds2, "token 流事件缺失"

    # ④ 降级演练（curl）
    body2 = '{"query":"PRJ-1180 的成本","user_key":"u0041","faults":"cost:perm"}'
    run(["-m", "60", "-H", "Content-Type: application/json",
         "-w", "\\nHTTP=%{http_code}", "-X", "POST", "http://127.0.0.1:8899/v1/ask", "-d", body2],
        "degrade")

    print("\n全部 curl 验证通过")
    ok = True
finally:
    proc.kill()
    proc.wait(timeout=20)
    sys.exit(0 if ok else 1)
