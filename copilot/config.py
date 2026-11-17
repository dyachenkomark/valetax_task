"""One configuration = model x retrieval mode."""

import os
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Settings:
    model: str = "k3"
    retrieval: str = "full"  # "full": whole dataset in the prompt; "bm25": top_k chunks
    top_k: int = 6

    @classmethod
    def from_env(cls) -> "Settings":
        """Reads COPILOT_<FIELD> overrides, e.g. COPILOT_RETRIEVAL=bm25."""
        overrides = {}
        for field in fields(cls):
            raw = os.environ.get(f"COPILOT_{field.name.upper()}")
            if raw:
                overrides[field.name] = type(field.default)(raw)
        return cls(**overrides)


# Kimi K3 runs on a flat subscription, so it has no price per token. To put a
# dollar figure on a ticket, the eval report prices the measured token counts
# at three public per-token price points: USD per 1M tokens (input, output),
# list prices of a small, a mid and a large hosted model as of 2026-11-03.
REFERENCE_PRICES: dict[str, tuple[float, float]] = {
    "small model ($0.10 / $0.50)": (0.10, 0.50),
    "mid model ($2 / $10)": (2.00, 10.00),
    "large model ($4 / $20)": (4.00, 20.00),
}
