"""模型与依赖缓存的定位：**必须在 import 重库之前调用**。

为什么单独一个模块：HuggingFace / sentence-transformers / torch 都在**导入时**读环境变量
决定缓存根，等看到 "Downloading …" 再设已经晚了（默认落 `C:\\Users\\<you>\\.cache\\huggingface`）。
2026-11-22 用户明确要求：本项目所有下载物⛔ 占 C 盘。

三条约定：

1. **根目录 = `FORGE_CACHE_ROOT`**，未设时默认 `D:/uv`；所有子路径由这里拼，
   ⛔ 在各模块里散落硬编码。
2. **⛔ 覆盖已有设置**（`setdefault` 语义）：用户或 CI 显式设过就尊重。
   实测本机 `HF_HOME=D:\\ai-cache\\huggingface`、`TORCH_HOME=D:\\ai-cache\\torch` 已存在 ⇒ 保留。
3. `audit()` / `guard_or_fail()` 给测试与 README 用：谁指向系统盘，下载前就失败。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_ROOT = "D:/uv"
MANAGED = "FORGE_CACHE_ROOT"

#: 变量名 → 相对根目录的子路径（⛔ 在这里写绝对路径）
ENDPOINT_VARS = ("HF_ENDPOINT",)

VARS: dict[str, str] = {
    "HF_HOME": "hf",
    "HUGGINGFACE_HUB_CACHE": "hf/hub",
    "SENTENCE_TRANSFORMERS_HOME": "st",
    "TORCH_HOME": "torch",
    "XDG_CACHE_HOME": "cache",
    "NUMBA_CACHE_DIR": "numba",
    # ⚠️ 这三个是后补的：只钉 HF/torch/uv 会漏掉**所有走 tempfile.gettempdir() 的库**。
    #    实测 jieba 把词典缓存写在 `C:\Users\<you>\AppData\Local\Temp\jieba.cache`
    #    —— 单个文件不大，但"默认落 C 盘"这件事本身就是用户明确禁止的。
    "TMPDIR": "tmp",
    "TEMP": "tmp",
    "TMP": "tmp",
}


#: 模型下载端点。默认走 hf-mirror（国内直连 hf.co 实测慢到不可用：15 MB 的
#: numpy 都要 15 分钟，几百 MB 的模型更别提），已设过的值⛔ 覆盖，CI 想走官方站
#: 就显式设 HF_ENDPOINT=https://huggingface.co。
DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"


#: 临时目录：Windows 上 TMP/TEMP 是**系统预设**（指向 C 盘），把它算成"用户显式设过"
#: 就会让 guard 永远拦死自己（实测：`RuntimeError: 模型缓存指向系统盘… {'TEMP': …}`）。
#: ⇒ 规则分两类：缓存/端点类**尊重已有值**，临时目录类**该覆盖就覆盖**
#:   （只影响本进程；想改回去显式设 FORGE_KEEP_TEMP=1）。
TEMP_VARS = ("TMPDIR", "TEMP", "TMP")


def _looks_system(val: str) -> bool:
    v = (val or "").replace("\\", "/").lower()
    home = str(Path.home()).replace("\\", "/").lower()
    return (not v) or v.startswith(home) or v.startswith("c:/users")


def root() -> Path:
    return Path(os.environ.get(MANAGED) or DEFAULT_ROOT)


def configure_caches(environ: dict[str, str] | None = None) -> dict[str, str]:
    """把缓存根设到非系统盘。返回**本次实际生效**的映射（含用户原有的）。"""
    env = environ if environ is not None else os.environ
    base = root()
    applied: dict[str, str] = {}
    # 用户已经设了 HF_HOME ⇒ ⛔ 再造 HUGGINGFACE_HUB_CACHE，否则模型会落到两个不同的根
    # （hub 缓存优先认 HUGGINGFACE_HUB_CACHE，那等于把用户既有的 HF_HOME 架空了）
    skip = {"HUGGINGFACE_HUB_CACHE"} if env.get("HF_HOME") else set()
    for key, sub in VARS.items():
        if key in skip:
            continue
        if key not in env:
            env[key] = str((base / sub).as_posix())
        applied[key] = env[key]
    env.setdefault("HF_ENDPOINT", DEFAULT_HF_ENDPOINT)
    applied["HF_ENDPOINT"] = env["HF_ENDPOINT"]   # ⚠️ 也要进返回值：⛔ 只写 os.environ 会让调用方读到 None
    if env.get("FORGE_KEEP_TEMP") != "1":
        for key in TEMP_VARS:                     # 临时目录：系统默认值该被覆盖（见上面注释）
            if _looks_system(env.get(key, "")):
                env[key] = str((base / "tmp").as_posix())
            applied[key] = env[key]
    env.setdefault(MANAGED, str(base))
    return applied


def audit(applied: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """每个缓存变量：路径、⛔ 是否指向系统盘（home 或 C:）、是否存在。"""
    env = applied or {k: os.environ.get(k, "") for k in list(VARS) + list(ENDPOINT_VARS)}
    home = str(Path.home()).lower().replace("\\", "/")
    out: dict[str, dict[str, Any]] = {}
    for key, val in env.items():
        if key in ENDPOINT_VARS and not str(val).startswith(("c:/", "C:/")):
            out[key] = {"path": val, "on_system_drive": False, "exists": True}
            continue
        norm = (val or "").replace("\\", "/").lower()
        p = Path(val) if val else None
        on_home = bool(norm) and (norm.startswith(home) or norm.startswith("c:/users"))
        out[key] = {"path": val, "on_system_drive": on_home, "exists": bool(p and p.exists())}
    return out


def guard_or_fail() -> dict[str, dict[str, Any]]:
    """重库 import 前的自检：缓存指向系统盘 ⇒ 立刻失败，⛔ 静默往 C 盘下几百 MB。"""
    configure_caches()
    report = audit()
    bad = {k: v["path"] for k, v in report.items() if v["on_system_drive"]}
    if bad:
        raise RuntimeError(
            f"模型缓存指向系统盘，已中止下载（要求：全部落 D 盘）。请清掉这些变量或设 {MANAGED}：{bad}")
    return report


if __name__ == "__main__":
    import json

    print(json.dumps({"root": str(root()), "audit": guard_or_fail()}, ensure_ascii=False, indent=1))
    print("python:", sys.version.split()[0])
