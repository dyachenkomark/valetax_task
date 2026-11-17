"""The co-pilot as a LangGraph graph.

    retrieve -> generate -> check --+--> END             (standard queue)
                                    +--> human_review    (pauses for a person)

LangGraph is here for one reason: the pause. A ticket that needs a person
stops at `human_review` (`interrupt`), its state is saved by the checkpointer,
and a separate process resumes it with the reviewer's decision - minutes or
days later. Without that requirement this would be a plain function.
"""

import uuid
from typing import TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from copilot import prompts, rules
from copilot.config import Settings
from copilot.dataset import Chunk, select
from copilot.llm import LLM, LLMError
from copilot.schemas import Citation, Classification, Draft, Result, TriageAndDraft, Usage


class State(TypedDict, total=False):
    """Plain dicts and lists only: the checkpointer stores this between runs."""

    ticket: str
    chunk_ids: list[str]
    classification: dict
    draft: dict
    usage: list[dict]
    reasons: list[str]  # why a person must handle the ticket; empty = standard queue
    review: dict  # the reviewer's decision: {"action": approve|edit|reject, "reply": ...}


def build_graph(
    dataset: list[Chunk], llm: LLM, settings: Settings, checkpointer: BaseCheckpointSaver
):
    by_id = {chunk.id: chunk for chunk in dataset}

    def retrieve(state: State) -> State:
        # The query is the raw ticket, so bm25 mode needs tickets in the dataset's
        # language; "full" mode passes every chunk and has no such limit.
        chunks = select(dataset, state["ticket"], settings.retrieval, settings.top_k)
        return {"chunk_ids": [chunk.id for chunk in chunks]}

    def generate(state: State) -> State:
        chunks = [by_id[i] for i in state["chunk_ids"]]
        try:
            answer, used = llm.parse(
                model=settings.model,
                system=[prompts.SYSTEM, prompts.dataset_block([c.render() for c in chunks])],
                user=f"<ticket>\n{state['ticket']}\n</ticket>",
                schema=TriageAndDraft,
            )
        except LLMError as error:
            # Outage, refusal or invalid output: the co-pilot may fail, but
            # the ticket must not get lost - it goes to a person.
            return {"reasons": [f"llm_error: {error}"]}
        return {
            "classification": answer.classification.model_dump(),
            "draft": answer.draft.model_dump(),
            "usage": [used.model_dump()],
        }

    def check(state: State) -> State:
        if "draft" not in state:
            return {}
        draft = Draft(**state["draft"])
        chunks = [by_id[i] for i in state["chunk_ids"]]
        classification = Classification(**state["classification"])
        issues = rules.validate_draft(
            draft, chunks, state["ticket"], classification.asks_trading_advice
        )
        reasons = rules.decide_route(
            classification,
            rules.keyword_flags(state["ticket"]),
            draft,
            issues,
        )
        return {"reasons": reasons}

    def human_review(state: State) -> State:
        # Execution stops here. `interrupt` returns only when someone resumes
        # the thread with Command(resume=decision).
        decision = interrupt({"reasons": state["reasons"], "draft": state.get("draft")})
        return {"review": decision}

    graph = StateGraph(State)
    graph.add_node("retrieve", retrieve)
    graph.add_node("generate", generate)
    graph.add_node("check", check)
    graph.add_node("human_review", human_review)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", "check")
    graph.add_conditional_edges(
        "check", lambda state: "human_review" if state.get("reasons") else END
    )
    graph.add_edge("human_review", END)
    return graph.compile(checkpointer=checkpointer)


def to_result(ticket_id: str, state: State, dataset: list[Chunk]) -> Result:
    by_id = {chunk.id: chunk for chunk in dataset}
    draft = Draft(**state["draft"]) if "draft" in state else None
    usage = [Usage(**u) for u in state.get("usage", [])]
    reasons = state.get("reasons", [])
    review = state.get("review")
    if not reasons:
        status, reply = "ready", draft.reply
    elif review is None:
        status, reply = "awaiting_review", None
    else:
        status = "reviewed"
        reply = {
            "approve": draft.reply if draft else None,
            "edit": review.get("reply"),
        }.get(review["action"])
    return Result(
        id=ticket_id,
        ticket=state["ticket"],
        classification=state.get("classification"),
        draft=draft,
        citations=[
            Citation(id=c, url=by_id[c].url, heading=by_id[c].heading)
            for c in (draft.citations if draft else [])
            if c in by_id
        ],
        route="human_review" if reasons else "standard",
        reasons=reasons,
        status=status,
        review=review,
        final_reply=reply,
        usage=usage,
        latency_s=round(sum(u.latency_s for u in usage), 2),
    )


def process(ticket: str, dataset: list[Chunk], llm: LLM, settings: Settings) -> Result:
    """Runs one ticket up to the pause, without persistence. For tests and eval."""
    ticket_id = uuid.uuid4().hex[:8]
    graph = build_graph(dataset, llm, settings, InMemorySaver())
    state = graph.invoke({"ticket": ticket}, {"configurable": {"thread_id": ticket_id}})
    return to_result(ticket_id, state, dataset)
