# Agent Runtime Forge

把一套**生产环境里跑过企业智能体的运行时框架**，用主流栈（Python + LangGraph + FastAPI + MCP + RAG + 评测流水线）重新实现一遍，做一次**有数据的对照**而不是偏好之争。

> ⚠️ 本项目使用**合成数据**。领域对象、指标公式与阈值取自一段真实的非标自动化装备行业项目管理系统经验，已全部脱敏：公司写作 `Acme Equipment Co.`，字段/表名均为通用命名，⛔ 不含任何真实企业数据、内部域名、应用 ID 或客户信息。

## 为什么要做这个对照

原实现运行在一个受限运行时里：平台只提供一个技能包 zip 和一条命令行，沙箱**不允许安装第三方依赖**，因此契约层、编排层、取数计算层、评测层只能用零依赖 Node.js 自己写。这个项目把它搬到 LangGraph 等现成框架上，回答两个问题：

1. 现成框架替你承担了哪一层（状态通道、checkpoint、HITL 中断、并行 fan-in）；
2. 它又替你把哪一层变成了黑盒（可观测粒度、评测口径、权限判定位置）。

## 当前状态（按天推进，每天有验收判据）

| 阶段 | 内容 | 状态 |
|---|---|---|
| D1 | 领域模型 + 确定性合成数据 + "能失败"的测试骨架 | ✅ 完成（2026-11-16） |
| D2 | LangGraph 图式编排，含真实竞态的解法 + checkpointer + interrupt | ✅ 完成（2026-11-18） |
| D3 | 工具契约层等价实现 + MCP Server | ⏳ 未开始 |
| D4 | 评估流水线（规则评分 / LLM-as-Judge / 回归差集 / 覆盖率矩阵 / PR 前检查） | ⏳ 未开始 |
| D5 | RAG 检索线（本地 BGE-M3 混合检索 + Reranker + 300 条业务 QA 评估集） | ⏳ 未开始 |
| D6 | FastAPI + SSE 服务化与可观测 | ⏳ 未开始 |
| D7 | 架构图、指标表、讲解稿 | ⏳ 未开始 |

⛔ **本 README 不承诺未经实测的能力**：`docker compose` 一键运行在 D6 之前不存在，届时若未在真实 Docker 环境验证过，也会在此明确标注"未验证"。

## D1 的设计决定（不是随便选的）

- **金额一律用整数分（cents），比率阈值用基点（bps）**。⛔ 不用 float 存钱——浮点表示会让"同一 seed 两次生成逐字节相同"这条验收变成运气。
- **所有日期敏感计算显式接收 `as_of`**，⛔ 禁止读系统时钟。真实项目里踩过：日期敏感指标跨天跑差集必然不等，差额恰好等于"没有实际结束日"的那批行数。
- **故意保留数据稀疏性**：`payment_plan` 只有 8% 的节点带计划金额、ECN 成本字段 90% 为 0、交付物每项目 10 项里只交付 0–3 项、750 用户只有 18 条权限配置。这些不是为了"数据难看"，它们正是逼出正确设计的原因（金额必须以计划表为主源；默认放行策略必须显式定义）。测试里**把比例钉死**，防止后来者为了好看把它填满。
- **"无数据"与 0 是两种状态**：公式在分母为 0／数据缺失时返回 `None`，⛔ 返回 0。真实系统的产物里，0 会被业务方读成"确实没花钱"。
- **不提供"实际工期"字段**：真实数据中实际开工日=实际完工日（完成时回填），任何基于"实际工期"的指标都是假的，所以口径直接否决，模型层就不给这个字段。

## 跑起来

```bash
uv sync                                                  # 装依赖（Python 3.11+）
uv run python -m forge.synth.generate --seed 42          # 生成合成数据到 data/
uv run pytest -q                                         # 全部测试
uv run python tools/mutation_check.py                    # 证明测试"能失败"（11 条）
uv run python tools/d2_api_probe.py                      # 框架语义实测读数（⛔ 凭记忆）

# D2 三条编排线跑同一个问句
uv run python -m forge.cli "PRJ-1180 的进度、里程碑偏差和成本怎么样" --line plan
uv run python -m forge.cli "PRJ-1180 进度" --line agent            # 离线打桩模型
uv run python -m forge.cli "PRJ-1180 进度" --line min              # 手写最小 ReAct 环
uv run python -m forge.cli "帮我给 bu-beta 立项" --line plan       # 停在人工闸上
uv run python -m forge.cli "帮我给 bu-beta 立项" --line plan --approve
# 跨进程断点续办（真进程、硬杀）
uv run python tools/intake_host.py --scratch .tmp/demo --thread t1 --crash-in-verify 1.5
uv run python tools/intake_host.py --scratch .tmp/demo --thread t1 --continue
```

## D1 验收读数（2026-11-16，真实输出）

| 判据 | 结果 |
|---|---|
| 同一 seed 两次生成逐字节相同 | `sha256 = f8939fd913cd663c91e5d563d076edc6f381fd14efc5ba05cd76f06713b660b8`，两次运行完全一致 |
| seed 真的起作用 | `--seed 7` → 字节数 5,374,777（≠ 42 的 5,374,743），行数同为 24,949 ⇒ **内容变、形状不变** |
| 规模符合 §3.5 | project 300（BU 180/90/10/10 + 未填 10）、task 12,000（阶段 1,200 / 里程碑 600 / 工序 2,400 / 任务 7,800）、payment_plan 2,300 且**恰好 184 条带计划金额（8.00%）**、ECN 120 且**恰好 108 条成本为 0（90%）**、报工 615 条**只有 9 条有值**、合同 94 份**只有 3 份关联项目**、交付物每项目 10 项已交付 0–3、用户 750 / 权限配置 18 |
| 逾期形状精确 | 逾期未开工 **1,400** 条、延期进行中 **60** 条（同余计数保证，不是抽样近似） |
| 测试 | `38 passed in 4.61s`（Python 3.11.15 / pydantic 2.14.3 / pytest 9.2.0） |
| **能失败** | 5 项变异全部 **改歪→红 (rc=1)、改回→绿 (rc=0)**：逾期率分母、超期占比夹紧、预算为 0 判空、EAC 的 AC=0 判空、payment_plan 填充率填满 |

> 数字怎么来的别信我：`uv run pytest -q` 与 `uv run python tools/mutation_check.py` 各跑一次即可复现上表。

---

# D2 · LangGraph 编排复现与那起并行竞态

## D2 的三条编排线

同一个业务问题（"某项目的进度 / 里程碑偏差 / 成本怎么样"）跑三遍，**只有编排方式不同**：

| 线 | 文件 | 是什么 | 循环在谁手里 | 模型轮次 |
|---|---|---|---|---|
| A | `forge/graph/react_agent.py` | **业界主流入口** `create_agent` + 中间件 | 框架 | 2 |
| A2 | `forge/graph/react_min.py` | **手写最小 ReAct 环**（`model ⇄ tools` 一条回头边） | 框架（但边是我画的） | 2 |
| B | `forge/graph/plan_graph.py` | **公司范式重画**：`router → guard → 三分支并行 → fanin → aggregate` | 宿主（一趟出计划，⛔ 问模型） | 0 |

## D2 的主流分层对照（也是"框架替你承担了什么"的答案表）

| 关注点 | 主流做法 | 本仓库落点 | 原实现（零依赖 Node）对应物 |
|---|---|---|---|
| 骨架 | `create_agent` + middleware；必须确定的段落才手写成显式节点 | Line A / A2 | 静态无环依赖图 + 拓扑计划 |
| 技能身份证 | `@tool(args_schema=Pydantic)`，描述由代码/docstring 派生 | `forge/graph/tools.py` | `--describe` 代码派生自描述 |
| 工具面 | 按路由 **裁剪 bind_tools 子集**（⛔ 一次绑全部） | `router.route()` → `tools_for()` | 只吐技能菜单 |
| 并行与合并 | **channel + reducer**（`Annotated[..., operator.add]`） | `forge/graph/state.py` | 共写一份交接文件 ⇒ 事故 |
| 跨轮恢复 | checkpointer（本地 SQLite / 生产 Postgres），⛔ 业务大对象进 state | `forge/graph/checkpoint.py`，Dataset 按 seed 重载 | 仅会话缓存，**无跨调用恢复** |
| 人在环 | `interrupt` + `Command(resume=…)`，**approve / edit / reject** | Line A 用 HITL 中间件；Line B 自画 `confirm` 节点 | 只有设计口径，未落地 |
| 治理扩展 | 中间件（`ModelCallLimitMiddleware` 等），⛔ 改模型节点本体 | `react_agent.build_react_agent` | 受限并发池 `CONCURRENCY=4` |
| 取数 | 工具内调确定性计算层，⛔ 让模型算数 | `forge/graph/compute.py`（复用 D1 `formulas`） | 度量注册表 |
| 输出 | 结构化信封 + 成品正文 | `forge/graph/contract.py` | 信封 `{ok,code,message,digest[],notes[],answer}` |

## D2 框架语义实测（⛔ 凭记忆写 API，全部来自 `tools/d2_api_probe.py` / `d2_probe2.py` / `d2_probe3.py`）

| 实测问题 | 真实读数 |
|---|---|
| 装到的是什么版本 | langgraph 1.2.22 / langgraph-checkpoint-sqlite 3.1.3 / langchain 1.5.0 / langchain-core 1.8.1 / Python 3.11.15 |
| `create_react_agent` 还在 `langgraph.prebuilt` 吗 | 主流入口已迁到 **`langchain.agents.create_agent`**；中间件在 `langchain.agents.middleware`（HITL / ModelCallLimit / Summarization / before_model… 均可导入） |
| **两个并行节点返回同一个 channel key** | 🔊 抛 `InvalidUpdateError: At key 'handoff': Can receive only one value per step` ⇒ **框架把静默覆盖升级成显式报错**（原实现没有这层保护） |
| 两个并行节点**原地改 state 里同一个 dict** | 🤫 静默覆盖，无任何报错 |
| 两个并行节点**读-改-写同一份交接文件** | 🤫 静默覆盖，而且比预想更狠：**后写者把整份文档换掉**（连 `branches` 列表都只剩一路） |
| 同一 key 声明了 reducer | ✅ 合法且正确合并 ⇒ "报错"不是目的，**让合并语义显式化**才是 |
| `interrupt()` 怎么读 | `state["__interrupt__"] -> [Interrupt(value=…, id=…, response_schema=…)]`，恢复用 `Command(resume=…)` |
| HITL 中间件的载荷形状 | `interrupt_on={tool: {"allowed_decisions": […]}}`；决定是 `{"type": "approve" \| "edit"(+`edited_action`) \| "reject"(+`message`) \| "respond"}`，包在 `{"decisions": […]}` 里 |
| `SqliteSaver` 能异步吗 | ⛔ `aget_tuple` 直接 `NotImplementedError` ⇒ 并发/服务化必须换 `AsyncSqliteSaver`（依赖 `aiosqlite`，已加入） |
| `@tool(args_schema=TypedDict)` | ⛔ `TypeError: args_schema must be a subclass of pydantic BaseModel or a JSON schema dict` |
| `BaseChatModel.bind_tools` 基类实现 | 字面就是一行 `raise NotImplementedError` ⇒ 打桩模型必须自己实现，实现后 **`create_agent` 可完全离线跑**（全套测试零网络、零成本） |
| 崩在节点中间后重启的语义 | 已完成节点⛔ 重跑，**该节点从头重跑**（实测 `collect=1, verify=2`）⇒ 节点必须幂等 |
| 重启时再传一次新输入 | ⚠️ **那不是续跑**，是从 `START` 开新一轮，已完成的节点会重跑（真续跑只能 `invoke(None, cfg)`） |

## 那起竞态在这里的三种形状

```
事故形状  分支A 读文件 → 改 from/step ─┐
          分支B 读文件 → 改 from/step ─┴→ 后写覆盖先写：数据对、来源标签漂（静默）
框架保护  两节点 return 同一个 key      → InvalidUpdateError（响亮，原实现没有）
修复形状  各写各的 state 通道 + notes 用 reducer + 单一 fan-in 写者 → 无覆盖窗口
```

⚠️ 一个设计上的坑（差点让验收变成假的）：第一版的 fan-in 在事故模式下也会顺手把命名空间写回文件，
于是**修复动作把事故现场抹掉了**，变异 M1b 永远转不了红。现在事故模式下的 fan-in **故意从文件回读**
`from`/`step` 当归因依据（这才是当年两下游技能的真实行为），漂掉的标签会一路带进答案。

## D2 验收读数（2026-11-18，真实输出）

| 判据 | 结果 |
|---|---|
| ① 并行分支压测 | 同一 `thread_id` 连跑 **20 次** + **20 个 thread 并发**（`AsyncSqliteSaver`）⇒ 来源标记覆盖 **0 次**；`stats←task_table(1)`、`deviation←milestone_table(2)`、`cost←cost_docs(3)` 三个通道的 `from`/`step` 全部健在 |
| 竞态可复现 | 事故形状（共写同一份交接文件）在 `threading.Barrier` 强制交错下 **100% 漂标记**，且 `envelope.ok=True`、指标全对 ⇒ **静默**；图内通道不受影响 ⇒ 症状正是"数据对、来源标签漂" |
| ② kill 后重启 | 停在人工闸上被硬杀 ⇒ 新进程 `Command(resume=…)` 续跑，`collect/verify` 计数保持 1/1（⛔ 重跑）；崩在节点中间 ⇒ 死掉的节点**一定**重放（`verify` 1→2），**上一个节点是否重放是竞态**（见下） |
| ③ interrupt 人工闸 | **approve / edit / reject** 三条路径各有测试（Line B 自画节点 + Line A HITL 中间件各一套）；reject/edit 都断言到**副作用层面**（草稿文件与 `audit.jsonl` 的行数），⛔ 只看返回值；缺必填时即使批准也返回 `INCOMPLETE_INTAKE` 且零写入 |
| ④ 改歪→红 | `tools/mutation_check.py` **11/11 通过（D1 5 + D2 6）**，每条"改歪 rc=1 / 改回 rc=0"：M1 共用 channel key、M1b 默认改回事故形状、M2 guard 默认放行、M3 摘掉 checkpointer、M4 删掉 `interrupt()`、M5 去掉 `operator.add` |
| ⑤ 不回归 | `uv run pytest` → **84 passed in 18.72s**（连跑两遍一致；D1 的 38 条全绿）<br>Python 3.11.15 / pydantic 2.14.3 / pytest 9.2.0 / langgraph 1.2.22 |
| ⑥ 三方对照 | 同一问句跑 Line A / A2 / B ⇒ **10 条指标逐字一致**（`completion_rate=86.8%(33/38)`、`overdue_rate=13.2%(5/568)`…）；代价不同：**模型轮次 A=2、A2=2、B=0**，Line B 节点执行 7 步 |
| ⑦ 脱敏 | `tests/test_d2_hygiene.py` 扫全部源文件：雇主指纹 / 真实字段名 / 人名手机号 / `sk-` 形态密钥 **0 命中**（禁词表本身按片段拼，否则清单自己就是泄露源） |
| ⑧ 密钥 | `.env` 未被 git 跟踪；`Settings.api_key` 用 `SecretStr` 且 `exclude=True`，`repr/str/model_dump` 三条路都不出现；⛔ 任何日志与异常文本里打印 key |

## ⚠️ D2 最值得记的一条读数：checkpoint 的耐久性窗口

`test_r2` 写了**三次**才对，两次失败都是我的测量方式在骗我：

1. 第一版让父进程掐表 `kill()` 子进程 → "整套跑 3 次红 2 次、单跑 6 次全绿"。死点落在 `sleep` 的哪一秒是随机的，这种红不是 bug 的信号。
2. 第二版改成节点内部 `os._exit(9)`（死点确定），断言"刚进节点就死会连前一个节点一起重跑"——在命令行里 **3/3 成立**，进了 pytest 就翻车（`collect` 变成 1）。
3. 还有一次更狼狈的：`--crash-in-verify 0` 的崩溃分支被我写在 `if verify_seconds:` 里面，于是**进程根本没崩**，我差点把"续跑不会重放任何节点"当成结论写进 README。

⇒ 真相是：**上一个超步的 checkpoint 是否已经落盘，与进程死亡是并发发生的**。所以"已完成的节点永不重跑"⛔ 无条件成立，它只在"已提交"之后成立。
可钉的不变量只有两条，测试也就只断言这两条：① 死掉的那个节点一定重放（它的产出从未提交）；② 续跑之后整体仍然正确——仍停在同一个闸上、批准只写一份草稿、审计只有一条。
⇒ 工程结论：**节点必须幂等，有副作用的写必须放到人工闸后面**（本项目 `write` 就在 `confirm` 之后）。

顺带一条同源的坑：**`--continue`（`invoke(None)`）才是续跑**，"重启进程再传一次新输入"是从 `START` 开新一轮，已完成的节点会重跑——第一版 R2 就是把这两件事混了。

## D2 其他踩过的坑（全是我自己的错，留着当验收的一部分）

1. **测试挂死不是框架的问题**：`--hold` 子进程的 STATUS 行没 `flush`，stdout 被重定向成管道时 Python 用块缓冲（8 KB），父进程永远读不到 ⇒ 每个 spawn 白等 120 s。
2. **`stderr=PIPE` 只读 stdout** 是经典管道死锁形状，改成 `stderr=STDOUT` 合并。
3. **变异"还原原样"悄悄改了行尾**：文本模式写文件在 Windows 会把换行译成 CRLF，`git status` 里整文件变红而真内容一处没动 ⇒ 改成**按字节**备份/还原，并把工作区 12 个 CRLF 文件归一回 LF。
4. **汇总计数报 5/5 而实际跑了 11 条**：给工具加第二批变异时漏改统计口径。一个"证明测试能失败"的工具自己报出错数，比没测更糟。
5. `basetemp` 写成 ini 选项 → pytest 9.2 报 `Unknown config option` 然后**照样跑 C 盘**；只能走命令行。现在 `addopts` 里钉死 `.tmp/pytest`，并有 `tests/test_d2_hygiene.py` 断言 `tmp_path` 落在项目盘内。

## 缓存与磁盘占用（⛔ 悄悄写系统盘）

- 依赖缓存：`uv.toml` 里 `cache-dir = "D:/uv/cache"`（跟着仓库走，⛔ 依赖环境变量）。
- 测试临时文件：`--basetemp=.tmp/pytest`（项目内，已 gitignore）。
- `python-install-dir` ⛔ 不是合法的 `uv.toml` 键（uv 会整份拒绝并报 `unknown field`，实测），解释器位置只能用 `UV_PYTHON_INSTALL_DIR` 指定。
