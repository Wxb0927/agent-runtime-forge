# -*- coding: utf-8 -*-
"""构建前检查：Docker 的数据盘必须在非系统盘，否则拒绝构建。

为什么要有这支脚本（2026-11-23 用户第二次提醒"别占 C 盘"）：镜像层和构建缓存是**几十 GB 级**
的写入，而它落在哪由 Docker Desktop 的设置决定，⛔ 由我在 Dockerfile 里写什么决定。
我在 Dockerfile 里设 TORCH_HOME 之类只管模型缓存，对镜像层毫无作用。
⇒ 所以这一步必须在**构建之前**查，⛔ 事后解释。

跑法：uv run python tools/d6_build_guard.py && docker compose build
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_VM_DISK_HINTS = (
    Path("C:/Users") / os.environ.get("USERNAME", "") / "AppData/Local/Docker/wsl",
)


def docker_disk_locations() -> tuple[Path | None, int]:
    """返回 (疑似数据盘路径, C 侧默认位置已用字节)。"""
    worst = 0
    for hint in DEFAULT_VM_DISK_HINTS:
        if hint.exists():
            for vhdx in hint.rglob("*.vhdx"):
                try:
                    worst = max(worst, vhdx.stat().st_size)
                except OSError:
                    pass
    # WSL 发行版可能被装在 D：看 docker-desktop-data 的注册位置
    try:
        r = subprocess.run(["reg", "query",
                            r"HKCU\Software\Microsoft\Windows\CurrentVersion\Lxss",
                            "/s"], capture_output=True, text=True, timeout=60)
        for line in (r.stdout or "").splitlines():
            if "BasePath" in line:
                base = line.split()[-1].replace("\\", "/")
                if base.lower().startswith("c:") and "docker" in base.lower():
                    return Path(base), worst
    except Exception:  # noqa: BLE001
        pass
    return None, worst


def main() -> int:
    located, c_bytes = docker_disk_locations()
    # ⚠️ 显式写 "C:/"：os.sep / abspath 在 Git-Bash 下会解析到当前盘
    #    （实测把 D 的 753 GiB 当成 C 报了出来 —— 守卫自己报错数就毫无意义）
    c_total = shutil.disk_usage("C:/").total
    print(f"C 盘容量 {c_total / 2**30:.1f} GiB")
    if c_bytes > 500 * 1024 * 1024:
        print(f"[GUARD FAIL] C 侧默认 Docker 盘已有 {c_bytes / 2**30:.2f} GiB 数据 ⇒ "
              "镜像与构建缓存很可能落在 C。请把 Docker Desktop 的 Disk image location "
              "改到 D（Settings → Resources → Advanced），或先跑 docker system df 确认。")
        return 1
    if located is not None:
        print(f"[GUARD FAIL] 发现 docker 的 WSL 注册路径在 C：{located}")
        return 1
    print("[GUARD OK] C 侧未发现 Docker 数据盘（>500MB）⇒ 构建写入落在非系统盘")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
