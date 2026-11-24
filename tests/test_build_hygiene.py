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


def test_mutation_inventory_total_is_derived_not_hand_summed() -> None:
    """对外报的"变异 N 条"必须由分组清单派生，⛔ 手拼列表。

    2026-11-24 发布复验的真事故：`ALL_MUTATIONS = ... + MUTATIONS_D4` 写了两遍
    ⇒ README 与简历上的 **40/40 其实是 34 条**（D4 那 6 条跑了两遍，
    还被索引式分组标签标成 D6）。检查"汇总数会不会说谎"的工具自己说了谎，
    而且说的还是**最容易被引用成简历数字**的那一句。
    ⇒ mutation_check 里已有三条 import 期断言，这里是**pytest 侧的门禁**：
      断言只长在跑工具的脑子里，没人跑工具时它就⛔ 存在。
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import mutation_check as MC

    names = [m[0] for m in MC.ALL_MUTATIONS]
    assert len(names) == len(set(names)), f"变异条目重复 ⇒ 总数虚高：{len(names)} vs {len(set(names))}"
    assert len(names) == sum(len(ms) for _, ms in MC.GROUPS), "分组明细之和 != 总数"
    assert set(MC.GROUP_OF) == set(names), "分组标签表与清单不同步"
    assert len(names) >= 30, f"只数出 {len(names)} 条 ⇒ 扫描/清单本身坏了（控制组）"


def test_every_binary_artifact_has_an_explicit_gitattributes_rule() -> None:
    """仓库里每个二进制文件都必须被 .gitattributes 显式标 binary。

    为什么⛔ 靠 `* text=auto` 的自动嗅探：它只看首 8000 字节，而 **GIF 的头部全是 ASCII**
    （`GIF89a` + 调色板），一旦被嗅成 text，Windows clone 的 CRLF 翻译会**逐字节改坏它**——
    坏掉的 GIF 在 GitHub 上就是"Demo 图⛔ 出来"。README 的三条命令跑得通⛔ 等于门面完好，
    这是第二条发布事故路径，所以钉成断言。
    """
    import subprocess
    from pathlib import Path

    ROOT = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT,
                             capture_output=True, text=True).stdout.splitlines()
    attrs = (ROOT / ".gitattributes").read_text("utf-8")
    exts = {Path(t).suffix.lower() for t in tracked if Path(t).suffix}
    suspect = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".webm", ".mp4", ".pdf"}
    missing = sorted(e for e in exts & suspect if f"*{e} binary" not in attrs)
    assert not missing, f"这些二进制后缀没有显式 binary 规则：{missing}"
