# Business case

Is the co-pilot worth running at 10,000 tickets a month, and in which
configuration?

Yes, with a modest return at this volume. On published evidence it frees about
100 agent-hours a month, worth roughly $1,200, for a model bill of $5 to $217.
That covers its upkeep and little more. The return grows with ticket volume,
because the saving scales and the upkeep mostly does not. Run one LLM call per
ticket with the whole knowledge base in the prompt, on Kimi K3.

## Cost

Measured on 89 tickets (`eval/runs/k3-full/report.md`): about 4,100 input and
260 output tokens per ticket, one call, median 10 s, p95 19 s.

Kimi K3 ran on a flat subscription with usage limits, so the run has no price
per ticket, and that plan is not sized for 10,000 tickets a month (about 44
million tokens). Production needs a per-token contract. These are the measured
tokens at three public per-token price points:

| Price point (input / output per 1M tokens) | Per ticket | 10,000 tickets a month |
|---|---|---|
| Small hosted model ($0.10 / $0.50) | $0.0005 | $5 |
| Mid hosted model ($2 / $10) | $0.011 | $108 |
| Large hosted model ($4 / $20) | $0.022 | $217 |

List prices as of November 2026, without prompt caching or batch discounts.
Token counts differ between models, so these are estimates.

## Saving

The saving is agent time. None of these inputs is measured at Valetax.

| Input | Value | Basis |
|---|---|---|
| Agent time per ticket today | 5 min | Vendor benchmarks put active time on an email ticket at 4 to 6 minutes |
| Loaded cost of an agent | $12 an hour | Vendor benchmarks put offshore support at $8 to $18 an hour |
| Productivity gain from AI assistance | 14% more tickets per hour | Brynjolfsson, Li and Raymond, "Generative AI at Work": 5,172 support agents, 14 to 15% on average, 34% for novices, close to zero for the most experienced |

10,000 tickets x 5 min is 833 agent-hours a month. A 14% gain in tickets per
hour cuts the time per ticket by 12%, which frees about 100 hours, 0.6 of a
full-time agent.

| | Low | Base | High |
|---|---|---|---|
| Productivity gain | 7%, half the study | 14%, the study average | 34%, the study's novices |
| Agent-hours freed a month | 55 | 100 | 210 |
| Value at $12 an hour | $650 | $1,230 | $2,540 |

The study covered chat support at a software company. Email tickets at a
broker may behave differently, and that is what the pilot is for.

## What decides it

| Factor | Effect |
|---|---|
| Model price | Small. An agent costs $0.20 a minute, so the most expensive price point pays for itself by saving 7 seconds per ticket. Cheapest to most expensive is $212 a month. The model should be chosen on quality |
| Draft acceptance | Decisive. If agents rewrite most drafts, the minutes saved go to zero. The share of drafts approved, edited and rejected is the first thing to measure, and `review` already records it |
| Upkeep | Someone keeps the ticket set current, re-runs it when a page or a rule changes, and reads agents' edits. The base case leaves about $1,000 a month for that after the model bill, which is a few hours of an engineer's week |
| Volume | At 50,000 tickets a month the same gain frees about 500 hours, and upkeep is nearly unchanged |
| Experience of the team | The study found the gain concentrated in new agents. A team with high turnover or many recent hires gets more |
| A wrong reply | One reply that gives trading advice or promises a KYC outcome can cost more than a year of savings. Complaint and KYC/AML tickets therefore always go to a person, and an agent sends every reply |

## Self-hosting the model

A self-hosted Gemma 12B ran the same 89 tickets. It has no bill per ticket,
only a GPU server, power and someone to run it.

At this volume that is not cheaper. The fair comparison for a 12B model is a
hosted small model, which costs about $5 a month for the same tickets and
about $55 at ten times the volume. A server bought for about $1,500 (an
assumption, not a quote) does not pay that back. It would pay back only
against the mid price point ($108 a month), in 14 months before power and
administration.

The reason to self-host at 10,000 tickets is data. Ticket text with account
and document details stays inside the company, which matters for a regulated
broker. The eval shows the price: Gemma triaged as well as Kimi K3 and drafted
worse. It obeyed one planted instruction and mentioned the knowledge base to
the customer in 15 of 76 drafts, against 3.

## Configuration

| Choice | Phase 1 | When it changes |
|---|---|---|
| LLM calls per ticket | One | Two when search needs a query written by the model |
| Knowledge base | Whole, in the prompt. BM25 search halved the tokens and sent every answerable non-English ticket to a person | Search when the knowledge base grows about tenfold. At 40,000 input tokens a ticket costs $0.16 at $4 per 1M tokens, $1,600 a month |
| Model | Kimi K3 | A small model for triage and a strong one for drafting, once agent edits show which tickets are easy |
| Who sends | An agent, always | Auto-sending is not proposed. It needs measured acceptance and error rates first |

## What a two-week pilot should answer

1. What share of drafts agents approve unchanged.
2. Real handling time with and without a draft.
3. What share of real tickets is routine. The eval set is heavy on hard cases
   and says nothing about the real mix.
4. Whether any draft that reached an agent broke a hard rule.

If the measured gain is at the low column or below, stop at this volume. $650
a month is unlikely to cover upkeep.

Sources: [Generative AI at Work](https://www.nber.org/system/files/working_papers/w31161/w31161.pdf)
(NBER working paper 31161, later in the Quarterly Journal of Economics). The
handle-time and wage ranges come from vendor benchmark pages and are
directional.
