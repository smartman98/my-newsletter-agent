# -*- coding: utf-8 -*-
"""전체 파이프라인. 판정 → 근거 조립 → 답변 → 검증을 하나의 그래프로 잇는다.

    classify → gate ┬→ answer → verify ┬→ END
                    │                  └→ answer(1회 재작성) → END
                    └→ escalate → END

classify(모델이 하는 일: 분류)와 gate(정책이 정하는 일: 처리/이관)를 나눠 둔 이유는,
임계값만 바꿀 때 모델을 다시 부르지 않아도 되고 "확신이 없을 때 넘기는 판단"을 모델이 아니라
우리가 통제하기 위해서다.
"""
import json
import operator
import re
import uuid
from typing import Annotated, Literal, Optional, TypedDict

from langchain.chat_models import init_chat_model
from langchain.tools import tool
from langchain_core.messages import AIMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from pydantic import BaseModel, Field

from config import ANSWER_MODEL, CONF_THRESHOLD, MAX_TOOL_TURNS, MODEL, chat_kwargs
from context import build_answer_prompt, used_sections
from prompts import ROUTE_GUIDE
from tools import TOOLS
from verify import verify_answer

LC_TOOLS = [tool(fn) for fn in TOOLS.values()]


# ---------- ① 카테고리 판정 ----------
class RouteDecision(BaseModel):
    """훈련생 문의 한 건에 대한 분류 결과."""

    category: Literal["ATTENDANCE", "LEAVE", "ALLOWANCE", "CONDUCT", "PROGRAM",
                      "OTHER"] = Field(
        description="문의를 배정할 카테고리. 6개 값 중 하나만 쓴다.")
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="판단의 확신도. 두 카테고리 사이에서 애매하면 0.5 미만으로 낮춘다.")
    reason: str = Field(description="그렇게 판단한 근거를 한 문장으로.")


_router_chain = None


def classify(state: dict) -> dict:
    global _router_chain
    if _router_chain is None:
        _router_chain = init_chat_model(
            MODEL, **chat_kwargs(MODEL)
        ).with_structured_output(RouteDecision)
    d = _router_chain.invoke(
        [("system", ROUTE_GUIDE), ("human", f"훈련생 문의: {state['question']}")])
    return {"category": d.category, "confidence": d.confidence, "reason": d.reason}


def gate(state: dict) -> dict:
    """확신도와 응대 범위를 보고 처리/이관/범위밖을 정한다 — 정책이 정하는 일."""
    if state["confidence"] < CONF_THRESHOLD:
        return {"action": "ESCALATE",
                "answer": "어느 쪽 문의인지 제가 확실히 판단하기 어려워, 운영 매니저에게 연결해 "
                          "드리겠습니다."}
    if state["category"] == "OTHER":
        return {"action": "OUT_OF_SCOPE"}
    return {"action": "HANDLE"}


# ---------- ②③ 근거 조립 + 답변 (도구 호출 루프) ----------
class ToolState(TypedDict, total=False):
    messages: Annotated[list, add_messages]


llm_t = init_chat_model(ANSWER_MODEL, **chat_kwargs(ANSWER_MODEL, for_tools=True)).bind_tools(LC_TOOLS)


def _agent(state: ToolState) -> ToolState:
    return {"messages": [llm_t.invoke(state["messages"])]}


def _build_tool_graph():
    g = StateGraph(ToolState)
    g.add_node("agent", _agent)
    g.add_node("tools", ToolNode(LC_TOOLS))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition)
    g.add_edge("tools", "agent")
    return g.compile()


tool_app = _build_tool_graph()


def _as_text(message) -> str:
    """Responses API(gpt-6 세대)는 content를 블록 리스트로 돌려준다 — 한 줄로 합친다."""
    content = message.content
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _tool_results(messages) -> dict:
    used = {}
    for m in messages:
        if getattr(m, "name", None) in TOOLS:
            try:
                used[m.name] = json.loads(m.content)
            except (json.JSONDecodeError, TypeError):
                used[m.name] = m.content
    return used


def answer(state: dict, extra_instruction: str = "") -> dict:
    """근거를 조립해 답변을 만든다. 모델이 필요하다고 판단하면 도구를 부른다."""
    category = state["category"]
    system = build_answer_prompt(state["question"], category)
    if extra_instruction:
        system += "\n" + extra_instruction
    init = {"messages": [("system", system), ("human", state["question"])]}
    try:
        out = tool_app.invoke(init, {"recursion_limit": 2 * MAX_TOOL_TURNS + 5})
    except GraphRecursionError:
        return {"answer": "정확한 확인을 위해 운영 매니저에게 문의해 주세요.",
                "tools_called": [], "tool_results": {},
                "sections_used": used_sections(category)}
    results = _tool_results(out["messages"])
    return {"answer": _as_text(out["messages"][-1]),
            "tools_called": sorted(results),
            "tool_results": results,
            "sections_used": used_sections(category)}


def node_answer(state: dict) -> dict:
    return answer(state)


# ---------- ④ 검증 ----------
def node_verify(state: dict) -> dict:
    result = verify_answer(state["answer"], state["category"], state.get("tool_results"))
    return {"verification": result,
            "attempts": state.get("attempts", 0) + 1}


def after_verify(state: dict) -> str:
    """통과하면 끝. 위반이면 한 번만 다시 쓰게 하고, 그래도 안 되면 이관한다."""
    if state["verification"]["ok"]:
        return END
    return "rewrite" if state.get("attempts", 0) < 2 else "escalate"


def node_rewrite(state: dict) -> dict:
    problems = "; ".join(v["detail"] for v in state["verification"]["violations"])
    nudge = (f"[검증 결과 — 아래 문제를 고쳐 다시 써라]\n{problems}\n"
             f"근거 문서에 없는 내용은 빼고, 근거 문서에 있는 값은 숫자 그대로 넣어라.")
    return answer(state, extra_instruction=nudge)


def node_out_of_scope(state: dict) -> dict:
    return {"answer": "문의하신 내용은 이 안내의 범위를 벗어나서 확인해 드리기 어렵습니다. "
                      "개인별 출결 기록은 HRD-Net 출결 앱에서, 그 밖의 사항은 운영 매니저에게 "
                      "문의해 주세요.",
            "tools_called": [], "tool_results": {}, "sections_used": []}


def node_escalate(state: dict) -> dict:
    return {"answer": state.get("answer") or "정확한 확인을 위해 운영 매니저에게 문의해 주세요.",
            "action": "ESCALATE"}


def after_gate(state: dict) -> str:
    return {"HANDLE": "answer", "OUT_OF_SCOPE": "out_of_scope"}.get(state["action"], "escalate")


class AgentState(TypedDict, total=False):
    question: str
    category: str
    confidence: float
    reason: str
    action: str
    answer: str
    tools_called: list
    tool_results: dict
    sections_used: list
    verification: dict
    attempts: int


def build():
    g = StateGraph(AgentState)
    g.add_node("classify", classify)
    g.add_node("gate", gate)
    g.add_node("answer", node_answer)
    g.add_node("verify", node_verify)
    g.add_node("rewrite", node_rewrite)
    g.add_node("out_of_scope", node_out_of_scope)
    g.add_node("escalate", node_escalate)

    g.add_edge(START, "classify")
    g.add_edge("classify", "gate")
    g.add_conditional_edges("gate", after_gate,
                            {"answer": "answer", "out_of_scope": "out_of_scope",
                             "escalate": "escalate"})
    g.add_edge("answer", "verify")
    g.add_conditional_edges("verify", after_verify,
                            {"rewrite": "rewrite", "escalate": "escalate", END: END})
    g.add_edge("rewrite", "verify")
    g.add_edge("out_of_scope", END)
    g.add_edge("escalate", END)
    return g.compile()


app = build()


def ask(question: str) -> dict:
    """문의 한 줄을 파이프라인에 통과시킨다."""
    return app.invoke({"question": question})
