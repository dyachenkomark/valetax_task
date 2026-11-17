"""CLI.

    python -m copilot ticket "How long does a withdrawal take?"
    python -m copilot review <id> --approve | --edit "new reply" | --reject
    python -m copilot eval --label k3-full

`ticket` runs the graph. If the ticket needs a person, the graph pauses and
its state stays in SQLite; `review`, a separate process, resumes it.
"""

import argparse
import sqlite3
import sys
import uuid
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from copilot.config import Settings
from copilot.dataset import load_dataset
from copilot.eval import run as run_eval
from copilot.llm import ChatLLM
from copilot.pipeline import build_graph, to_result
from copilot.schemas import Result

STATE_DB = Path(__file__).resolve().parent.parent / "state" / "tickets.sqlite"


def render(result: Result) -> str:
    """The result as an agent would want to read it."""
    c, d = result.classification, result.draft
    lines = [f"Ticket {result.id}", f"  {result.ticket}", ""]
    if c:
        flags = [name for name, on in (("trading advice", c.asks_trading_advice),
                                       ("complaint", c.is_complaint),
                                       ("KYC/AML", c.asks_kyc_aml_decision)) if on]
        lines.append(f"Category: {c.category} | urgency: {c.urgency} | language: {c.language} "
                     f"| sentiment: {c.sentiment} | flags: {', '.join(flags) or 'none'}")
    if result.route == "standard":
        lines.append("Route: STANDARD QUEUE - an agent checks and sends the draft")
    else:
        lines.append(f"Route: HUMAN REVIEW - {', '.join(result.reasons)}")
    if d:
        lines += ["", "Draft reply", *(f"  {line}" for line in d.reply.splitlines())]
        if result.citations:
            lines += ["", "Sources", *(f"  - {s.heading} ({s.url})" for s in result.citations)]
        if d.missing:
            lines += ["", f"Not in the knowledge base: {d.missing}"]
        if d.agent_note:
            lines += ["", f"Note for the agent: {d.agent_note}"]
    if result.status == "awaiting_review":
        lines += ["", f"Waiting for a person: python -m copilot review {result.id} "
                      '--approve | --edit "..." | --reject']
    elif result.status == "reviewed":
        lines += ["", f"Reviewed: {result.review['action']}",
                  f"Goes to the customer: {result.final_reply or 'nothing, the draft was rejected'}"]
    for u in result.usage:
        lines += ["", f"{u.model}: {u.input_tokens} tokens in, {u.output_tokens} out, {u.latency_s} s"]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(prog="copilot")
    commands = parser.add_subparsers(dest="command", required=True)
    ticket = commands.add_parser("ticket", help="process one ticket and print the result")
    ticket.add_argument("text", nargs="?", help="ticket text; omit to read from --file or stdin")
    ticket.add_argument("--file", type=Path)
    review = commands.add_parser("review", help="resume a ticket that is waiting for a person")
    review.add_argument("id")
    for command in (ticket, review):
        command.add_argument("--json", action="store_true", help="print the full result as JSON")
    decision = review.add_mutually_exclusive_group(required=True)
    decision.add_argument("--approve", action="store_true", help="send the draft as is")
    decision.add_argument("--edit", metavar="REPLY", help="send this text instead of the draft")
    decision.add_argument("--reject", action="store_true", help="discard the draft")
    evaluate = commands.add_parser("eval", help="run eval/tickets.jsonl and write a report")
    evaluate.add_argument("--label", required=True, help="name of the run, e.g. k3-full")
    evaluate.add_argument("--retrieval", choices=["full", "bm25"])
    evaluate.add_argument("--model")
    evaluate.add_argument("--limit", type=int, help="only the first N cases")
    evaluate.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    load_dotenv()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.command == "eval":
        overrides = {k: v for k in ("retrieval", "model") if (v := getattr(args, k))}
        settings = replace(Settings.from_env(), **overrides)
        print(run_eval(ChatLLM(), settings, args.label, args.limit, args.workers))
        return
    dataset = load_dataset()
    STATE_DB.parent.mkdir(exist_ok=True)
    saver = SqliteSaver(sqlite3.connect(STATE_DB, check_same_thread=False))
    graph = build_graph(dataset, ChatLLM(), Settings.from_env(), saver)

    if args.command == "ticket":
        text = args.text or (args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read())
        ticket_id = uuid.uuid4().hex[:8]
        config = {"configurable": {"thread_id": ticket_id}}
        state = graph.invoke({"ticket": text.strip()}, config)
    else:
        ticket_id = args.id
        config = {"configurable": {"thread_id": ticket_id}}
        if graph.get_state(config).next != ("human_review",):
            parser.error(f"ticket {ticket_id} is not waiting for review")
        if args.approve:
            answer = {"action": "approve"}
        elif args.reject:
            answer = {"action": "reject"}
        else:
            answer = {"action": "edit", "reply": args.edit}
        state = graph.invoke(Command(resume=answer), config)

    result = to_result(ticket_id, state, dataset)
    print(result.model_dump_json(indent=2) if args.json else render(result))


if __name__ == "__main__":
    main()
