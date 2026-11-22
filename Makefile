# D4 评估流水线一键入口。
# ⚠️ 本机没装 make（2026-11-22 实测 `command -v make` 为空），所以 `make eval`
#    这条命令本身**尚未在本机实跑过**；下面每个 target 的等价命令都是跑过的，
#    README 也按这个口径写，⛔ 承诺未验证的东西。
PY ?= uv run python

.PHONY: eval eval-live cases rebase check gates test mutate

eval:            ## 跑完整评估流水线并写报告
	$(PY) -m eval.run

cases:           ## 从「注册表 × 问法」生成语料（⛔ 人挑题）
	$(PY) -m eval.run --write-cases

rebase:          ## 用本次结果更新回归基线（改动被批准后才做）
	$(PY) -m eval.run --rebase

check:           ## PR 前自动检查：有未登记的新红即 exit 1
	$(PY) -m eval.run --check

eval-live:       ## 额外启用 LLM judge（会花额度；离线跑时报告会写 llm=skipped）
	$(PY) -m eval.run --live-judge

gates:           ## 契约层三道门禁 G1/G2/G3
	$(PY) -m forge.contract.gates

test:
	$(PY) -m pytest -q

mutate:          ## 证明测试"能失败"（改歪→红、改回→绿）
	$(PY) tools/mutation_check.py
