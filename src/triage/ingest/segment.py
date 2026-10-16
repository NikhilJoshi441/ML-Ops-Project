"""Clause-aware segmentation (L1).

Regulatory text is a hierarchy: paragraph -> lettered item -> roman item, plus
tables. A fixed-size chunker cuts the liability table away from the sentence
that says how its "working days" are counted, and cuts "(ii)" away from the
lead-in sentence that says whether (ii) is a zero-liability or a
limited-liability case. This segmenter

1. rebuilds the clause tree from numbering markers,
2. emits one chunk per clause at the document's configured depth (deeper items
   are folded into their parent so a chunk is a citable unit),
3. prefixes every chunk with its heading path and the parent's lead-in
   sentence (contextual chunk text, used for embedding),
4. keeps tables as single chunks, and attaches a sentence that follows a table
   and names it (e.g. the working-day counting rule after Table 2) to the
   table chunk.

`naive_chunks` is the fixed-window baseline used for the before/after
comparison in the evaluation (L3).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

ROMANS = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii"]
MAX_CHUNK_CHARS = 1600

RE_L1 = re.compile(r"^(\d{1,2})\.\s+(.+)$")
RE_PAREN_NUM = re.compile(r"^\((\d{1,2})\)\s*(.*)$")
RE_PAREN_ALPHA = re.compile(r"^\(([a-z]{1,4})\)\d*\s*(.*)$")
RE_LETTER_BRACKET = re.compile(r"^([a-h])\)\s+(.+)$")
RE_TABLE = re.compile(r"^Table\s+(\d+)\b\s*(.*)$")
RE_CHAPTER = re.compile(r"^CHAPTER\s+[IVX]+\b")


@dataclass
class Node:
    path: tuple[str, ...]
    heading_path: list[str]
    lines: list[str] = field(default_factory=list)
    kind: str = "clause"  # clause | table | preamble

    @property
    def text(self) -> str:
        return " ".join(self.lines).strip()


@dataclass
class Chunk:
    chunk_id: str
    clause_id: str
    doc_id: str
    label: str
    heading: str
    text: str            # verbatim clause text (what citations quote)
    context_text: str    # heading path + lead-in + text (what is embedded)
    kind: str
    part: int = 1
    order: int = 0


def _is_heading(line: str) -> bool:
    if line.startswith("## "):
        return True
    if RE_CHAPTER.match(line):
        return True
    letters = [c for c in line if c.isalpha()]
    return bool(letters) and len(line) <= 90 and sum(c.isupper() for c in letters) / len(letters) > 0.85 and len(letters) > 6


def _clause_path_str(path: tuple[str, ...], style: str) -> str:
    if not path:
        return "preamble"
    if path[0].startswith("Table-") or path[0] == "preamble":
        return path[0]
    if style == "dotted":  # FED-CPP: 4.b(iii)
        out = path[0]
        for p in path[1:]:
            if p in ROMANS and len(path) > 2 and p == path[-1] and len(path) == 3:
                out += f"({p})"
            elif re.fullmatch(r"[a-h]", p):
                out += f".{p}"
            else:
                out += f"({p})"
        return out
    out = path[0]
    for p in path[1:]:
        out += f"({p})"
    return out


class Segmenter:
    def __init__(self, doc_id: str, chunk_depth: int, label_prefix: str, short: str, style: str):
        self.doc_id = doc_id
        self.depth = chunk_depth
        self.label_prefix = label_prefix
        self.short = short
        self.style = style

    def build_nodes(self, lines: list[str]) -> list[Node]:
        nodes: list[Node] = []
        headings: list[str] = []
        pending_heading: list[str] = []
        stack: list[str] = []          # current structural path
        cur_l1 = 0
        current: Node | None = None
        structural: Node | None = None
        table: Node | None = None
        last_roman: str | None = None
        last_alpha: str | None = None

        def open_node(path: tuple[str, ...], kind: str = "clause") -> Node:
            nonlocal pending_heading
            inherited = headings
            if len(path) > 1 and kind == "clause":
                for anc in reversed(nodes):
                    if anc.path == path[: len(anc.path)] and len(anc.path) < len(path):
                        inherited = anc.heading_path
                        break
            n = Node(path=path, heading_path=inherited + pending_heading, kind=kind)
            pending_heading = []
            nodes.append(n)
            return n

        for raw in lines:
            line = raw.strip()
            if not line:
                continue

            # ---- tables -------------------------------------------------
            mt = RE_TABLE.match(line)
            if mt and (not mt.group(2) or len(mt.group(2)) < 120) and not line.lower().startswith("table 1, whichever"):
                if mt.group(2) and re.match(r"^(of|whichever|above|of the|for ease)", mt.group(2)):
                    pass  # a sentence that merely mentions a table
                else:
                    table = open_node((f"Table-{mt.group(1)}",), kind="table")
                    if mt.group(2):
                        table.lines.append(mt.group(2))
                    current = table
                    continue
            if table is not None:
                if line.startswith("| "):
                    table.lines.append(line[2:])
                    continue
                is_marker = bool(
                    RE_L1.match(line) or RE_PAREN_NUM.match(line) or RE_PAREN_ALPHA.match(line)
                    or RE_LETTER_BRACKET.match(line) or _is_heading(line)
                )
                if not is_marker:
                    tname = f"Table {table.path[0].split('-')[1]}"
                    if tname in line or not structural or self.style == "dotted" and not line.startswith("Further"):
                        # PDF tables have no row markup: every non-marker line belongs to the
                        # table; for HTML only a trailing sentence that names the table does.
                        if self.style == "dotted" or tname in line:
                            table.lines.append(line)
                            continue
                table = None
                current = structural

            # ---- headings -----------------------------------------------
            if _is_heading(line):
                h = line[3:] if line.startswith("## ") else line
                if RE_CHAPTER.match(h):
                    headings = [h]
                else:
                    pending_heading.append(h)
                continue

            # ---- level 1: "7. ..." ------------------------------------
            m1 = RE_L1.match(line)
            if m1 and cur_l1 < int(m1.group(1)) <= cur_l1 + 2:
                cur_l1 = int(m1.group(1))
                stack = [m1.group(1)]
                last_roman = last_alpha = None
                rest = m1.group(2)
                if pending_heading:
                    headings = [h for h in headings if RE_CHAPTER.match(h)] + pending_heading
                    pending_heading = []
                # a short title-like remainder is the clause heading (PDF style)
                if len(rest) < 90 and not rest.endswith((".", ":", ";")):
                    structural = open_node(tuple(stack))
                    structural.heading_path = headings + [rest]
                else:
                    structural = open_node(tuple(stack))
                    structural.lines.append(rest)
                current = structural
                continue

            # ---- "(1)" numbered sub-clause ---------------------------------
            mn = RE_PAREN_NUM.match(line)
            if mn and stack:
                stack = [stack[0], mn.group(1)]
                last_roman = last_alpha = None
                structural = open_node(tuple(stack))
                structural.lines.append(line)
                current = structural
                continue

            # ---- "a)" lettered (FED style) ---------------------------------
            ml = RE_LETTER_BRACKET.match(line)
            if ml and stack:
                stack = [stack[0], ml.group(1)]
                last_roman, last_alpha = None, ml.group(1)
                structural = open_node(tuple(stack))
                structural.heading_path = structural.heading_path + [ml.group(2)]
                current = structural
                continue

            # ---- "(a)" / "(ii)" ------------------------------------------
            ma = RE_PAREN_ALPHA.match(line)
            if ma and stack:
                tok = ma.group(1)
                is_roman = tok in ROMANS and (
                    (last_roman is not None and ROMANS.index(tok) == ROMANS.index(last_roman) + 1)
                    or (tok == "i" and last_alpha != "h")
                )
                if is_roman:
                    last_roman = tok
                    base = [s for s in stack if s not in ROMANS or s in ("i",) and False]
                    base = stack[:-1] if stack and stack[-1] in ROMANS and len(stack) > 1 else stack
                    stack = base + [tok]
                else:
                    last_alpha, last_roman = tok, None
                    base = stack
                    while len(base) > 1 and (base[-1] in ROMANS or re.fullmatch(r"[a-z]{1,2}", base[-1])):
                        base = base[:-1]
                    stack = base + [tok]
                if len(stack) > self.depth:
                    # deeper than the chunk depth: fold into parent node text
                    if current is not None:
                        current.lines.append(line)
                    continue
                structural = open_node(tuple(stack))
                structural.lines.append(line)
                current = structural
                continue

            # ---- continuation ----------------------------------------------
            if current is None:
                current = open_node(("preamble",), kind="preamble")
            current.lines.append(line)
        return [n for n in nodes if n.text or n.kind == "table"]

    def chunks(self, lines: list[str]) -> list[Chunk]:
        nodes = self.build_nodes(lines)
        by_path = {n.path: n for n in nodes}
        out: list[Chunk] = []
        order = 0
        for n in nodes:
            if not n.text:
                continue
            cpath = _clause_path_str(n.path, self.style)
            clause_id = f"{self.doc_id}:{cpath}"
            parent = by_path.get(n.path[:-1]) if len(n.path) > 1 else None
            lead_in = parent.text if parent is not None and parent.text and len(parent.text) < 600 else ""
            heading = " > ".join(n.heading_path[-3:])
            if n.kind == "table":
                label = f"{self.short}, {n.path[0].replace('-', ' ')}"
            elif n.kind == "preamble":
                label = f"{self.short}, preamble"
            else:
                label = f"{self.short}, {self.label_prefix}{cpath}"
            parts = _split(n.text)
            for k, part in enumerate(parts, start=1):
                ctx = f"[{self.short}] [{heading}] [{label}] " + (f"(Lead-in: {lead_in}) " if lead_in else "") + part
                out.append(
                    Chunk(
                        chunk_id=clause_id if len(parts) == 1 else f"{clause_id}~p{k}",
                        clause_id=clause_id,
                        doc_id=self.doc_id,
                        label=label,
                        heading=heading,
                        text=part,
                        context_text=ctx,
                        kind=n.kind,
                        part=k,
                        order=order,
                    )
                )
                order += 1
        # de-duplicate ids (a path can legitimately reappear, e.g. two "preamble" runs)
        seen: dict[str, int] = {}
        for c in out:
            if c.chunk_id in seen:
                seen[c.chunk_id] += 1
                c.chunk_id = f"{c.chunk_id}~d{seen[c.chunk_id]}"
            else:
                seen[c.chunk_id] = 1
        return out


def _split(text: str) -> list[str]:
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    sents = re.split(r"(?<=[.;:])\s+", text)
    parts, cur = [], ""
    for s in sents:
        if cur and len(cur) + len(s) > MAX_CHUNK_CHARS:
            parts.append(cur.strip())
            cur = ""
        cur += " " + s
    if cur.strip():
        parts.append(cur.strip())
    return parts


def naive_chunks(doc_id: str, short: str, lines: list[str], size: int = 800, overlap: int = 100) -> list[Chunk]:
    """Fixed-size character windows with no structure (baseline)."""
    text = " ".join(l[3:] if l.startswith("## ") else l.lstrip("| ") for l in lines)
    out: list[Chunk] = []
    i, k = 0, 0
    while i < len(text):
        piece = text[i : i + size]
        cid = f"{doc_id}:naive-{k}"
        out.append(Chunk(cid, cid, doc_id, f"{short}, characters {i}-{i + len(piece)}", "", piece, piece, "window", 1, k))
        k += 1
        i += size - overlap
    return out
