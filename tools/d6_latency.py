# -*- coding: utf-8 -*-
"""P50/P95/P99 延迟实测（D6）。

⚠️ 报延迟⛔ 不带条件就是编数，所以结果必须写清四件事：
**样本量、并发、trace 后端、是容器还是本机 fork**——**全部来自实测**，
⛔ 硬写在模板里（第一版就硬写了"容器 = 否"，结果我把容器测完了报告还那么说）。

冷启动与热态**分开报**：冷启动的高 P99 不能被抹掉，热态数也不能冒充"首次响应体验"。

跑法：
    uv run python tools/d6_latency.py                      # 自己起一个本机实例
    docker compose up -d && uv run python tools/d6_latency.py --external --port 8000
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
    req = urllib.request.Request(
        f"{base}/v1/ask", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()
    return (time.perf_counter() - t0) * 1000.0


def wait_up(base: str, limit: int = 90) -> bool:
    for _ in range(limit):
        try:
            with urllib.request.urlopen(f"{base}/healthz", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    return False


def batch(base: str, n: int, concurrency: int) -> tuple[list[float], int]:
    samples: list[float] = []
    errors = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for f in [pool.submit(post, base, QUERY) for _ in range(n)]:
            try:
                samples.append(f.result())
            except Exception:  # noqa: BLE001
                errors += 1
    return samples, errors


def percentiles(samples: list[float]) -> tuple[float, float, float, float]:
    ordered = sorted(samples)

    def pct(q: float) -> float:
        idx = max(0, min(len(ordered) - 1, int(round(q / 100.0 * (len(ordered) - 1)))))
        return round(ordered[idx], 1)

    return pct(50), pct(95), pct(99), round(statistics.mean(samples), 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--warmup", type=int, default=15,
                    help="预热请求数：**单独留档**，⛔ 混进热态统计，也⛔ 抹掉不报")
    ap.add_argument("--external", action="store_true",
                    help="压已经在跑的服务（本项目里就是 compose 起的容器）")
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
        if not wait_up(base):
            print("服务没起来 ⇒ 本次无数据（⛔ 编一个 P95）")
            proc.kill()
            return 1

    health = json.loads(urllib.request.urlopen(f"{base}/healthz", timeout=10).read())
    backend = health.get("trace_backend", "?")
    vector_in_use = health.get("vector_in_use")
    # 形态来自参数：--external 在本项目里就是压 compose 起的容器
    form = "compose 起的容器（外部服务）" if a.external else "本机 fork 的单实例 uvicorn"

    cold, cold_err = batch(base, max(a.warmup, 0), a.concurrency) if a.warmup else ([], 0)
    samples, errors = batch(base, a.n, a.concurrency)
    if not samples:
        print("一条都没成功 ⇒ ⛔ 报延迟")
        if proc:
            proc.kill()
        return 1

    p50, p95, p99, mean = percentiles(samples)
    lines = [
        f"# 服务延迟实测（{time.strftime('%Y-%m-%d')}）", "",
        f"- 形态：**{form}** · trace 后端 = **{backend}** · 向量库实际启用 = {vector_in_use}"
        f"（均来自 `/healthz` 实测，⛔ 写死在模板里）",
        f"- 并发 **{a.concurrency}** · 热态样本 **{len(samples)}**（失败 {errors}）· "
        f"预热 {len(cold)} 次（失败 {cold_err}）**不计入**热态",
        f"- 问句：`{QUERY['query']}`（三分支全跑，digest 10 项）", "",
        "## 热态（预热后）", "",
        f"| P50 | P95 | P99 | 均值 |",
        f"|---|---|---|---|",
        f"| {p50} ms | **{p95} ms** | {p99} ms | {mean} ms |", "",
        "## 冷启动（单独留档）", "",
    ]
    if cold:
        c50, c95, c99, cmean = percentiles(cold)
        lines += [f"首 {len(cold)} 次请求：P50 {c50} ms · P95 {c95} ms · P99 {c99} ms · "
                  f"均值 {cmean} ms · 最大 {round(max(cold), 1)} ms", "",
                  "冷启动里包含数据集构建与首次导入，所以 P99 明显偏高。"
                  "**这栏⛔ 被抹掉，也⛔ 混进热态数**——两个数各有各的说法场合。"]
    else:
        lines.append("（本次未采样）")

    lines += ["", "⚠️ 引用延迟必须带上「形态 + 并发 + 样本量 + 后端」这一行，"
              "否则 P95 会被读成生产容量承诺。本机测的是**单实例、无鉴权、无网络跳数**，"
              "容器那一档也只到'单机 compose'，⛔ 等于压测集群。"]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    print("\n".join(lines))
    if proc:
        proc.kill()
        proc.wait(timeout=20)
    return 0


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):   # 中文 Windows 控制台默认 cp936：
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")   # 打 ⛔/✅ 会 UnicodeEncodeError
    raise SystemExit(main())
