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


def test_license_claim_and_artifact_cannot_diverge() -> None:
    """README 写「MIT（见 LICENSE）」⇒ LICENSE 必须真在仓库里，且两边说的是同一个许可证。

    这条和 `test_basetemp_parent_is_tracked_so_fresh_clone_works` 同形：
    断言的是**仓库自身的可交付物存在**，本机永远有这个文件所以我永远看不见它缺失。
    许可证一旦缺失，公开仓库的默认法律状态是"⛔ 任何人可复用"，
    而 README 那句"可以放心复制、改写、用于商业用途"就从说明变成了误导。
    """
    from pathlib import Path

    lic = Path("LICENSE")
    assert lic.exists(), "LICENSE 缺失：README 声称 MIT 却没有许可证正文"
    license_text = lic.read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "MIT License" in license_text, "LICENSE 不是 MIT 正文"
    assert "MIT" in readme, "README 声称 MIT，正文里却找不到这个词"
    # 只有一处许可证声明：pyproject 若写了别的许可证，公开时 GitHub 会以 SPDX 字段为准
    import tomllib

    proj = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    claimed = proj.get("project", {}).get("license")
    assert claimed in (None, "MIT", {"spdx_id": "MIT"}), (
        f"pyproject 的 license={claimed!r} 与 LICENSE(MIT) 不一致")
