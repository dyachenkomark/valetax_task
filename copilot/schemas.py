"""Data contracts: what the LLM must return and what the co-pilot outputs."""

from typing import Literal

from pydantic import BaseModel, Field


class Classification(BaseModel):
    category: Literal["deposit", "withdrawal", "account_opening", "verification", "other"]
    urgency: Literal["low", "normal", "high"] = Field(
        description="high: money missing or stuck, account locked, or the customer "
        "reports a deadline or repeated unanswered contact"
    )
    language: str = Field(description="ISO 639-1 code of the ticket language, e.g. en, ru, es")
    sentiment: Literal["negative", "neutral", "positive"]
    asks_trading_advice: bool = Field(
        description="Asks what to buy or sell, for signals, price targets, forecasts, "
        "or which leverage or strategy to choose"
    )
    is_complaint: bool = Field(
        description="Expresses a grievance about the company or demands redress: "
        "refund, compensation, escalation, threat of legal or regulator action"
    )
    asks_kyc_aml_decision: bool = Field(
        description="Asks to approve, reverse, speed up or explain the outcome of an "
        "identity verification, document check, account block or AML review"
    )


class Draft(BaseModel):
    reply: str = Field(description="Reply to the customer, in the customer's language, plain text")
    citations: list[str] = Field(description="Ids of the chunks the reply relies on")
    abstain: bool = Field(
        description="true if the knowledge base does not answer the customer's main question"
    )
    missing: str | None = Field(
        default=None, description="If abstain: what information the knowledge base lacks"
    )
    agent_note: str | None = Field(
        default=None,
        description="Internal note for the support agent: contradictions between "
        "chunks, caveats, what to check before sending",
    )


class TriageAndDraft(BaseModel):
    """What one LLM call returns. Field order is generation order: triage, then reply."""

    classification: Classification
    draft: Draft


class Usage(BaseModel):
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0


class Citation(BaseModel):
    id: str
    url: str
    heading: str


class Result(BaseModel):
    id: str  # thread id: `copilot review <id>` resumes a paused ticket
    ticket: str
    classification: Classification | None = None
    draft: Draft | None = None
    citations: list[Citation] = []
    route: Literal["standard", "human_review"]
    # Why the ticket needs a person. Empty for the standard queue.
    reasons: list[str] = []
    status: Literal["ready", "awaiting_review", "reviewed"]
    review: dict | None = None
    # What goes to the customer: the draft, the reviewer's edit, or nothing yet.
    final_reply: str | None = None
    usage: list[Usage] = []
    latency_s: float = 0.0
