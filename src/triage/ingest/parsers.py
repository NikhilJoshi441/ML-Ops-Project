"""Document readers. Each returns a list of logical lines with light markup:

* ``## heading``   - a heading paragraph (HTML class="head", chapter titles)
* ``Table N``      - start of a table
* ``| a | b``      - a table row (HTML only)

PDFs must have a text layer; a page with no extractable text is reported so
the operator knows OCR is needed (brief, section 3).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

ROMAN = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii"]


def _clean(s: str) -> str:
    s = s.replace(" ", " ").replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip()


def read_html(path: Path, start_marker: str | None, end_marker: str | None) -> list[str]:
    from bs4 import BeautifulSoup, NavigableString, Tag

    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    lines: list[str] = []

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name
            if name == "ol":
                items = [li for li in child.find_all("li", recursive=False)]
                otype = (child.get("type") or "1").lower()
                for idx, li in enumerate(items):
                    marker = f"({ROMAN[idx]})" if otype == "i" else (f"({chr(97 + idx)})" if otype == "a" else f"({idx + 1})")
                    text = _clean(li.get_text(" "))
                    if text:
                        lines.append(f"{marker} {text}")
                continue
            if name == "table" and (child.find(["table", "ol", "div"]) is not None or len(child.find_all("p")) > 2):
                walk(child)  # layout table: recurse into it
                continue
            if name == "table":
                for tr in child.find_all("tr"):
                    cells = [_clean(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
                    cells = [c for c in cells if c]
                    if not cells:
                        continue
                    if len(cells) == 1 and re.match(r"^Table\s+\d+$", cells[0]):
                        lines.append(cells[0])
                    else:
                        lines.append("| " + " | ".join(cells))
                continue
            if name in ("p", "h1", "h2", "h3", "h4", "li"):
                text = _clean(child.get_text(" "))
                if text:
                    cls = " ".join(child.get("class") or [])
                    lines.append(("## " + text) if "head" in cls else text)
                continue
            walk(child)

    body = soup.body or soup
    walk(body)
    return _slice(lines, start_marker, end_marker)


def read_pdf(path: Path, start_marker: str | None, end_marker: str | None) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    lines: list[str] = []
    for pno, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if not text.strip():
            logger.warning("page has no text layer; OCR required", extra={"extra_fields": {"file": path.name, "page": pno}})
            continue
        for raw in text.splitlines():
            s = _clean(raw)
            if not s:
                continue
            if re.fullmatch(r"\d{1,3}", s) or re.match(r"^Page \d+ of \d+$", s):
                continue  # page numbers
            if re.match(r"^\d{1,2} (Inserted|Substituted|Deleted) by Notification", s):
                continue  # footnotes recording amendments
            lines.append(s)
    return _slice(lines, start_marker, end_marker)


def _slice(lines: list[str], start: str | None, end: str | None) -> list[str]:
    i0 = 0
    if start:
        for i, s in enumerate(lines):
            if start in s:
                i0 = i
                break
    i1 = len(lines)
    if end:
        for i in range(i0 + 1, len(lines)):
            if end in lines[i]:
                i1 = i
                break
    return lines[i0:i1]


def read_document(path: Path, fmt: str, start: str | None, end: str | None) -> list[str]:
    if fmt == "html":
        return read_html(path, start, end)
    if fmt == "pdf":
        return read_pdf(path, start, end)
    if fmt == "txt":
        return _slice([_clean(x) for x in path.read_text().splitlines() if x.strip()], start, end)
    raise ValueError(f"unsupported format {fmt}")
