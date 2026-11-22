"""``--intent`` 字典校验：非法键点名丢弃、越界钳位、``BAD_INTENT`` 回带合法枚举。

任务书验收原文：
> 故意传 ``{"metric":"conplte_rate","topK":999}`` ⇒ 只应报 ``metric`` 非法、``topK`` 钳位到上限、
> 并回带合法枚举；把字典校验改成"未知名当默认值"⇒ 对应用例必须红。

⛔ 模糊匹配、⛔ 自动纠正、⛔ 分词猜意图——猜出来的数字看起来最像真的，也最危险。
"""
from __future__ import annotations

import pytest

from forge.contract import intent as I
from forge.contract import metrics as M
from forge.graph import contract

AS_OF = "2026-10-30"   # 由下面 test_control_date_must_itself_be_valid 现场验证可解析
# 非法基准日的三种写法：13 月、纯文字、斜杠分隔（⛔ 让 run() 拿今天兜底）
BAD_DATES = ("20261301", "昨天", "2026/10/30")


# ------------------------------------------------------------- 任务书验收案例


def test_acceptance_case_names_only_the_illegal_metric():
    env = I.run({"metric": "conplte_rate", "topK": 999})
    assert env["ok"] is False and env["code"] == "BAD_INTENT"
    assert "conplte_rate" in env["message"]
    # ⛔ 因为 metric 非法就连 topK 的钳位也不报了（宿主得再烧一轮）
    assert any(n.startswith("CLAMPED topK 999→50") for n in env["notes"]), env["notes"]
    # ⛔ 把非法 metric 悄悄当成 completion_rate 执行
    assert env["digest"] == []
    assert "意图未执行" in env["answer"]


def test_acceptance_case_returns_the_legal_enum_for_self_healing():
    env = I.run({"metric": "conplte_rate", "topK": 999})
    heal = I.parse_heal(env)
    assert heal["metric"] == M.declared_metrics()
    assert "completion_rate" in heal["metric"], "回带的枚举里没有正确拼法，宿主怎么自愈"


@pytest.mark.parametrize("typo", ["conplte_rate", "COMPLETION_RATE", "completion-rate",
                                  " completion_rate", "compl", "逾期率"])
def test_typos_are_never_guessed(typo):
    """⛔ 猜测的主路径：大小写、连字符、前缀、中文别名，一律按非法处理。"""
    env = I.run({"metric": typo})
    assert env["code"] == "BAD_INTENT", f"{typo!r} 竟然被执行了 ⇒ 有模糊匹配混进来"
    assert env["digest"] == []


# ------------------------------------------------------------- 键与钳位语义


def test_unknown_key_is_named_but_execution_continues():
    """任务书原话"非法键丢弃并点名"：⛔ 静默忽略，也⛔ 一个多余键毁掉整次查询。"""
    env = I.run({"metric": "overdue_rate", "engine": "openai"})
    assert env["ok"] is True and env["digest"], "多余键把整条查询打死了"
    assert any(n == "DROPPED:engine" for n in env["notes"])
    assert any("未知键已丢弃" in n for n in env["notes"])
    assert I.parse_heal(env)["unknown_keys"] == ["engine"]


def test_out_of_range_is_clamped_not_rejected():
    env = I.run({"metric": "completion_rate", "topK": 999}, user_key="u0000", as_of=AS_OF)
    assert env["ok"] is True, "钳位是合法语义，⛔ 报错"
    assert any("CLAMPED topK 999→50" in n for n in env["notes"])
    env2 = I.run({"metric": "completion_rate", "topK": 0})
    assert env2["ok"] is True and any("topK 0→1" in n for n in env2["notes"])


def test_non_integer_topk_is_rejected():
    env = I.run({"metric": "completion_rate", "topK": "many"})
    assert env["code"] == "BAD_INTENT" and "topK 必须是整数" in env["message"]


def test_missing_required_metric_is_reported():
    env = I.run({"topK": 3})
    assert env["code"] == "BAD_INTENT"
    assert "缺少必填键" in env["message"] and "metric" in env["message"]


def test_all_problems_reported_in_one_pass():
    """一轮改完：三个毛病一次全说，⛔ 挤牙膏式一次报一个。"""
    env = I.run({"metric": "nope", "topK": 999, "junk": 1})
    assert env["code"] == "BAD_INTENT"
    assert "metric 非法" in env["message"]
    assert any(n.startswith("CLAMPED") for n in env["notes"])
    assert any(n == "DROPPED:junk" for n in env["notes"])


# ------------------------------------------------------- 宿主注入项（基准日）


@pytest.mark.parametrize("bad", BAD_DATES)
def test_host_injected_bad_date_is_never_backfilled_with_today(bad):
    """基准日畸形 ⇒ 必须拒答。⛔ 悄悄用今天兜底——那会让同一意图每天给出不同答案。"""
    env = I.run({"metric": "completion_rate"}, as_of=bad)
    assert env["code"] == "BAD_HOST_ARG", f"{bad!r} 竟然被接受了"
    assert "系统时间兜底" in env["message"]
    assert env["digest"] == []


def test_bad_date_samples_are_really_bad():
    """元检查：**坏样例必须真的解析不了**——否则"坏日期被拒"的用例是空转。

    今天为这条花了 30 分钟：输出通道把样例里的斜杠渲染成了连字符，我一度以为
    "合法日期被判非法"，于是去查字节码缓存、查环境变量、查 datetime 被谁替换。
    ⇒ ⛔ 相信"看起来是 X"，凡是判据就写成可执行断言（这条就是那种断言）。
    """
    from datetime import date

    for bad in BAD_DATES:
        with pytest.raises(ValueError):
            date.fromisoformat(bad)


def test_control_date_must_itself_be_valid():
    """**自检**：基准日常量本身必须可解析，否则上面所有"坏日期"用例的绿都不可信。

    今天在这组用例上被坑过：参数里那个看起来合法的日期，在 pytest 进程里被
    ``date.fromisoformat`` 判成非法，于是"好日期""坏日期"两种相反断言**同时通过**，
    光看摘要一行都发现不了。⇒ "对照组为真"必须写成**独立可失败的测试**，⛔ 靠我盯着常量。
    """
    from datetime import date

    parsed = date.fromisoformat(AS_OF)
    assert parsed.isoformat() == AS_OF, \
        f"环境里 fromisoformat({AS_OF!r}) 都不认 ⇒ 本文件其余日期断言全部作废"


def test_valid_host_date_is_accepted():
    """对照组：合法基准日必须走通（⛔ 只测坏例子会掩盖"永远拒答"这种反向 bug）。"""
    env = I.run({"metric": "completion_rate"}, as_of=AS_OF)
    assert env["ok"] is True and env["code"] == "OK", env["message"]


# ------------------------------------------------------------- 与权限层的接缝


def test_out_of_scope_project_is_refused_without_data():
    """契约层⛔ 是绕过权限的后门：u0041 是 bu-beta，PRJ-1007 属 bu-alpha。"""
    env = I.run({"metric": "completion_rate", "project": "PRJ-1007"}, user_key="u0041")
    assert env["code"] == "NOT_IN_SCOPE"
    assert env["digest"] == []
    assert "PRJ-1180" not in env["answer"]


def test_in_scope_project_returns_traceable_numbers():
    env = I.run({"metric": "completion_rate", "project": "PRJ-1180"}, user_key="u0041")
    assert env["ok"] is True
    m = env["digest"][0]
    assert m["numerator"] == 33 and m["denominator"] == 38   # 与 D2 三方对照同一读数
    assert m["as_of"] == AS_OF and m["value"] in env["answer"]


# ------------------------------------------------------------- 信封纪律


@pytest.mark.parametrize("intent", [
    {"metric": "completion_rate"},
    {"metric": "cpi", "topK": 2},
    {"metric": "nope"},
    {"metric": "milestone_offschedule_rate", "project": "PRJ-1180"},
])
def test_every_response_is_a_valid_envelope(intent):
    env = I.run(intent, user_key="u0000", as_of=AS_OF)
    assert list(env) == list(contract.ENVELOPE_KEYS), tuple(env)
    for word in contract.BANNED_PHRASES:
        assert word not in env["answer"] and word not in env["message"]


def test_run_is_deterministic():
    a = I.run({"metric": "overdue_rate", "topK": 5}, as_of=AS_OF)
    b = I.run({"metric": "overdue_rate", "topK": 5}, as_of=AS_OF)
    assert a == b, "同一意图两次结果不同 ⇒ 有隐藏状态或读了系统时钟"
