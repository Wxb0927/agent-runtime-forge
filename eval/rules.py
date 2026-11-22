"""② 规则评分：结构断言，⛔ 语义判断（那是 ③ judge 的活）。

每条规则都对应一个**真实会犯的错误**，⛔ 凑数：

* 信封字段顺序 —— 下游宿主按 key 取值，多一个少一个都算契约破坏。
* 段名与列序 —— 生产上"输出规范化每一刀都来自真机 trace"，段名漂了就退回自由文本。
* 数字形态 —— 百分比一位小数、金额两位小数带千分位、日期 ISO；`0.868` 或 `86.8 %` 都算错。
* 禁词表 —— 「大概/约莫/一般来说/我建议/无法回答」出现即红（渲染层在猜或在推责）。
* **数字反查** —— digest 每个 value 必须等于独立重算的值；answer 里出现的数字
  必须能在 digest 里找到（渲染层⛔ 再算一遍）。
* **无数据 ≠ 0** —— value 为 None 的项必须在 answer 里点名，⛔ 显示 0 或 ¥0.00。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from eval import system
from forge.graph import contract

# 数字形态（⛔ 放宽：`86.8%` 与 `86.8 %` 不同，后者算错，因为下游按字面比对）
PERCENT_RE = re.compile(r"^\d+(?:\.\d+)?%$")
MONEY_RE = re.compile(r"^¥-?\d{1,3}(?:,\d{3})*\.\d{2}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NUM_IN_TEXT_RE = re.compile(r"\d+(?:\.\d+)?%|¥[\d,]+\.\d{2}|\d{4}-\d{2}-\d{2}")
SECTIONS = ("【范围】", "【进度】", "【偏差】", "【成本】", "【无数据】", "【来源】",
            "【拒答】", "【意图未执行】")   # 后两个来自契约层与 guard，均已在信封里定型


@dataclass
class Finding:
    rule: str
    message: str
    severity: str = "red"     # red=必修；warn=写进报告但⛔ 拦 PR

    def as_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "message": self.message, "severity": self.severity}


def score(run: system.Run, *, check_oracle: bool = True,
          expect_sections: Optional[list[str]] = None) -> list[Finding]:
    findings: list[Finding] = []
    env = run.envelope
    answer = env.get("answer") or ""

    # 1) 信封形状
    if list(env) != list(contract.ENVELOPE_KEYS):
        findings.append(Finding("envelope-shape", f"信封字段不符：{list(env)}"))

    # 2) 禁词
    hits = [w for w in contract.BANNED_PHRASES if w in answer or w in (env.get("message") or "")]
    if hits:
        findings.append(Finding("banned-words", f"答案含禁词 {hits}"))

    # 3) 段名：首段必须是【范围】，且⛔ 出现表外段名
    if env.get("ok") and not answer.startswith("【范围】"):
        findings.append(Finding("section-order", f"首段不是【范围】：{answer[:20]!r}"))
    for seg in re.findall(r"【[^】]{1,8}】", answer):
        if seg not in SECTIONS:
            findings.append(Finding("section-unknown", f"出现未登记段名 {seg}"))
    for want in expect_sections or []:
        if want not in answer:
            findings.append(Finding("section-missing", f"缺段 {want}"))

    # 4) 数字形态 + 可反查
    for m in run.digest:
        val = m.get("value")
        if val is None:
            if m["name"] not in answer:
                findings.append(Finding("no-data-named",
                                        f"{m['name']} 无数据但未在 answer 点名（⛔ 当作 0）"))
            continue
        if PERCENT_RE.match(val):
            pass
        elif MONEY_RE.match(val):
            pass
        elif re.search(r"^\d+(?:\.\d+)?\s*天", val) or "（" in val:
            pass      # 天数与带编号的说明串，形态另计
        else:
            findings.append(Finding("number-format", f"{m['name']} 数字形态可疑：{val!r}",
                                    severity="warn"))
        if m.get("as_of") and not DATE_RE.match(m["as_of"]):
            findings.append(Finding("date-format", f"{m['name']} 基准日非 ISO：{m['as_of']!r}"))
        # 5) 渲染层⛔ 改数：digest 的值必须原样出现在 answer
        if val not in answer:
            findings.append(Finding("value-in-answer", f"{m['name']}={val} 未出现在 answer"))
        # 6) 派生量必须可反查
        if m.get("numerator") is not None and m.get("denominator") is not None:
            frag = f"{m['numerator']:,}/{m['denominator']:,}"
            if frag not in answer:
                findings.append(Finding("traceable", f"{m['name']} 缺分子/分母：{frag}"))
        elif m.get("denominator") is not None:
            findings.append(Finding("traceable", f"{m['name']} 只有分母没有分子", severity="warn"))

    # 7) answer 里的数字必须都能在 digest 找到（⛔ 凭空多出一个数）
    known = {str(m.get("value")) for m in run.digest if m.get("value")}
    known |= {f"{m['numerator']:,}/{m['denominator']:,}" for m in run.digest
              if m.get("numerator") is not None and m.get("denominator") is not None}
    for token in NUM_IN_TEXT_RE.findall(answer):
        if token in known:
            continue
        if token in ("2026-10-30",) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
            continue          # 基准日与"可见 N 个项目"类不算业务数字
        if token.endswith("%") or token.startswith("¥"):
            findings.append(Finding("fabricated-number", f"answer 里出现 digest 没有的数字 {token}"))

    # 8) 独立预言：digest == 计算层重算
    if check_oracle and run.digest:
        for m in run.digest:
            try:
                ref = system.oracle(m["name"], run.scope_projects, run.as_of)
            except LookupError as exc:
                findings.append(Finding("oracle-missing", str(exc)))
                continue
            if str(ref.get("value")) != str(m.get("value")):
                findings.append(Finding("oracle-mismatch",
                                        f"{m['name']} 系统给 {m.get('value')!r}、重算给 {ref.get('value')!r}"))
    return findings


def summarize(findings: list[Finding]) -> dict[str, int]:
    out = {"red": 0, "warn": 0}
    for f in findings:
        out[f.severity] = out.get(f.severity, 0) + 1
    return out
