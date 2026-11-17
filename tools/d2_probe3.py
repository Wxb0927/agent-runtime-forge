"""D2 probe #3: can a SCRIPTED (offline, zero-cost) chat model drive create_agent?

If bind_tools / tool_calls don't work on a hand-written BaseChatModel, the whole
"tests run offline and deterministically" plan is wrong and I need to know now.
Run: uv run python -X utf8 tools/d2_probe3.py
"""
from __future__ import annotations

import inspect
import json
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import BaseModel, Field


def show(t: str) -> None:
    print()
    print("=" * 76)
    print(t)
    print("=" * 76)


# ---------------------------------------------------------------- A
show("A) @tool with pydantic args_schema -- what the model sees")


class ProgressIn(BaseModel):
    project: str = Field(description="project code, e.g. PRJ-0007")
    as_of: str = Field(description="date YYYY-MM-DD, injected by caller")


@tool(args_schema=ProgressIn)
def progress_stats(project: str, as_of: str) -> dict:
    """Progress statistics for one project: completion and overdue rates."""
    return {"project": project, "as_of": as_of, "ok": True}


print("  name        :", progress_stats.name)
print("  description :", repr(progress_stats.description))
print("  args        :", json.dumps(progress_stats.args, ensure_ascii=False)[:300])


# ---------------------------------------------------------------- B
show("B) BaseChatModel.bind_tools: does the DEFAULT implementation exist?")
print("  bind_tools defined in:", next(
    c.__name__ for c in BaseChatModel.__mro__ if "bind_tools" in c.__dict__))
print("  signature            :", str(inspect.signature(BaseChatModel.bind_tools))[:200])


# ---------------------------------------------------------------- C
show("C) Scripted model drives create_agent fully OFFLINE")


class Scripted(BaseChatModel):
    """Emits a canned script of tool_calls, then a final text answer."""

    script: list[dict[str, Any]] = []
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        # BaseChatModel.bind_tools is literally `raise NotImplementedError`, so a
        # stub must provide it. Pass the OpenAI-shaped specs down as _generate kwargs,
        # exactly like a real provider does -- that is what lets tests assert which
        # tool subset the graph handed the model.
        from langchain_core.utils.function_calling import convert_to_openai_tool

        return self.bind(tools=[convert_to_openai_tool(t) for t in tools],
                         tool_choice=tool_choice, **kwargs)

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: CallbackManagerForLLMRun | None = None,
                  **kwargs: Any) -> ChatResult:
        tools_seen = kwargs.get("tools")
        step = self.script[self.calls] if self.calls < len(self.script) else {"final": "done"}
        self.calls += 1
        print(f"  [scripted step {self.calls}] bound tools="
              f"{[t['function']['name'] for t in tools_seen] if tools_seen else None}")
        if "final" in step:
            msg = AIMessage(content=step["final"])
        else:
            msg = AIMessage(content="", tool_calls=[
                {"name": step["name"], "args": step["args"], "id": f"call_{self.calls}"}])
        return ChatResult(generations=[ChatGeneration(message=msg)])


s = Scripted(script=[{"name": "progress_stats", "args": {"project": "PRJ-0007", "as_of": "2026-11-17"}},
                      {"final": "completion 62% / overdue 3"}])
try:
    bound = s.bind_tools([progress_stats])
    print("  bind_tools ->", type(bound).__name__)
    from langchain.agents import create_agent

    agent = create_agent(model=s, tools=[progress_stats],
                         system_prompt="Use tools. Report numbers verbatim.")
    res = agent.invoke({"messages": [{"role": "user", "content": "PRJ-0007 progress?"}]})
    print("  keys      :", sorted(res.keys()))
    for m in res["messages"]:
        tc = getattr(m, "tool_calls", None)
        print(f"   - {type(m).__name__}: content={str(m.content)[:60]!r} tool_calls={tc}")
except Exception as exc:  # noqa: BLE001
    print(f"  FAIL {type(exc).__name__}: {str(exc)[:400]}")
