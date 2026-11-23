# -*- coding: utf-8 -*-
"""P50/P95/P99 延迟实测（D6）。

⚠️ 报延迟⛔ 不带条件就是编数，所以结果里必须写清四件事：
**样本量、并发、后端（sqlite/postgres）、是否容器内**。
本机默认这组是"单实例 uvicorn + sqlite + 非容器"，与 compose 那一档不是一个数，
因此简历里引哪一个要按**实际部署形态**说，⛔ 混。

跑法：uv run python tools/d6_latency.py [--n 120] [--concurrency 4] [--port 8899]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = Path("eval/latency.md")
QUERY = {"query": "PRJ-1180 的进度、偏差和成本", "user_key": "u0041"}


def post(base: str, body: dict[str, object], timeout: float = 30.0) -> float:
    req = urllib.request.Request(f"{base}/v1/ask", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()
    return (time.perf_counter() - t0) * 1000.0


def wait(base: str, limit: int = 90) -> bool:
    for _ in range(limit):
        try:
            with urllib.request.urlopen(f"{base}/healthz", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--external", action="store_true",
                    help="压已经在跑的服务（例如 compose 起的容器），⛔ 再 fork 一个")
    a = ap.parse_args()
    base = f"http://127.0.0.1:{a.port}"

    proc = None
    if not a.external:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO)
        proc = subprocess.Popen([sys.executable, "-X", "utf-8", "-m", "uvicorn",
                                 "server.app:app", "--host", "127.0.0.1", "--port", str(a.port),
                                 "--log-level", "error"], cwd=str(REPO), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        if not wait(base):
            print("服务没起来 ⇒ 本次无数据（⛔ 编一个 P95）")
            if proc:
                proc.kill()
            return 1

    health = json.loads(urllib.request.urlopen(f"{base}/healthz", timeout=10).read())
    backend = health.get("trace_backend", "?")

    samples: list[float] = []
    errors = 0
    with ThreadPoolExecutor(max_workers=a.concurrency) as pool:
        futs = [pool.submit(post, base, QUERY) for _ in range(a.n)]
        for f in futs:
            try:
                samples.append(f.result())
            except Exception:  # noqa: BLE001
                errors += 1
    if not samples:
        print("一条都没成功 ⇒ ⛔ 报延迟")
        if proc:
            proc.kill()
        return 1

    ordered = sorted(samples)

    def pct(q: float) -> float:
        idx = max(0, min(len(ordered) - 1, int(round(q / 100.0 * (len(ordered) - 1)))))
        return round(ordered[idx], 1)

    p50, p95, p99 = pct(50), pct(95), pct(99)
    text = "\n".join([
        f"# 服务延迟实测（{time.strftime('%Y-%m-%d')}）", "",
        f"- 形态：**本机单实例 uvicorn，trace 后端 = {backend}，容器 = 否**，"
        f"并发 {a.concurrency}，样本 {len(samples)}（失败 {errors}）",
        f"- P50 = **{p50} ms** · P95 = **{p95} ms** · P99 = {p99} ms · 均值 "
        f"{round(statistics.mean(samples), 1)} ms",
        f"- 问句：`{QUERY['query']}`（三分支全跑，digest 10 项）", "",
        "⚠️ 这组数**⛔ 代表生产**：单实例、sqlite、无网络跳转、模型走打桩。"
        "compose 那一档（Postgres + Qdrant + 真模型）要另测：`docker compose up -d && "
        "uv run python tools/d6_latency.py --external --port 8000`。",
        "⚠️ 引用时必须带上形态那句，否则 P95 会被读成生产承诺。",
    ]) + "\n"
    OUT.write_text(text, encoding="utf-8", newline="")
    print(text)
    if proc:
        proc.kill()
        proc.wait(timeout=20)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
