"""Tests for everything that does not need a model: dataset, guardrails, routing, the pause."""

import json

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from copilot import rules
from copilot.config import Settings
from copilot.eval import EVAL_DIR, grade
from copilot.dataset import bm25_top, load_dataset
from copilot.llm import ChatLLM, LLMError
from copilot.pipeline import build_graph, process, to_result
from copilot.schemas import Classification, Draft, TriageAndDraft, Usage

DATASET = load_dataset()
MIN_DEPOSIT = "how-to-deposit#what-is-the-minimum-deposit-amount"
PENDING = "how-to-deposit#what-is-my-withdrawal-request-status-pending"


def classification(**overrides) -> Classification:
    base = dict(
        category="deposit", urgency="normal", language="en", sentiment="neutral",
        asks_trading_advice=False, is_complaint=False, asks_kyc_aml_decision=False,
    )
    return Classification(**(base | overrides))


def draft(**overrides) -> Draft:
    base = dict(reply="The minimum deposit starts from 8 USD.", citations=[MIN_DEPOSIT], abstain=False)
    return Draft(**(base | overrides))


class FakeLLM:
    """Returns a prepared answer, or raises a prepared error."""

    def __init__(self, c=None, d=None, error=None):
        self.error = error
        self.answer = TriageAndDraft(classification=c or classification(), draft=d or draft())

    def parse(self, *, model, system, user, schema):
        if self.error:
            raise self.error
        return self.answer, Usage(model=model)


def run(ticket="What is the minimum deposit?", c=None, d=None, error=None, **settings):
    return process(ticket, DATASET, FakeLLM(c, d, error), Settings(**settings))


# --- knowledge base

def test_kb_has_all_pages_and_unique_ids():
    ids = [s.id for s in DATASET]
    assert len(ids) == len(set(ids))
    assert {i.split("#")[0] for i in ids} == {
        "how-to-deposit", "how-to-withdraw", "how-to-open-an-account", "how-to-verify-account",
    }
    assert MIN_DEPOSIT in ids


def test_bm25_finds_the_relevant_section():
    top = bm25_top(DATASET, "documents required to open an account", k=3)
    assert "how-to-open-an-account#do-i-need-to-provide-any-documents-to-open-an-account" in [s.id for s in top]


# --- routing

def test_grounded_answer_goes_to_standard_queue():
    result = run()
    assert (result.route, result.status, result.reasons) == ("standard", "ready", [])
    assert result.final_reply == result.draft.reply
    assert result.citations[0].url == "https://valetax.com/how-to-deposit/"


def test_complaint_always_goes_to_human():
    result = run(c=classification(is_complaint=True))
    assert "complaint" in result.reasons and result.status == "awaiting_review"
    assert result.final_reply is None  # nothing reaches the customer before a person decides


def test_kyc_aml_always_goes_to_human():
    assert "kyc_aml" in run(c=classification(asks_kyc_aml_decision=True)).reasons


def test_keyword_backstop_overrides_a_classifier_miss():
    result = run(ticket="This is a scam, I will contact the regulator. My account was frozen.")
    assert {"complaint", "kyc_aml"} <= set(result.reasons)


def test_abstain_goes_to_human():
    result = run(d=draft(reply="Our team will check and get back to you.", citations=[], abstain=True))
    assert result.reasons == ["kb_gap"]


def test_llm_failure_goes_to_human_and_keeps_the_ticket():
    result = run(error=LLMError("stop_reason=refusal"))
    assert result.route == "human_review" and result.ticket and result.draft is None
    assert result.reasons == ["llm_error: stop_reason=refusal"]


# --- the pause: a person resumes the ticket later

def review(decision, c=None):
    """Runs a complaint to the pause, then resumes it the way `copilot review` does."""
    graph = build_graph(DATASET, FakeLLM(c or classification(is_complaint=True)), Settings(), InMemorySaver())
    config = {"configurable": {"thread_id": "t1"}}
    graph.invoke({"ticket": "I want a refund"}, config)
    assert graph.get_state(config).next == ("human_review",)
    return to_result("t1", graph.invoke(Command(resume=decision), config), DATASET)


def test_reviewer_can_approve_edit_or_reject():
    assert review({"action": "approve"}).final_reply == draft().reply
    edited = review({"action": "edit", "reply": "A specialist will contact you."})
    assert (edited.status, edited.final_reply) == ("reviewed", "A specialist will contact you.")
    assert review({"action": "reject"}).final_reply is None


# --- draft validation

def test_invented_number_is_caught():
    result = run(d=draft(reply="The minimum deposit is 8 USD and the fee is 2.5%."))
    assert result.reasons == ["ungrounded_number: 2.5"]


def test_number_from_another_section_must_be_cited():
    # "24 hours" is in the DATASET, but not in the chunk the draft cites.
    result = run(d=draft(reply="From 8 USD. Withdrawals arrive within 24 hours."))
    assert result.reasons == ["ungrounded_number: 24"]


def test_customer_numbers_and_list_markers_are_not_flagged():
    reply = "1. Log in\n2. Your request for 500 USD is in the queue."
    result = run(ticket="I withdrew 500 USD", d=draft(reply=reply, citations=[PENDING]))
    assert result.reasons == []


def test_unknown_citation_and_missing_citation_are_caught():
    assert "unknown_citation: fees#withdrawal" in run(d=draft(citations=["fees#withdrawal"])).reasons[0]
    assert run(d=draft(reply="Yes.", citations=[])).reasons == ["answer_without_citation"]


def test_advice_refusal_needs_no_citation_but_still_no_invented_numbers():
    advice = classification(asks_trading_advice=True)
    refusal = draft(reply="We cannot give trading advice.", citations=[])
    assert run(c=advice, d=refusal).route == "standard"
    smuggled = draft(reply="We cannot give advice, but 1:500 is popular.", citations=[])
    assert run(c=advice, d=smuggled).reasons == ["ungrounded_number: 1:500"]


def test_bm25_mode_rejects_citation_outside_retrieved_chunks():
    # In bm25 mode the model only saw top_k chunks; citing anything else is invented.
    result = run(ticket="Do you have an Islamic swap-free account?", top_k=1, retrieval="bm25")
    assert result.reasons[0].startswith("unknown_citation")


def test_backstop_flags():
    assert rules.keyword_flags("This is fraud, I want compensation") == {"complaint"}
    assert rules.keyword_flags("How do I deposit?") == set()


# --- the eval's own grader and labels

def test_grader_passes_an_ideal_result_and_fails_a_wrong_one():
    expect = {
        "category": ["deposit", "withdrawal"], "language": "en", "advice": False, "complaint": False,
        "abstain": False, "route": "standard", "cite": ["#what-is-the-minimum-deposit-amount"], "forbid": ["1 USD"],
    }
    assert all(grade(expect, run()).values())
    wrong = run(
        c=classification(category="other", language="ru", asks_trading_advice=True, is_complaint=True),
        d=draft(reply="It is 1 USD.", citations=[PENDING], abstain=True),
    )
    assert not any(grade(expect, wrong).values())


def test_every_expected_citation_exists_in_the_dataset():
    ids = [chunk.id for chunk in DATASET]
    for line in (EVAL_DIR / "tickets.jsonl").read_text("utf-8").splitlines():
        case = json.loads(line)
        for part in case["expect"].get("cite", []) + case["expect"].get("cite_all", []):
            assert any(part in chunk_id for chunk_id in ids), f"{case['id']}: {part}"


# --- Kimi provider (no schema enforcement on the server, so parsing is ours)

def kimi(monkeypatch, replies):
    """ChatLLM whose HTTP layer returns `replies` in order."""
    monkeypatch.setenv("LLM_API_KEY", "test")
    queue = iter(replies)

    def handler(request):
        reply = next(queue)
        if isinstance(reply, int):
            return httpx.Response(reply)
        body = {"choices": [{"message": {"content": reply}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        return httpx.Response(200, json=body)

    return ChatLLM(transport=httpx.MockTransport(handler))


def kimi_parse(llm):
    return llm.parse(model="k3", system=["rules"], user="ticket", schema=Draft)


def test_kimi_parses_fenced_json_and_counts_tokens(monkeypatch):
    fenced = "```json\n" + draft().model_dump_json() + "\n```"
    parsed, usage = kimi_parse(kimi(monkeypatch, [fenced]))
    assert parsed.citations == [MIN_DEPOSIT] and usage.input_tokens == 10


def test_kimi_retries_once_on_invalid_json(monkeypatch):
    parsed, usage = kimi_parse(kimi(monkeypatch, ['{"reply": "no other fields"}', draft().model_dump_json()]))
    assert parsed.abstain is False and usage.output_tokens == 10


def test_kimi_failures_become_llm_error(monkeypatch):
    for replies in ([500], ["not json", "still not json"]):
        with pytest.raises(LLMError):
            kimi_parse(kimi(monkeypatch, replies))
