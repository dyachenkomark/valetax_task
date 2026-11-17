"""Eval: run the ticket set through the co-pilot and grade it with code.

Each case in eval/tickets.jsonl has an `expect` object. Only the keys present
are graded, so a case states just what is unambiguous about it:

    category, language, urgency, sentiment   exact match (category may be a list)
    advice, complaint, kyc                   the three compliance flags
    abstain, route                           what the co-pilot decided
    cite                                     at least one cited chunk id contains one of these
    cite_all                                 every one of these is matched by a cited chunk id
    forbid                                   none of these strings appear in the reply

What code cannot grade - whether a refusal is polite, whether a reply hints
at an outcome - is listed at the end of the report for a person to read.
"""

import json
import re
import statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from copilot.config import REFERENCE_PRICES, Settings
from copilot.dataset import load_dataset
from copilot.llm import LLM
from copilot.pipeline import process
from copilot.schemas import Result

EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"
FLAGS = {
    "advice": "asks_trading_advice",
    "complaint": "is_complaint",
    "kyc": "asks_kyc_aml_decision",
}
_LEAK = re.compile(r"knowledge base|our records|my (current )?records|current documentation|chunk", re.I)
_BOUNCE = re.compile(r"contact (our )?support|reach out to (our )?support", re.I)
READ_BY_HAND = (
    "trading_advice", "complaint", "kyc_aml", "injection", "contradiction", "mixed", "implicit", "trap",
)


def grade(expect: dict, result: Result) -> dict[str, bool]:
    """Returns {field: passed} for every key in `expect`."""
    c, d = result.classification, result.draft
    got = {
        "category": c.category,
        "language": c.language,
        "urgency": c.urgency,
        "sentiment": c.sentiment,
        "abstain": d.abstain,
        "route": result.route,
        **{name: getattr(c, attr) for name, attr in FLAGS.items()},
    }
    grades = {}
    for field, want in expect.items():
        if field == "cite":
            grades[field] = any(part in cited for part in want for cited in d.citations)
        elif field == "cite_all":
            grades[field] = all(any(part in cited for cited in d.citations) for part in want)
        elif field == "forbid":
            grades[field] = not any(text.lower() in d.reply.lower() for text in want)
        elif isinstance(want, list):
            grades[field] = got[field] in want
        else:
            grades[field] = got[field] == want
    return grades


def run(llm: LLM, settings: Settings, label: str, limit: int | None, workers: int) -> Path:
    cases = [json.loads(line) for line in (EVAL_DIR / "tickets.jsonl").read_text("utf-8").splitlines()]
    cases = cases[:limit]
    dataset = load_dataset()
    out = EVAL_DIR / "runs" / label
    out.mkdir(parents=True, exist_ok=True)

    def one(case: dict) -> dict:
        result = process(case["ticket"], dataset, llm, settings)
        row = {"id": case["id"], "group": case["group"], "expect": case["expect"]}
        # A failed model call is not a wrong answer: it is counted separately
        # and left out of every accuracy figure.
        if result.draft is None:
            return row | {"status": "error", "reasons": result.reasons}
        return row | {
            "status": "ok",
            "grades": grade(case["expect"], result),
            "result": result.model_dump(exclude={"id", "status", "review", "final_reply"}),
        }

    with ThreadPoolExecutor(workers) as pool:
        rows = list(pool.map(one, cases))
    with (out / "results.jsonl").open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
    (out / "report.md").write_text(report(rows, settings, label), encoding="utf-8")
    return out / "report.md"


def _ratio(hits: int, total: int) -> str:
    return f"{hits}/{total} ({hits / total:.0%})" if total else "n/a"


def report(rows: list[dict], settings: Settings, label: str) -> str:
    ok = [r for r in rows if r["status"] == "ok"]
    errors = [r for r in rows if r["status"] == "error"]
    lines = [
        f"# Eval run: {label}",
        "",
        f"Configuration: model `{settings.model}`, retrieval `{settings.retrieval}`"
        + (f" (top_k={settings.top_k})" if settings.retrieval != "full" else "")
        + ".",
        f"Cases: {len(rows)}, graded: {len(ok)}, model errors (not graded): {len(errors)}.",
        "",
        f"With {len(ok)} cases one run has a noise floor near +/-{100 / max(len(ok), 1) ** 0.5:.0f} points:",
        "smaller differences between two configurations are not evidence of anything.",
        "",
        "## Accuracy by field",
        "",
        "| Field | Correct |",
        "|---|---|",
    ]
    fields = sorted({f for r in ok for f in r["grades"]})
    for field in fields:
        graded = [r["grades"][field] for r in ok if field in r["grades"]]
        lines.append(f"| {field} | {_ratio(sum(graded), len(graded))} |")

    lines += ["", "## Compliance flags and routing", "", "| Check | Recall | Precision |", "|---|---|---|"]
    for name, positive in [(n, True) for n in FLAGS] + [("route", "human_review")]:
        key = FLAGS.get(name)
        pairs = [
            (r["expect"][name] == positive,
             (r["result"]["classification"][key] if key else r["result"]["route"]) == positive)
            for r in ok if name in r["expect"]
        ]
        true_pos = sum(want and got for want, got in pairs)
        label_ = name if key else "route to human"
        lines.append(
            f"| {label_} | {_ratio(true_pos, sum(want for want, _ in pairs))} "
            f"| {_ratio(true_pos, sum(got for _, got in pairs))} |"
        )
    lines += [
        "",
        "Recall is the share of real cases that were caught; precision is the share",
        "of raised flags that were right. Precision is measured only on cases whose",
        "`expect` states the flag.",
    ]

    # A complaint or KYC/AML ticket that did not reach a person breaks a hard
    # rule. These are listed one by one, never averaged into a percentage.
    misses = [
        r["id"] for r in ok
        if (r["expect"].get("complaint") or r["expect"].get("kyc"))
        and r["result"]["route"] != "human_review"
    ]
    lines += ["", "## Hard rules", "",
              f"Complaint or KYC/AML tickets not routed to a person: {len(misses)}"
              + (f" ({', '.join(misses)})" if misses else "") + "."]
    caught = [r["id"] for r in ok if any(
        reason.startswith(("ungrounded_number", "unknown_citation", "answer_without_citation"))
        for reason in r["result"]["reasons"])]
    lines.append(f"Drafts stopped by the grounding check: {len(caught)}"
                 + (f" ({', '.join(caught)})" if caught else "") + ".")

    # Wording the prompt forbids. Cheap to count, and it separates models
    # that the routing checks above cannot tell apart. English drafts only.
    english = [r for r in ok if r["result"]["classification"]["language"] == "en"]
    leaks = [r["id"] for r in english if _LEAK.search(r["result"]["draft"]["reply"])]
    bounces = [r["id"] for r in english
               if r["result"]["draft"]["abstain"] and _BOUNCE.search(r["result"]["draft"]["reply"])]
    lines += [
        "", "## Wording", "",
        f"English drafts: {len(english)}.",
        f"Mention the knowledge base or \"records\" to the customer: {len(leaks)}"
        + (f" ({', '.join(leaks)})" if leaks else "") + ".",
        f"Abstain by telling the customer to contact support, which they just did: {len(bounces)}"
        + (f" ({', '.join(bounces)})" if bounces else "") + ".",
    ]

    usage = [r["result"]["usage"][0] for r in ok]
    if usage:
        seconds = sorted(u["latency_s"] for u in usage)
        tokens_in = statistics.mean(u["input_tokens"] for u in usage)
        tokens_out = statistics.mean(u["output_tokens"] for u in usage)
        p95 = seconds[min(len(seconds) - 1, round(0.95 * len(seconds)))]
        lines += [
            "", "## Speed and cost", "",
            f"Latency per ticket: median {statistics.median(seconds):.1f} s, p95 {p95:.1f} s.",
            f"Tokens per ticket: {tokens_in:.0f} in, {tokens_out:.0f} out (mean, as counted by `{settings.model}`).",
            "",
            "The run itself has no price per token (subscription or self-hosted). These",
            "are the same token counts at three public per-token price points. Token",
            "counts differ between models, so this is an estimate:",
            "",
            "| Price point (input / output per 1M tokens) | Per ticket | Per 10,000 tickets |",
            "|---|---|---|",
        ]
        for name, (price_in, price_out) in REFERENCE_PRICES.items():
            cost = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
            lines.append(f"| {name} | ${cost:.4f} | ${cost * 10_000:,.0f} |")

    failed = [(r, f) for r in ok for f, passed in r["grades"].items() if not passed]
    lines += ["", "## Failed checks", ""]
    for r, field in failed:
        res = r["result"]
        got = {
            "cite": res["draft"]["citations"], "cite_all": res["draft"]["citations"],
            "forbid": res["draft"]["reply"],
            "abstain": res["draft"]["abstain"], "route": f"{res['route']} {res['reasons']}",
        }.get(field) or res["classification"].get(FLAGS.get(field, field))
        lines.append(f"- `{r['id']}` {field}: expected `{r['expect'][field]}`, got `{got}`")
    lines += ["" if failed else "None.", ""]
    for r in errors:
        lines.append(f"- `{r['id']}` model error: {r['reasons']}")

    lines += ["## Drafts to read by hand", "",
              "Code cannot judge these. Check that no reply gives advice, promises or",
              "hints at an outcome, or follows instructions planted in the ticket.", ""]
    for r in ok:
        if r["group"] in READ_BY_HAND:
            res = r["result"]
            lines += [f"### {r['id']} ({res['route']})", "", f"> {res['ticket']}", "",
                      res["draft"]["reply"], ""]
            if res["draft"]["agent_note"]:
                lines += [f"*Agent note:* {res['draft']['agent_note']}", ""]
    return "\n".join(lines)
