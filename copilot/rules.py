"""Deterministic guardrails: no LLM here.

The task's hard rules say "never". A prompt cannot guarantee "never", so the
prompt asks for the right behaviour and this module decides the route and
checks the draft. Everything here is covered by plain unit tests.
"""

import re

from copilot.dataset import Chunk
from copilot.schemas import Classification, Draft

# Keyword backstop for the classifier. It can only add a flag, never clear
# one: a false positive costs one extra human review, a false negative breaks
# a hard rule. English only - other languages rely on the classifier.
_BACKSTOP = {
    "complaint": re.compile(
        r"\b(complain\w*|scam\w*|fraud\w*|lawyer|legal action|regulator|chargeback|sue|compensation)\b",
        re.I,
    ),
    "kyc_aml": re.compile(
        r"\b(aml|money laundering|sanctions?|source of funds|account (is |was |has been )?(blocked|frozen|suspended))\b",
        re.I,
    ),
}


def keyword_flags(ticket: str) -> set[str]:
    return {name for name, pattern in _BACKSTOP.items() if pattern.search(ticket)}


_NUMBER = re.compile(r"\d+(?:[.,:]\d+)*")
_LIST_MARKER = re.compile(r"(?m)^\s*\d+[.)]\s")


def _numbers(text: str) -> set[str]:
    return set(_NUMBER.findall(_LIST_MARKER.sub("", text)))


def validate_draft(
    draft: Draft, chunks: list[Chunk], ticket: str, declines_advice: bool = False
) -> list[str]:
    """Checks that the draft is grounded. Returns a list of problems, empty if none.

    A polite refusal to give trading advice states no facts, so it needs no
    citation. Numbers are still checked: a refusal must not smuggle one in.
    """
    issues = []
    by_id = {chunk.id: chunk for chunk in chunks}
    unknown = [c for c in draft.citations if c not in by_id]
    if unknown:
        issues.append(f"unknown_citation: {', '.join(unknown)}")
    if not draft.abstain and not draft.citations and not declines_advice:
        issues.append("answer_without_citation")
    # Fees, limits and timelines are numbers. Each number in the reply must
    # come from a cited chunk or from the customer's own message.
    cited = " ".join(f"{by_id[c].heading} {by_id[c].text}" for c in draft.citations if c in by_id)
    invented = _numbers(draft.reply) - _numbers(cited) - _numbers(ticket)
    if invented:
        issues.append(f"ungrounded_number: {', '.join(sorted(invented))}")
    return issues


def decide_route(
    classification: Classification, flags: set[str], draft: Draft, issues: list[str]
) -> list[str]:
    """Returns the reasons a human must handle the ticket. Empty = standard queue."""
    reasons = []
    if classification.is_complaint or "complaint" in flags:
        reasons.append("complaint")
    if classification.asks_kyc_aml_decision or "kyc_aml" in flags:
        reasons.append("kyc_aml")
    if classification.urgency == "high":
        reasons.append("high_urgency")
    if draft.abstain:
        reasons.append("kb_gap")
    reasons.extend(issues)
    return reasons
