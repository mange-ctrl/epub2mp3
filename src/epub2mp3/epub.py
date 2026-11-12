"""EPUB reader: metadata, spine order, TOC titles and plain-text extraction.

Deliberately dependency free (stdlib only) so the packaged exe stays small and
robust across the many quirky EPUBs found in the wild.
"""

from __future__ import annotations

import os
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import unquote
from xml.etree import ElementTree as ET

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
XHTML_NS = "http://www.w3.org/1999/xhtml"

_TOC_NAMES = {"contents", "table of contents", "inhall", "innehåll",
              "innehållsförteckning", "inhalt", "indice", "índice"}

_CHAPTER_LINE = re.compile(
    r"^(?:chapter|part|book|volume|prologue|epilogue|introduction|preface|"
    r"foreword|afterword)\b.{0,70}$",
    re.IGNORECASE,
)


def _normalise_for_match(value: str) -> str:
    value = re.sub(r"[^\w\s]", " ", value.lower())
    return " ".join(value.split())


def _looks_like_front_matter(text: str) -> bool:
    lowered = text.lower()
    if "project gutenberg" in lowered and (
        "contents" in lowered or "release date" in lowered or "language: english" in lowered
    ):
        return len(text) < 8000
    return False


def _looks_like_table_of_contents(text: str) -> bool:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paragraphs) < 8:
        return False
    short = sum(
        1 for paragraph in paragraphs
        if len(paragraph) < 70 and _CHAPTER_LINE.match(paragraph)
    )
    return short / len(paragraphs) >= 0.5 and len(text) < 5000


def _strip_leading_heading(text: str, title: str) -> str:
    """Drop a heading that merely repeats the chapter title read by the TOC."""
    if not title:
        return text
    wanted = _normalise_for_match(title)
    if not wanted:
        return text
    paragraphs = text.split("\n\n")
    for count in (1, 2):
        if len(paragraphs) < count:
            continue
        head = _normalise_for_match(" ".join(paragraphs[:count]))
        if not head:
            continue
        if head == wanted or wanted.endswith(head):
            return "\n\n".join(paragraphs[count:]).strip()
    return text


class EpubError(Exception):
    """Raised when an EPUB cannot be opened or contains no readable text."""


@dataclass
class Chapter:
    title: str
    text: str
    href: str = ""


@dataclass
class Book:
    title: str
    author: str
    language: str
    chapters: list[Chapter]
    cover: tuple[bytes, str] | None = None
    date: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return sum(len(c.text.split()) for c in self.chapters)

    @property
    def char_count(self) -> int:
        return sum(len(c.text) for c in self.chapters)


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #

class _Extractor(HTMLParser):
    """Turn XHTML into plain text, keeping paragraph breaks and headings."""

    BLOCK_TAGS = {
        "p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "blockquote", "tr", "section", "article", "pre", "table", "ul", "ol",
        "dd", "dt", "figcaption", "hr", "center", "main", "header", "footer",
        "aside", "figure", "td", "th", "address", "dl", "fieldset",
    }
    # Elements whose content is never part of the narration.
    SKIP_TAGS = {"script", "style", "head", "title", "svg", "math", "template",
                 "noscript", "nav"}
    SKIP_CLASS_HINTS = ("pagenum", "page-number", "pagebreak", "page-break",
                        "pagelist", "footnote-ref", "noteref")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.headings: list[str] = []
        self._skip_depth = 0
        self._heading_level = 0
        self._heading_buf: list[str] = []

    # -- helpers ---------------------------------------------------------- #
    @staticmethod
    def _attrs(attrs) -> dict[str, str]:
        return {k.lower(): (v or "") for k, v in attrs}

    def _should_skip(self, tag: str, attrs) -> bool:
        if tag in self.SKIP_TAGS:
            return True
        a = self._attrs(attrs)
        if a.get("role", "").lower() in ("doc-pagebreak", "presentation"):
            return a.get("role", "") == "doc-pagebreak"
        if a.get("aria-hidden", "").lower() == "true":
            return True
        style = a.get("style", "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            return True
        classes = a.get("class", "").lower()
        if classes:
            for hint in self.SKIP_CLASS_HINTS:
                if hint in classes:
                    return True
        return False

    # -- parser callbacks -------------------------------------------------- #
    def handle_starttag(self, tag, attrs):
        if self._skip_depth:
            self._skip_depth += 1
            return
        if self._should_skip(tag, attrs):
            self._skip_depth = 1
            return
        if tag in self.BLOCK_TAGS:
            if tag == "br":
                self.parts.append("\n")
            else:
                self.parts.append("\n\n")
        if tag in ("h1", "h2", "h3", "h4"):
            self._heading_level = int(tag[1])
            self._heading_buf = []

    def handle_startendtag(self, tag, attrs):
        if self._skip_depth:
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag == "hr":
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in self.BLOCK_TAGS and tag != "br":
            self.parts.append("\n\n")
        if tag in ("h1", "h2", "h3", "h4") and self._heading_level:
            text = " ".join("".join(self._heading_buf).split())
            if text:
                self.headings.append(text)
            self._heading_level = 0
            self._heading_buf = []

    def handle_data(self, data):
        if self._skip_depth or not data:
            return
        if self._heading_level:
            self._heading_buf.append(data)
        self.parts.append(data)


def extract_text(raw_html: str) -> tuple[str, list[str]]:
    """Return (text_with_breaks, heading_texts) for an XHTML document."""
    parser = _Extractor()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:
        pass
    return "".join(parser.parts), parser.headings


# --------------------------------------------------------------------------- #
# Navigation (EPUB3 nav / EPUB2 NCX)
# --------------------------------------------------------------------------- #

class _NavParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._toc_depth = 0
        self._nav_depth = 0
        self._href: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "nav":
            self._nav_depth += 1
            kind = (a.get("epub:type") or a.get("type") or "").lower()
            if "toc" in kind.split() and not self._toc_depth:
                self._toc_depth = 1
            elif self._toc_depth:
                self._toc_depth += 1
        elif tag == "a":
            self._href = a.get("href") or None
            self._buf = []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._buf).split())
            if text:
                self.links.append((self._href, text))
            self._href = None
        elif tag == "nav" and self._nav_depth:
            self._nav_depth -= 1
            if self._toc_depth:
                self._toc_depth -= 1


def _parse_nav(raw: str) -> list[tuple[str, str]]:
    parser = _NavParser()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        pass
    if parser.links:
        return parser.links
    # No anchors found: fall back to every <a href> in the document.
    return []


def _parse_ncx(raw: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return out
    for nav_point in root.iter():
        if not nav_point.tag.endswith("navPoint"):
            continue
        label = ""
        src = ""
        for child in nav_point:
            if child.tag.endswith("navLabel"):
                label = " ".join((child.itertext() if hasattr(child, "itertext") else [])).strip()
                label = " ".join(label.split())
            elif child.tag.endswith("content"):
                src = child.get("src", "")
        if src:
            out.setdefault(src, label)
    return out


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def _resolve(base_dir: str, href: str) -> str:
    href = unquote(href.split("#")[0]).strip()
    if not href:
        return ""
    return posixpath.normpath(posixpath.join(base_dir, href))


def _zip_read(zf: zipfile.ZipFile, names: dict[str, str], path: str) -> bytes:
    actual = names.get(path.lower())
    if actual is None:
        raise KeyError(path)
    return zf.read(actual)


def _find_opf(zf: zipfile.ZipFile, names: dict[str, str]) -> str:
    try:
        container = _zip_read(zf, names, "META-INF/container.xml")
        root = ET.fromstring(container)
        for elem in root.iter():
            full = elem.get("full-path")
            if full:
                return full
    except (KeyError, ET.ParseError):
        pass
    for lower, actual in names.items():
        if lower.endswith(".opf"):
            return actual
    raise EpubError("Not a valid EPUB: no package document (.opf) found.")


def _first_meta(metadata, local: str) -> str:
    for elem in metadata.iter():
        tag = elem.tag
        name = tag.rsplit("}", 1)[-1] if "}" in tag else tag
        if name == local:
            text = " ".join((elem.text or "").split())
            if text:
                return text
    return ""


def _all_meta(metadata, local: str) -> list[str]:
    values = []
    for elem in metadata.iter():
        tag = elem.tag
        name = tag.rsplit("}", 1)[-1] if "}" in tag else tag
        if name == local:
            text = " ".join((elem.text or "").split())
            if text:
                values.append(text)
    return values


def load_book(path: str | os.PathLike) -> Book:
    """Read an EPUB file and return its metadata plus ordered chapters."""
    path = os.fspath(path)
    if not os.path.isfile(path):
        raise EpubError(f"File not found:\n{path}")
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise EpubError("This file is not a valid EPUB (it is not a readable ZIP archive).") from exc

    with zf:
        names = {n.lower(): n for n in zf.namelist()}
        warnings: list[str] = []
        if "meta-inf/encryption.xml" in names:
            warnings.append("This EPUB contains encrypted (DRM) content; some text may be unreadable.")

        opf_path = _find_opf(zf, names)
        opf_dir = posixpath.dirname(opf_path)
        try:
            opf_raw = _zip_read(zf, names, opf_path)
        except KeyError as exc:
            raise EpubError("Not a valid EPUB: the package document is missing from the archive.") from exc

        try:
            opf = ET.fromstring(opf_raw)
        except ET.ParseError as exc:
            raise EpubError("Not a valid EPUB: the package document is not well-formed XML.") from exc

        metadata = None
        manifest = None
        spine = None
        for elem in opf.iter():
            name = elem.tag.rsplit("}", 1)[-1] if "}" in elem.tag else elem.tag
            if name == "metadata" and metadata is None:
                metadata = elem
            elif name == "manifest" and manifest is None:
                manifest = elem
            elif name == "spine" and spine is None:
                spine = elem

        title = ""
        author = ""
        language = ""
        date = ""
        if metadata is not None:
            title = _first_meta(metadata, "title")
            authors = _all_meta(metadata, "creator")
            author = ", ".join(dict.fromkeys(authors))
            language = _first_meta(metadata, "language")
            date = _first_meta(metadata, "date")
            if not author:
                author = _first_meta(metadata, "publisher")

        # ---- manifest ---------------------------------------------------- #
        items: dict[str, dict] = {}
        if manifest is not None:
            for item in manifest:
                name = item.tag.rsplit("}", 1)[-1] if "}" in item.tag else item.tag
                if name != "item":
                    continue
                item_id = item.get("id") or ""
                href = item.get("href") or ""
                if not item_id or not href:
                    continue
                items[item_id] = {
                    "href": href,
                    "path": _resolve(opf_dir, href),
                    "media_type": (item.get("media-type") or "").lower(),
                    "properties": (item.get("properties") or "").lower(),
                }

        # ---- spine ------------------------------------------------------- #
        spine_ids: list[str] = []
        if spine is not None:
            for itemref in spine:
                name = itemref.tag.rsplit("}", 1)[-1] if "}" in itemref.tag else itemref.tag
                if name == "itemref":
                    idref = itemref.get("idref")
                    if idref:
                        spine_ids.append(idref)

        # ---- cover ------------------------------------------------------- #
        cover: tuple[bytes, str] | None = None
        cover_item = None
        if metadata is not None:
            for elem in metadata.iter():
                if (elem.tag.rsplit("}", 1)[-1] if "}" in elem.tag else elem.tag) == "meta":
                    if (elem.get("name") or "").lower() == "cover":
                        cover_item = items.get(elem.get("content") or "")
        if cover_item is None:
            for item in items.values():
                if "cover-image" in item["properties"]:
                    cover_item = item
                    break
        if cover_item is None:
            for item in items.values():
                if item["media_type"].startswith("image/") and "cover" in item["href"].lower():
                    cover_item = item
                    break
        if cover_item is not None:
            try:
                data = _zip_read(zf, names, cover_item["path"])
                mime = cover_item["media_type"] or "image/jpeg"
                if mime == "image/jpg":
                    mime = "image/jpeg"
                if len(data) < 2_000_000:
                    cover = (data, mime)
            except KeyError:
                pass

        # ---- table of contents ------------------------------------------- #
        toc: dict[str, str] = {}
        nav_item = None
        for item in items.values():
            if "nav" in item["properties"].split():
                nav_item = item
                break
        if nav_item is not None:
            try:
                raw_nav = _zip_read(zf, names, nav_item["path"]).decode("utf-8", "replace")
                for href, text in _parse_nav(raw_nav):
                    target = _resolve(posixpath.dirname(nav_item["path"]), href)
                    if target:
                        toc.setdefault(target, text)
            except KeyError:
                pass

        ncx_item = None
        for item in items.values():
            if item["media_type"] == "application/x-dtbncx+xml":
                ncx_item = item
                break
        if ncx_item is None:
            for item in items.values():
                if item["path"].lower().endswith(".ncx"):
                    ncx_item = item
                    break
        if ncx_item is not None:
            try:
                ncx = _parse_ncx(_zip_read(zf, names, ncx_item["path"]))
                ncx_dir = posixpath.dirname(ncx_item["path"])
                for src, label in ncx.items():
                    target = _resolve(ncx_dir, src)
                    if target and target not in toc:
                        toc[target] = label
            except KeyError:
                pass

        # ---- chapters ---------------------------------------------------- #
        chapters: list[Chapter] = []
        for idref in spine_ids:
            item = items.get(idref)
            if item is None:
                continue
            if item["media_type"] not in ("application/xhtml+xml", "text/html", ""):
                continue
            try:
                raw = _zip_read(zf, names, item["path"])
            except KeyError:
                warnings.append(f"Missing spine file: {item['path']}")
                continue
            html = raw.decode("utf-8", "replace")
            raw_text, headings = extract_text(html)
            cleaned = clean_text(raw_text)
            if not cleaned.strip():
                continue
            chapter_title = (toc.get(item["path"]) or "").strip()
            if not chapter_title and headings:
                chapter_title = headings[0]
            if not chapter_title:
                chapter_title = f"Chapter {len(chapters) + 1}"

            if _looks_like_front_matter(cleaned) or _looks_like_table_of_contents(cleaned):
                continue

            cleaned = _strip_leading_heading(cleaned, chapter_title)
            if len(cleaned.strip()) < 40:
                continue

            chapters.append(Chapter(title=chapter_title, text=cleaned, href=item["path"]))

    # Merge very short sections (title pages, copyright blurbs) into a neighbour
    merged: list[Chapter] = []
    for chapter in chapters:
        if len(chapter.text) < 350 and merged:
            merged[-1].text = merged[-1].text.rstrip() + "\n\n" + chapter.text.strip()
        elif len(chapter.text) < 60:
            continue
        else:
            merged.append(chapter)
    chapters = merged

    # Drop a leftover table-of-contents page (it duplicates the nav and is
    # nothing but a list of chapter names to listen to).
    chapters = [
        ch for ch in chapters
        if not (ch.title.strip().lower() in _TOC_NAMES and len(ch.text) < 3000)
    ]

    if not chapters:
        raise EpubError(
            "No readable text was found in this EPUB.\n"
            "It may be image-only, DRM protected, or damaged."
        )

    if not title:
        title = os.path.splitext(os.path.basename(path))[0]

    return Book(
        title=title,
        author=author,
        language=language,
        chapters=chapters,
        cover=cover,
        date=date,
        warnings=warnings,
    )


# Imported late to avoid a circular import at module load time.
from .textproc import clean_text  # noqa: E402
