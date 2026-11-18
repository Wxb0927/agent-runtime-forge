"""Line A2：**手写最小 ReAct 环**，为了"框架里面转的是什么"这句话有代码兜底。

主流做法是 ``create_agent``（Line A）；这一条故意不用它，只用 ``StateGraph`` 的原语把
那个环亲手搭出来。两边工具、guard、数据集、信封完全同一套，差异只在编排。

    START → model ──(有 tool_calls?)──> tools → model → … ──(没有)──> END

⚠️ 三条读数（都是 Line A 帮你做掉、Line A2 要你亲自负责的事）：
1. **环的终止条件在人手里**：``tools_condition`` 只看最后一条 AIMessage 有没有 tool_calls；
   ⛔ 上限时模型会永远要调工具 ⇒ 这里靠 ``recursion_limit`` 兜底（Line A 用中间件兜）。
2. **并行工具调用**由 ``ToolNode`` 一次执行多个 call，写回 ``messages`` 走
   ``add_messages`` reducer ⇒ 不会互相覆盖（对比 Line B 的共写竞态）。
3. 状态只有 ``messages`` 一个通道——这就是"框架跑循环"的最小形态。
"""
from __future__ import annotations

from typing import Annotated, Any, Optional, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AnyMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from forge.graph.tools import all_tools
from forge.model.scripted import ScriptedChatModel


class MinState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]


def build_react_min(*, model: Optional[BaseChatModel] = None,
                    skills: Optional[list[str]] = None,
                    scratch_dir: str | Any = "data/react_min",
                    checkpointer: Any = None):
    tools = all_tools(skills, scratch_dir)
    llm = (model or ScriptedChatModel()).bind_tools(tools)

    def call_model(state: MinState) -> dict[str, Any]:
        return {"messages": [llm.invoke(state["messages"])]}

    g = StateGraph(MinState)
    g.add_node("model", call_model)
    g.add_node("tools", ToolNode(tools))
    g.add_edge(START, "model")
    g.add_conditional_edges("model", tools_condition, {"tools": "tools", END: END})
    g.add_edge("tools", "model")  # ← 这一条回头边就是"环"，公司那层物理上画不出来
    return g.compile(checkpointer=checkpointer), [t.name for t in tools]
