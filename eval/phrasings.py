"""评估语料的两个来源：**域来自度量注册表，问法来自这张表**。

⚠️ 域⛔ 手抄、⛔ 凭空造（比如造一个"回款域"而计算层没有对应指标）——那会让矩阵
"覆盖率"变成自造的漂亮数字。域轴直接取 ``metrics.declared_metrics()``，
所以加一个指标就自动多一行格子，⛔ 矩阵会跟着长。

问法模板故意混了三种风格：**术语说法**（该命中）、**口语说法**（考验路由）、
**跨域歧义说法**（考验降级与弃权）。⚠️ ⛔ 只挑术语说法做题——
"自选验收集必然偏向我已经想到的说法"，这条在生产上付过代价（人挑曾报 100%，
换成矩阵枚举后大幅回落）。
"""
from __future__ import annotations

from forge.contract import metrics as M

TARGET = "PRJ-1180"

# 问法：8 种，覆盖术语/口语/省略/英文混排/带歧义
PHRASINGS: tuple[tuple[str, str], ...] = (
    ("plain", "查一下 {p} 的{d}"),                       # 术语直陈
    ("ask_ratio", "{p} 完成得怎么样了"),                  # 口语，⛔ 出现指标名
    ("ask_pct", "{p} 还剩几成没做完"),                    # 反向问法
    ("ask_report", "给我一份 {p} 的{d}报表"),              # 报表口吻
    ("ask_compare", "{p} 这季度和上季度比{d}如何"),        # 含不支持的时间对比
    ("ask_en", "status of {p} about {d}"),                # 中英混排
    ("ask_vague", "{p} 现在到底什么情况"),                 # 极模糊，考验降级
    ("ask_why", "{p} 的{d}为什么这样"),                    # 要因果，应⛔ 编因果
)

# 指标 → 人类说法里可能出现的词（只用于**生成语料**，⛔ 参与判定）
DOMAIN_WORDS: dict[str, str] = {
    "completion_rate": "完成率",
    "overdue_rate": "逾期率",
    "milestone_offschedule_rate": "里程碑脱期",
    "max_milestone_deviation_days": "最大偏差天数",
    "budget_execution[material]": "材料预算执行率",
    "budget_execution[labor]": "人工预算执行率",
    "budget_execution[manufacturing]": "制造费用执行率",
    "budget_execution[misc]": "其他预算执行率",
    "cpi": "成本绩效指数",
    "eac": "完工成本估算",
}


def domains() -> list[str]:
    """矩阵横轴：与注册表严格同源。"""
    return M.declared_metrics()


def build_queries() -> list[dict[str, str]]:
    """域 × 问法 → 语料条目（含目标指标，供命中判定）。"""
    out: list[dict[str, str]] = []
    for metric in domains():
        word = DOMAIN_WORDS.get(metric, metric)
        for key, tpl in PHRASINGS:
            out.append({
                "domain": metric,
                "phrasing": key,
                "query": tpl.format(p=TARGET, d=word),
            })
    return out


def grid_shape() -> tuple[int, int]:
    return len(domains()), len(PHRASINGS)
