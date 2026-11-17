"""The dataset: dataset/*.md -> chunks, plus chunk selection.

A chunk is one step or one FAQ entry. It is the unit the model sees,
cites, and the unit BM25 ranks.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

DATASET_DIR = Path(__file__).resolve().parent.parent / "dataset"


@dataclass(frozen=True)
class Chunk:
    id: str  # "<page>#<heading-slug>", stable while the heading is unchanged
    url: str
    page: str
    heading: str
    text: str

    def render(self) -> str:
        body = f"\n{self.text}" if self.text else ""
        return f'<chunk id="{self.id}" page="{self.page}">\n{self.heading}{body}\n</chunk>'


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def load_dataset(directory: Path = DATASET_DIR) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(directory.glob("*.md")):
        _, front, body = path.read_text(encoding="utf-8").split("---\n", 2)
        meta = dict(line.split(": ", 1) for line in front.strip().splitlines())
        heading, lines = "", []

        def flush() -> None:
            if not heading:
                return
            name = "intro" if heading == meta["title"] else _slug(heading)
            chunks.append(
                Chunk(
                    id=f"{path.stem}#{name}",
                    url=meta["source"],
                    page=meta["title"],
                    heading=heading,
                    text="\n".join(lines).strip(),
                )
            )

        for line in body.splitlines():
            if line.startswith("## "):
                continue  # group title ("Steps", "Got a question...") carries no facts
            if line.startswith("# ") or line.startswith("### "):
                flush()
                heading, lines = line.lstrip("# "), []
            else:
                lines.append(line)
        flush()
    return chunks


_STOPWORDS = frozenset(
    "a an and are as at be by can do does for from how i in is it my of on or "
    "the to what when where will with you your".split()
)


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOPWORDS]


def bm25_top(chunks: list[Chunk], query: str, k: int) -> list[Chunk]:
    """Plain BM25 over heading + text. Query must be English, like the dataset."""
    docs = [_tokens(f"{c.page} {c.heading} {c.text}") for c in chunks]
    avg_len = sum(map(len, docs)) / len(docs)
    df = Counter(term for doc in docs for term in set(doc))
    scores = []
    for doc in docs:
        tf = Counter(doc)
        score = 0.0
        for term in set(_tokens(query)):
            if term not in tf:
                continue
            idf = math.log(1 + (len(docs) - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * tf[term] * 2.5 / (tf[term] + 1.5 * (0.25 + 0.75 * len(doc) / avg_len))
        scores.append(score)
    ranked = sorted(range(len(chunks)), key=lambda i: -scores[i])
    return [chunks[i] for i in ranked[:k] if scores[i] > 0]


def select(chunks: list[Chunk], query: str, mode: str, k: int) -> list[Chunk]:
    if mode == "full":
        return chunks
    if mode == "bm25":
        return bm25_top(chunks, query, k)
    raise ValueError(f"unknown retrieval mode: {mode}")
