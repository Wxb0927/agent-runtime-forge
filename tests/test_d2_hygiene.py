"""卫生检查：临时文件不许偷偷写系统盘，仓库里不许出现密钥与雇主指纹。

这几条都是**元测试**——它们不测业务逻辑，测的是"这个项目有没有污染用户机器 /
有没有把不该开源的东西带进仓库"。2026-11-17 的教训：pytest 的 ``tmp_path`` 默认落在
C 盘用户 Temp 里，本项目每个测试都要往里写 sqlite checkpoint，⛔ 有人会发现得太晚。
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 脱敏清单：出现任何一条，仓库就不能公开（雇主指纹 / 真实字段名 / 真实人名手机号）。
#
# ⚠️ **必须拼出来，⛔ 直接写成字面量**——这张清单本身要被下面的扫描器读，
# 写成字面量的话，仓库一旦公开，"禁词表"自己就成了泄露源（第一版真的就是这样：
# 扫描器唯一的命中就是它自己）。拼接后任何 contiguous grep 都抓不到，而检查照样生效。
def _t(*parts: str) -> str:
    return "".join(parts)


BANNED_TOKENS = (
    _t("men", "techs"), _t("infor", "mat"), _t("cp", "dl"), _t("pms", "_"),
    _t("belong", "Bu"), _t("payment", "Json"),
    _t("孟", "腾"), _t("易", "之德"), _t("H", "RM"), _t("Q", "MS"),
    _t("王", "晓斌"), _t("189", "3911", "767", "5"),
)
SOURCE_DIRS = ("forge", "tests", "tools", "data", "contracts", "eval")
SOURCE_FILES = ("README.md", "pyproject.toml", "uv.toml", ".gitignore", "uv.lock")

#: 用户目录路径。捕获组是要检查的"用户名"那一段，占位写法（`<you>` 等）在断言里放过。
USER_PATH_RE = re.compile(r"C:\\+Users\\+([A-Za-z0-9._-]{1,40})", re.IGNORECASE)


def _iter_text_files():
    for d in SOURCE_DIRS:
        for p in (ROOT / d).rglob("*"):
            if p.is_file() and p.suffix in {".py", ".md", ".toml", ".json", ".txt"} \
                    and "__pycache__" not in p.parts:
                yield p
    for f in SOURCE_FILES:
        p = ROOT / f
        if p.is_file():
            yield p


def test_tmp_path_lands_on_the_project_drive(tmp_path: Path) -> None:
    """每个测试的临时目录都必须在项目盘内（pyproject 里钉了 ``basetemp``）。"""
    got = tmp_path.resolve()
    assert got.is_relative_to(ROOT.resolve()), \
        f"tmp_path 跑到了 {got}，会把 sqlite/草稿写进系统盘"


def test_uv_cache_is_not_on_the_system_drive() -> None:
    cfg = tomllib.loads((ROOT / "uv.toml").read_text(encoding="utf-8"))
    cache = Path(cfg["cache-dir"]).resolve()
    assert not str(cache).startswith(str(Path.home().resolve())), \
        f"uv 缓存指回用户目录了：{cache}"


def test_repo_contains_no_company_fingerprints() -> None:
    hits: list[str] = []
    for p in _iter_text_files():
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        low = text.lower()
        for tok in BANNED_TOKENS:
            if tok.lower() in low:
                hits.append(f"{p.relative_to(ROOT)}: {tok}")
    assert not hits, "仓库里出现雇主指纹，⛔ 能公开：\n" + "\n".join(hits)


def test_repo_contains_no_api_keys() -> None:
    # ⚠️ 左边界的 ^/非字母数字是必要的：⛔ 写成 r"sk-..." 会把 task- / risk-level-2026
    #    这类普通词当密钥报（`git log -S"sk-"` 就在 task_ 上误报过一次，实测）。
    pat = re.compile(r"(?:^|[^A-Za-z0-9])sk-[A-Za-z0-9._-]{6,}")
    hits = []
    for p in _iter_text_files():
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in pat.finditer(text):
            hits.append(f"{p.relative_to(ROOT)}: {m.group()[:8]}…")
    assert not hits, "疑似把密钥写进了文件：" + ", ".join(hits)
    env = ROOT / ".env"
    if env.exists():  # .env 只允许存在于本地，⛔ 被 git 跟踪
        import subprocess

        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"],
                                 cwd=ROOT, capture_output=True, text=True).returncode
        assert tracked != 0, ".env 被 git 跟踪了，立刻 git rm --cached"


def test_no_legacy_hardware_names_in_state_keys() -> None:
    """状态通道名必须是通用词（company>bu>subline>member 的档名沿用 D1 的枚举，
    但⛔ 出现真实角色键/权限码）。"""
    from forge.domain.models import ScopeTier

    assert [t.value for t in ScopeTier] == ["company", "bu", "subline", "member"]
    from forge.graph.state import BRANCH_SOURCE

    assert set(BRANCH_SOURCE.values()) == {"task_table", "milestone_table", "cost_docs"}

def test_temp_dir_is_not_on_the_system_drive() -> None:
    """HF/torch/uv 之外，任何走 tempfile.gettempdir() 的库也会写 C 盘（实测 jieba）。"""
    from forge import env

    applied = env.configure_caches({})
    home = str(Path.home()).lower()
    for key in ('TMPDIR', 'TEMP', 'TMP'):
        val = applied[key].lower().replace(chr(92), '/')
        assert not val.startswith(home.lower().replace(chr(92), '/')), (key, val)
        assert not val.startswith('c:/users'), (key, val)

def test_basetemp_parent_is_tracked_so_fresh_clone_works() -> None:
    """全新 clone 必须能直接 `uv run pytest`。

    2026-11-23 的 fresh-clone 验证抓到：`.tmp/` 整个被 gitignore ⇒ clone 后父目录不存在，
    而 pytest 的 `--basetemp=.tmp/pytest` 只 mkdir 一层 ⇒ **35 个 error**。
    本机一直有这个目录所以我从没看见。⇒ 占位文件 `.tmp/.keep` 必须被跟踪。
    """
    assert (ROOT / ".tmp" / ".keep").exists(), (
        "缺 .tmp/.keep：fresh clone 里 pytest 会因 basetemp 父目录不存在而报错")


def test_no_oneoff_artifacts_or_machine_paths_are_tracked() -> None:
    """仓库⛔ 跟踪一次性产物，也⛔ 出现带真用户名的绝对路径。

    2026-11-24 发布复验时才从 **公网 clone** 里看到：`.tmp_lat_fix.py`、`.tmp_rerank2.log`、
    `.tmp_rr3.log` 早在 D5/D6 就被 `git add -A` 捎进提交推上去了。三个后果：
    ① 那个 .py 是 shell 转义事故的产物，中文全成乱码、语法还是坏的 ⇒ 陌生人扫仓库时
       第一条印象就是"这人有垃圾没清"；
    ② 那两个 log 里有一行 `C:\\Users\\<数字账号>\\AppData\\Local\\Temp\\jieba.cache`
       ⇒ **机器指纹**，而且恰好是 README「缓存⛔ 写系统盘」那段的反证（jieba 那条后来才补上）；
    ③ 原有的脱敏扫描⛔ 覆盖它们：白名单只收 .py/.md/.toml/.json/.txt，`.log` 整个不在论域里。
    ⇒ 论域换成「git 跟踪的文件」这个事实集合，⛔ 我再手抄一遍后缀白名单。
    """
    import subprocess

    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True).stdout.splitlines()
    junk = [t for t in tracked
            if Path(t).name.startswith(".tmp_") or Path(t).suffix == ".log"]
    assert not junk, "一次性产物被跟踪了（该 git rm，靠 .gitignore 挡不住已入库的）：" + ", ".join(junk)

    # 机器用户名形态的路径：`C:\Users\<you>` 这种占位写法是**文档**，合法；真名/真账号非法
    bad = []
    for t in tracked:
        p = ROOT / t
        if not p.is_file() or p.suffix not in {".py", ".md", ".toml", ".json", ".txt",
                                               ".log", ".yml", ".yaml", ".sh", ".cfg", ".ini"}:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in USER_PATH_RE.finditer(text):
            if m.group(1).lower() not in ("<you>", "<user>", "%username%", "<user_name>"):
                bad.append(f"{t}: {m.group()[:40]}")
    assert not bad, "跟踪文件里出现真实用户目录路径（机器指纹）：\n" + "\n".join(bad)
