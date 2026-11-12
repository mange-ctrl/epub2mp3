"""Plain-text clean-up, sentence splitting and TTS chunking."""

from __future__ import annotations

import re

# Words whose trailing period must not end a sentence.
_ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "fig",
    "no", "vol", "pp", "ch", "cf", "al", "ca", "approx", "dept", "est",
    "inc", "ltd", "co", "corp", "jan", "feb", "mar", "apr", "jun", "jul",
    "aug", "sep", "sept", "oct", "nov", "dec", "mt", "ft", "gen", "col",
    "lt", "sgt", "capt", "rev", "hon", "pres", "gov", "sen", "rep", "messrs",
    "mme", "mlle", "p", "e.g", "i.e", "a.m", "p.m", "u.s", "u.k",
}

_PAGE_NUMBER = re.compile(r"\d{1,4}")
_PAGE_LABEL = re.compile(r"(?:page|p\.)\s*\d{1,4}", re.IGNORECASE)
_BRACKET_IMAGE = re.compile(
    r"\[\s*(?:illustration|image|figure|picture|photo|diagram|map|chart)[^\]]*\]",
    re.IGNORECASE,
)
_SENT_BOUNDARY = re.compile(r"([.!?…]+[\"'”’\)\]]*)(\s+)")


def clean_text(raw: str, *, strip_gutenberg: bool = True) -> str:
    """Turn extracted markup text into clean paragraphs separated by blanks."""
    if not raw:
        return ""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = (
        text.replace("\u00a0", " ")
        .replace("\u00ad", "")
        .replace("\u2007", " ")
        .replace("\u202f", " ")
    )
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\ufeff]", "", text)

    if strip_gutenberg:
        text = re.sub(
            r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*\*\*\*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*\*\*\*.*\Z",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(
            r"^\s*(?:produced by|updated editions will replace)[^\n]*\n",
            "",
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )

    lines = [ln.strip() for ln in text.split("\n")]
    paragraphs: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if not buf:
            return
        joined = ""
        for piece in buf:
            if not joined:
                joined = piece
            elif joined.endswith("-") and piece[:1].islower():
                joined = joined[:-1] + piece          # de-hyphenate line wraps
            else:
                joined += " " + piece
        joined = " ".join(joined.split())
        if joined:
            paragraphs.append(joined)
        buf.clear()

    for line in lines:
        if not line:
            flush()
            continue
        if paragraphs and (_PAGE_NUMBER.fullmatch(line) or _PAGE_LABEL.fullmatch(line)):
            continue
        if _BRACKET_IMAGE.fullmatch(line):
            continue
        buf.append(line)
    flush()

    out = "\n\n".join(paragraphs)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _ends_with_abbreviation(candidate: str) -> bool:
    match = re.search(r"([A-Za-z][A-Za-z.]*)\.$", candidate)
    if not match:
        return False
    word = match.group(1).lower().rstrip(".")
    if word in _ABBREV:
        return True
    # Single initials such as "J. R. R."
    return len(match.group(1)) == 1


def split_sentences(text: str) -> list[str]:
    """Split a paragraph into sentences, tolerating abbreviations and quotes."""
    text = text.strip()
    if not text:
        return []
    sentences: list[str] = []
    start = 0
    for match in _SENT_BOUNDARY.finditer(text):
        following = text[match.end():match.end() + 1]
        if following and not (following.isupper() or following.isdigit()
                              or following in "\"'“”‘’(["):
            continue
        candidate = text[start:match.end(1)].strip()
        if not candidate or _ends_with_abbreviation(candidate):
            continue
        sentences.append(candidate)
        start = match.end()
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences or [text]


def _hard_split(text: str, limit: int) -> list[str]:
    """Split an over-long sentence on word boundaries."""
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for word in text.split():
        if current and size + len(word) + 1 > limit:
            pieces.append(" ".join(current))
            current, size = [], 0
        current.append(word)
        size += len(word) + 1
    if current:
        pieces.append(" ".join(current))
    return pieces


def chunk_paragraphs(
    text: str,
    max_chars: int = 900,
    paragraph_pause_ms: int = 260,
) -> list[tuple[str, int]]:
    """Split chapter text into TTS chunks.

    Short paragraphs are packed together (joined with a newline, which the
    neural voices read as a soft pause) so a book does not turn into thousands
    of tiny speech requests.  Returns ``(chunk_text, pause_after_ms)`` pairs.
    """
    chunks: list[tuple[str, int]] = []
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]

    current: list[str] = []
    current_size = 0

    def flush(pause: int) -> None:
        nonlocal current_size
        if current:
            chunks.append(("\n".join(current), pause))
            current.clear()
            current_size = 0

    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            if current and current_size + len(paragraph) + 1 > max_chars:
                flush(paragraph_pause_ms)
            current.append(paragraph)
            current_size += len(paragraph) + 1
            continue

        # An over-long paragraph: finish what we have, then split by sentence.
        flush(paragraph_pause_ms)
        sentences = split_sentences(paragraph)
        sentence_buf: list[str] = []
        sentence_size = 0

        def flush_sentences(pause: int) -> None:
            nonlocal sentence_size
            if sentence_buf:
                chunks.append((" ".join(sentence_buf), pause))
                sentence_buf.clear()
                sentence_size = 0

        for sentence in sentences:
            if len(sentence) > max_chars * 1.2:
                flush_sentences(0)
                for piece in _hard_split(sentence, max_chars):
                    chunks.append((piece, 0))
                continue
            if sentence_buf and sentence_size + len(sentence) + 1 > max_chars:
                flush_sentences(0)
            sentence_buf.append(sentence)
            sentence_size += len(sentence) + 1
        flush_sentences(paragraph_pause_ms)

    flush(paragraph_pause_ms)
    return chunks


def estimate_seconds(chars: int, rate_pct: int = 0) -> float:
    """Rough narration length; calibrated against real Edge/SAPI output."""
    speed = 1.0 + (rate_pct / 100.0)
    return (chars / 13.5) / max(0.5, speed)
