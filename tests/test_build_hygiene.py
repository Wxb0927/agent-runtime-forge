# -*- coding: utf-8 -*-
"""依赖与构建形态的自检。

⚠️ 为什么需要这个文件：我在 README 里写过"torch 已绑 CPU 索引"，
后来实测 `[tool.uv.sources]` **对传递依赖不生效**（torch 是 sentence-transformers
带进来的），也就是说那句"配置保证"其实没保证任何东西。
⇒ 结论性的事实一律用断言钉，⛔ 靠配置或靠我记得。
"""
from __future__ import annotations

import importlib.util


def test_torch_is_the_cpu_build_when_present() -> None:
    """装到 torch 就必须是 CPU 构建（PyPI 默认那枚 cu121 约 2.5 GB，白占空间）。"""
    if not importlib.util.find_spec("torch"):
        return  # semantic extra 未装 ⇒ 本断言不适用（也⛔ 假装通过为"已验证"）
    import torch

    assert torch.version.cuda is None, (
        f"装到的是 CUDA 构建 {torch.__version__}；本仓库只做 CPU 推理，"
        "请用 uv sync --extra semantic 配 CPU 索引重装")
    assert torch.__version__.endswith("+cpu"), torch.__version__


def test_uv_config_has_no_shadowed_index_block() -> None:
    """uv.toml 存在时 pyproject 的 [tool.uv].index 会被**静默忽略**（实测有 warning）。

    两处配同一件事必然分叉，所以这里断言重复定义已经被清掉。
    """
    from pathlib import Path

    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    has_uv_toml = Path("uv.toml").exists()
    if has_uv_toml:
        assert "[[tool.uv.index]]" not in pyproject, (
            "uv.toml 与 pyproject 同时定义索引 ⇒ pyproject 那份会被忽略，"
            "改它毫无效果还留着当'看起来配好了'。删掉一处。")
