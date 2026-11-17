"""Snapshot the four public Valetax help pages into dataset/*.md.

The text is copied verbatim, including typos, overlaps and contradictions:
the co-pilot must work with the knowledge base as it is. Only the layout is
ours: every step and every FAQ entry becomes a `###` chunk, which is the
unit the co-pilot retrieves and cites.

Stdlib only. Run: python scripts/fetch_dataset.py
In production this would be a scheduled sync with a content hash per page,
so a changed page triggers re-indexing and a re-run of the eval set.
"""

import re
import urllib.request
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

PAGES = [
    "how-to-deposit",
    "how-to-withdraw",
    "how-to-open-an-account",
    "how-to-verify-account",
]
BASE = "https://valetax.com"
DATASET_DIR = Path(__file__).resolve().parent.parent / "dataset"

BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li"}
VOID_TAGS = {"img", "br", "hr", "input", "meta", "link", "source", "wbr"}
SKIP_TAGS = {"script", "style", "svg", "noscript"}
# Layout containers of the help pages, innermost match wins.
REGIONS = (
    "seps-list__label",
    "seps-list__item",
    "accordion__title",
    "accordion__item",
    "page__content",
    "trade-device",  # promo banner, not knowledge
)


class PageParser(HTMLParser):
    """Turns <main> into a flat list of (region, tag, text) blocks."""

    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[tuple[str, str, str]] = []
        self._stack: list[tuple[str, str]] = []  # (tag, region or "")
        self._in_main = False
        self._skip = 0
        self._block: str | None = None
        self._buf: list[str] = []
        self._href: str | None = None

    def _region(self) -> str:
        for _, region in reversed(self._stack):
            if region:
                return region
        return ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "main":
            self._in_main = True
        if not self._in_main:
            return
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if self._skip or tag in VOID_TAGS:
            return
        classes = (attrs.get("class") or "").split()
        region = next((r for r in REGIONS if r in classes), "")
        self._stack.append((tag, region))
        if tag in BLOCK_TAGS and self._block is None:
            self._block, self._buf = tag, []
        elif tag == "a" and self._block:
            self._href = attrs.get("href")
            self._buf.append("[")

    def handle_endtag(self, tag):
        if not self._in_main:
            return
        if tag in SKIP_TAGS:
            self._skip -= 1
            return
        if self._skip or tag in VOID_TAGS:
            return
        if tag == "a" and self._block and self._href is not None:
            self._buf.append(f"]({self._href})")
            self._href = None
        if tag == self._block:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if text:
                # Region is read before the block's own tag is popped.
                self.blocks.append((self._region(), tag, text))
            self._block = None
        # Pop up to the matching tag: tolerates unclosed tags in the source.
        while self._stack:
            if self._stack.pop()[0] == tag:
                break
        if tag == "main":
            self._in_main = False

    def handle_data(self, data):
        if self._in_main and not self._skip and self._block:
            self._buf.append(data)


def to_markdown(blocks: list[tuple[str, str, str]]) -> tuple[str, str]:
    """Returns (title, markdown body)."""
    title = ""
    out: list[str] = []
    label = ""
    steps_started = False
    for region, tag, text in blocks:
        if region == "trade-device":
            continue
        if region == "seps-list__label":
            label = text
        elif region == "seps-list__item" and tag.startswith("h"):
            if not steps_started:
                out.append("## Steps")
                steps_started = True
            out.append(f"### {label}: {text}" if label else f"### {text}")
            label = ""
        elif region == "accordion__title":
            out.append(f"## {text}")
        elif region == "accordion__item" and tag.startswith("h"):
            out.append(f"### {text}")
        elif tag == "h1":
            title = text
            out.append(f"# {text}")
        elif tag == "li":
            out.append(f"- {text}")
        elif tag.startswith("h"):
            # Unknown layout: keep the heading rather than lose content.
            out.append(f"{'#' * max(2, int(tag[1]))} {text}")
        else:
            out.append(text)
    lines: list[str] = []
    for i, line in enumerate(out):
        # Blank line between blocks, except inside a list.
        if i and not (line.startswith("- ") and out[i - 1].startswith("- ")):
            lines.append("")
        lines.append(line)
    return title, "\n".join(lines) + "\n"


def fetch(slug: str) -> str:
    request = urllib.request.Request(
        f"{BASE}/{slug}/", headers={"User-Agent": "Mozilla/5.0 (dataset-fetch)"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def main() -> None:
    DATASET_DIR.mkdir(exist_ok=True)
    for slug in PAGES:
        parser = PageParser()
        parser.feed(fetch(slug))
        title, body = to_markdown(parser.blocks)
        front = (
            "---\n"
            f"source: {BASE}/{slug}/\n"
            f"title: {title}\n"
            f"fetched: {date.today().isoformat()}\n"
            "---\n\n"
        )
        path = DATASET_DIR / f"{slug}.md"
        path.write_text(front + body, encoding="utf-8")
        print(f"{path.name}: {len(parser.blocks)} blocks, {len(body)} chars")


if __name__ == "__main__":
    main()
