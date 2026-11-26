"""Jarviz indexer: scan the markdown notes and write viewer/graph-data.js.

Python 3, standard library only.

    python build.py            index the folder named in config.json ("notes_dir")
    python build.py PATH       index another folder

Every node gets a numeric id equal to its index in GRAPH.nodes. The server, the
viewer and the brain all look notes up by that index.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
VIEWER_DIR = ROOT / "viewer"
GRAPH_PATH = VIEWER_DIR / "graph-data.js"
DEFAULT_NOTES_DIR = "notes"

EXCERPT_CHARS = 700
MIN_TITLE_CHARS = 4          # shorter titles match too much ordinary text
MAX_SHARED_LINK_NOTES = 10   # a [[wikilink]] used by more notes than this is too generic to link them
TITLE_WEIGHT = 3.0           # a keyword in a title counts this much more than one in the body
SKIP_DIRS = frozenset({"node_modules", "__pycache__"})

GRAPH_PREFIX = "const GRAPH = "
GRAPH_SUFFIX = ";\n"

WIKILINK_RE = re.compile(r"\[\[([^\[\]|#\n]+)(?:#[^\[\]|\n]*)?(?:\|[^\[\]\n]*)?\]\]")
FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n(?:.*?\r?\n)?---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
WORD_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)?")     # letters and digits of any script
STOPWORDS = frozenset(
    """a about above after again all also am an and any are as at be because been before being below
    between both but by can could did do does doing done down during each few for from further had
    has have having he her here hers him his how i if in into is it its just me more most my no nor
    not now of off on once only or other our ours out over own same she should so some such than
    that the their theirs them then there these they this those through to too under until up us
    very was we were what when where which while who whom whose why will with would you your yours
    please tell say said know""".split()
)


# ---------------------------------------------------------------- keywords

def normalize_word(word: str) -> str:
    """Fold simple plurals and possessives so "prices" finds "price"."""
    if word.endswith("'s"):
        word = word[:-2]
    word = word.replace("'", "")
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def fold(text: str) -> str:
    """Lowercase and drop accents, so "cafe" finds "café"."""
    decomposed = unicodedata.normalize("NFKD", text.replace("’", "'").casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def tokenize(text: str) -> list[str]:
    """The keywords of a text: lowercase, no accents, no stopwords, plurals folded."""
    tokens = []
    for raw in WORD_RE.findall(fold(text)):
        if raw in STOPWORDS:
            continue
        word = normalize_word(raw)
        if len(word) > 1 and word not in STOPWORDS:
            tokens.append(word)
    return tokens


# ------------------------------------------------------------------- notes

@dataclass
class Note:
    label: str                      # the file name without .md
    group: str                      # the parent folder
    path: str                       # relative to the notes folder, forward slashes
    text: str
    excerpt: str = ""
    wikilinks: frozenset = frozenset()
    title_tokens: frozenset = frozenset()
    body_tokens: Counter = field(default_factory=Counter)

    @cached_property
    def folded_text(self) -> str:
        return self.text.casefold()

    @cached_property
    def folded_title(self) -> str:
        return self.label.casefold()

    @cached_property
    def title_pattern(self) -> re.Pattern:
        return re.compile(r"(?<!\w)" + re.escape(self.folded_title) + r"(?!\w)")


def make_excerpt(text: str, label: str, limit: int = EXCERPT_CHARS) -> str:
    """About `limit` characters of the note, without a heading that only repeats the title."""
    lines = text.splitlines()
    if lines and lines[0].startswith("#") and lines[0].lstrip("#").strip().casefold() == label.casefold():
        lines = lines[1:]
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines).strip())
    if len(body) <= limit:
        return body
    cut = max(body.rfind(" ", 0, limit), body.rfind("\n", 0, limit))
    if cut < limit * 0.6:
        cut = limit
    return body[:cut].rstrip() + " …"


def make_note(file_path: Path, notes_dir: Path) -> Note:
    raw = file_path.read_text(encoding="utf-8-sig", errors="replace")
    text = FRONT_MATTER_RE.sub("", raw, count=1).strip()
    relative = file_path.relative_to(notes_dir)
    label = file_path.stem
    return Note(
        label=label,
        group=relative.parent.name or notes_dir.name,
        path=relative.as_posix(),
        text=text,
        excerpt=make_excerpt(text, label),
        wikilinks=frozenset(target.strip().casefold() for target in WIKILINK_RE.findall(text)),
        title_tokens=frozenset(tokenize(label)),
        body_tokens=Counter(tokenize(text)),
    )


def note_files(notes_dir: Path) -> list[Path]:
    """Every .md file under the folder, in a stable order. Hidden folders are skipped."""
    found: list[Path] = []
    for folder, dirs, files in os.walk(notes_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
        found.extend(
            Path(folder) / name
            for name in files
            if name.lower().endswith(".md") and not name.startswith(".")
        )
    return sorted(found, key=lambda path: path.relative_to(notes_dir).as_posix().casefold())


def scan_notes(notes_dir: Path) -> list[Note]:
    notes = []
    for path in note_files(notes_dir):
        try:
            notes.append(make_note(path, notes_dir))
        except OSError as error:
            print(f"Skipped {path.name}: {error.strerror or error}", file=sys.stderr)
    return notes


# ------------------------------------------------------------------- links

def mentions(reader: Note, other: Note) -> bool:
    """True when the reader's text names the other note's title as whole words."""
    title = other.folded_title
    if len(title) < MIN_TITLE_CHARS or title not in reader.folded_text:
        return False
    return other.title_pattern.search(reader.folded_text) is not None


def unshareable_wikilinks(notes: list[Note]) -> set[str]:
    """Wikilinks that do not link the notes that share them.

    A target that is itself a note already links each of them to that note, and a target
    used by a crowd of notes says too little. Only shared "concept" links join two notes.
    """
    counts = Counter(link for note in notes for link in note.wikilinks)
    crowded = {link for link, uses in counts.items() if uses > MAX_SHARED_LINK_NOTES}
    return crowded | {note.folded_title for note in notes}


def linked(a: Note, b: Note, unshareable: set[str]) -> bool:
    if b.folded_title in a.wikilinks or a.folded_title in b.wikilinks:
        return True         # a [[link]] written on purpose always counts, even to a short title like REA
    if mentions(a, b) or mentions(b, a):
        return True
    return any(link not in unshareable for link in a.wikilinks & b.wikilinks)


def compute_links(notes: list[Note]) -> list[tuple[int, int]]:
    """Link two notes when one mentions the other's title or they share a [[wikilink]]."""
    unshareable = unshareable_wikilinks(notes)
    return [
        (i, j)
        for i in range(len(notes))
        for j in range(i + 1, len(notes))
        if linked(notes[i], notes[j], unshareable)
    ]


def links_for_new(note: Note, notes: list[Note]) -> list[tuple[int, int]]:
    """Links for a note that is about to get index len(notes), by the same rules."""
    unshareable = unshareable_wikilinks([*notes, note])
    new_index = len(notes)
    return [(i, new_index) for i, other in enumerate(notes) if linked(other, note, unshareable)]


def flipped_wikilinks(note: Note, others: list[Note]) -> set[str]:
    """The wikilinks whose power to join two notes changes when `note` joins or leaves `others`.

    `others` is the list WITHOUT `note`. A link that exactly MAX_SHARED_LINK_NOTES of them use gets crowded
    when the note comes, and stops being crowded when it goes. The note's own title is a wikilink target too:
    while the note is there, a shared [[link]] to it does not join the notes that share it. A title that more
    than MAX_SHARED_LINK_NOTES of them use is crowded with the note and without it, so it changes no link.
    """
    title = note.folded_title
    counts = Counter(link for other in others for link in other.wikilinks if link in note.wikilinks or link == title)
    flipped = {link for link in note.wikilinks if counts[link] == MAX_SHARED_LINK_NOTES}
    if 0 < counts[title] <= MAX_SHARED_LINK_NOTES and not any(other.folded_title == title for other in others):
        flipped.add(title)
    return flipped


def relink(notes: list[Note], links: set[tuple[int, int]], flipped: set[str]) -> tuple[set, set]:
    """What to add to `links` and what to take away, so the links among the notes that use a flipped wikilink
    are the ones compute_links() would give. Both are sets of (i, j) pairs with i < j. Other pairs do not change."""
    if not flipped:
        return set(), set()
    pairs: set[tuple[int, int]] = set()
    for link in flipped:
        users = [index for index, note in enumerate(notes) if link in note.wikilinks]
        pairs.update((a, b) for position, a in enumerate(users) for b in users[position + 1:])
    unshareable = unshareable_wikilinks(notes)
    to_add, to_remove = set(), set()
    for i, j in pairs:
        if linked(notes[i], notes[j], unshareable):
            if (i, j) not in links:
                to_add.add((i, j))
        elif (i, j) in links:
            to_remove.add((i, j))
    return to_add, to_remove


def keyword_score(query_tokens, note: Note) -> float:
    """Keyword overlap between a question and a note. Title matches weigh more."""
    score = 0.0
    for token in query_tokens:
        if token in note.title_tokens:
            score += TITLE_WEIGHT
        count = note.body_tokens.get(token, 0)
        if count:
            score += 1.0 + 0.1 * min(count, 5)
    return score


# -------------------------------------------------------------- graph file

def new_build_id() -> str:
    return format(time.time_ns() // 1_000_000, "x")


def graph_payload(notes: list[Note], links, build_id: str, rev: int) -> dict:
    return {
        "build_id": build_id,
        "rev": rev,
        "nodes": [
            {"id": index, "label": note.label, "group": note.group, "excerpt": note.excerpt}
            for index, note in enumerate(notes)
        ],
        "links": [{"source": a, "target": b} for a, b in sorted(links)],
    }


def write_graph(payload: dict, path: Path | None = None) -> None:
    """Write graph-data.js in one step, so the browser never reads half a file."""
    path = path or GRAPH_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    text = GRAPH_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + GRAPH_SUFFIX
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")    # own name: build.py and the server may both write
    try:
        temp.write_text(text, encoding="utf-8", newline="\n")
        for attempt in range(5):
            try:
                os.replace(temp, path)
                return
            except PermissionError:  # Windows: the file is open for a moment while it is served
                if attempt == 4:
                    raise
                time.sleep(0.05)
    finally:
        temp.unlink(missing_ok=True)


def parse_graph_text(text: str) -> dict:
    return json.loads(text[text.index("{"): text.rindex("}") + 1])


def read_graph(path: Path | None = None) -> dict:
    return parse_graph_text((path or GRAPH_PATH).read_text(encoding="utf-8"))


def build(notes_dir: Path, graph_path: Path | None = None) -> tuple[list[Note], list[tuple[int, int]], str]:
    notes = scan_notes(notes_dir) if notes_dir.is_dir() else []
    links = compute_links(notes)
    build_id = new_build_id()
    write_graph(graph_payload(notes, links, build_id, 0), graph_path)
    return notes, links, build_id


# ------------------------------------------------------------------- paths

def resolve_notes_dir(value: str) -> Path:
    path = Path(str(value)).expanduser()
    return (path if path.is_absolute() else ROOT / path).resolve()


def configured_notes_dir(config_path: Path = CONFIG_PATH) -> Path:
    name = DEFAULT_NOTES_DIR
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if isinstance(loaded, dict) and isinstance(loaded.get("notes_dir"), str) and loaded["notes_dir"].strip():
            name = loaded["notes_dir"].strip()
    except (OSError, ValueError):
        pass
    return resolve_notes_dir(name)


def main(argv: list[str]) -> int:
    notes_dir = resolve_notes_dir(argv[1]) if len(argv) > 1 else configured_notes_dir()
    if not notes_dir.is_dir():
        print(f"Notes folder not found: {notes_dir}", file=sys.stderr)
        return 1
    notes, links, _ = build(notes_dir)
    folders = len({note.group for note in notes})
    print(f"Indexed {len(notes)} notes in {folders} folders, {len(links)} links -> viewer/graph-data.js")
    return 0 if notes else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
