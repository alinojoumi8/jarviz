"""Jarviz preflight: run every LIVE chain end to end and print a verdict.

    python preflight.py        (the server must be running: python server.py)

Real calls against the running system. No mocks. The exit code is 0 only when nothing failed.
"Done" means this passed. Add a check here every time something breaks.
"""
from __future__ import annotations

import email.message
import hashlib
import http.client
import json
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import brains
import build
import server
import teardown

PASS, FAIL, WARN = "pass", "fail", "warn"
CODE_WORD = "marmalade"

# Every way a browser might ask for the key file. None may work.
CONFIG_PROBES = (
    "/config.json", "/../config.json", "/..%2fconfig.json", "/%2e%2e/config.json", "/%2e%2e%2fconfig.json",
    "/..%5cconfig.json", "/..\\config.json", "/viewer/../config.json", "/viewer/../../config.json",
    "//config.json", "/./../config.json", "/index.html/../../config.json", "/%252e%252e/config.json",
    "/config.json.", "/config.json%20", "/CONFIG.JSON", "/config.json::$DATA", "/config.json%00.js",
    "/?file=../config.json", "/graph-data.js/../../config.json", "/..%3fconfig.json",
    "/%EF%BC%8E%EF%BC%8E/config.json", "/%c0%ae%c0%ae/config.json", "/CONFIG~1.JSO", "/SERVER~1.PY",
    "/server.py", "/../server.py", "/build.py", "/preflight.py", "/.gitignore", "/../.gitignore",
    # Windows tricks on real viewer files, drive letters, shares, device names, leftovers
    "/core.js::$DATA", "/core.js%3a%3a$DATA", "/core.js%3astream", "/index.html.", "/core.js%20",
    "/C:/Windows/win.ini", "/c%3a/Windows/win.ini", "/%5c%5clocalhost%5cc$%5cWindows%5cwin.ini",
    "/con.js", "/nul.html", "/aux.png", "/com1.js", "/graph-data.js.tmp", "/__pycache__/",
)

# Every way a browser might ask the teardown routes for a file outside the teardowns folder. None may work.
TEARDOWN_PROBES = (
    "/teardown/note?name=../config.json", "/teardown/note?name=..%2F..%2F..%2Fconfig", "/teardown/note?name=../../../config",
    "/teardown/note?name=config.json", "/teardown/note?name=%2e%2e%2f%2e%2e%2fconfig", "/teardown/note?name=..%5C..%5C..%5Cconfig",
    "/teardown/note?name=C:/Windows/win.ini", "/teardown/note?name=/etc/passwd", "/teardown/note?name=../REA",
    "/teardown/note?name=assets/../../../config", "/teardown/note?name=.", "/teardown/note?name=", "/teardown/note",
    "/teardown/image?name=../config.json", "/teardown/image?name=../../../config.json", "/teardown/image?name=..%2F..%2F..%2Fconfig.json",
    "/teardown/image?name=../../../viewer/index.html", "/teardown/image?name=x.png/../../../../config.json",
    "/teardown/image?name=..%5C..%5C..%5Cconfig.json", "/teardown/image?name=config.json%00.png", "/teardown/image?name=.png",
    "/teardown/image?name=../../../config.png", "/teardown/image",
)

# Runs the SERVED core.js in Node: the mute rule and the finish window, with a fake voice and clock.
NODE_HARNESS = r"""
const Core = require(process.argv[2]);
function spoken(search) {
  let count = 0;
  const synth = { speak() { count += 1; }, cancel() {} };
  function Utterance(text) { this.text = text; }
  const speaker = Core.createSpeaker({ synth, Utterance, getSearch: () => search });
  speaker.unlock();
  speaker.speak('Hello, sir.');
  return count;
}
function beforeClick() {
  let count = 0;
  const synth = { speak() { count += 1; }, cancel() {} };
  function Utterance(text) { this.text = text; }
  const speaker = Core.createSpeaker({ synth, Utterance, getSearch: () => '' });
  speaker.speak('Hello, sir.');
  const early = count;
  speaker.unlock();
  return { early, afterClick: count };
}
function finishWindow() {
  let now = 0, nextId = 1;
  const timers = new Map(), sentences = [], stops = [];
  const buffer = Core.createFinishBuffer({
    onSentence: text => sentences.push([now, text]),
    onInterrupt: word => stops.push([now, word]),
    setTimer: (fn, ms) => { const id = nextId++; timers.set(id, [now + ms, fn]); return id; },
    clearTimer: id => timers.delete(id),
  });
  const advance = to => {
    for (;;) {
      let due = null;
      for (const [id, [at, fn]] of timers) if (at <= to && (!due || at < due.at)) due = { id, at, fn };
      if (!due) break;
      timers.delete(due.id); now = due.at; due.fn();
    }
    now = to;
  };
  buffer.push('what did we agree'); advance(600); buffer.touch(); advance(800);
  buffer.push('with the hotel'); advance(5000);
  buffer.push('tell me about'); advance(5300); buffer.push('stop'); advance(9000);
  return { sentences, stops };
}
console.log(JSON.stringify({
  finishMs: Core.FINISH_MS,
  muted: spoken('?mute=1'), mutedAmongOthers: spoken('?tab=2&mute=1'), open: spoken(''), lookalike: spoken('?unmute=1'),
  beforeClick: beforeClick(), finish: finishWindow(),
  remember: Core.matchRemember('Remember that the till was short'), notRemember: Core.matchRemember('Do you remember that day'),
  focus: [Core.planFocus([]).mode, Core.planFocus([3]).mode, Core.planFocus([1, 2, 3, 4]).mode],
  brains: [Core.matchBrainSwitch('Switch brain to Deep Seek.'), Core.matchBrainSwitch('Jarvis, use the OpenAI brain'),
           Core.matchBrainSwitch('switch to claude'), Core.matchBrainSwitch('How does a brain switch work?'),
           Core.matchBrainSwitch('switch to decaf')],
  teardowns: ['reverse engineer igloo dot inc', 'tear down linear.app', 'how does stripe.com do that', 'what is example.com built with',
              'how does this site do that', 'Tear down the old shed', 'how should I reverse engineer my pricing',
              'what do we charge Hotel Marlowe', 'how does server.py work', 'Tear down Example.com/Docs'].map(s => { const hit = Core.matchTeardown(s); return hit ? hit.site : null; }),
  addresses: ['igloo dot inc', 'stripe.com slash pricing', 'localhost:5001', 'make it pop', 'wikipedia.org'].map(s => Core.findAddress(s)),
  commands: [['last', 'show me the last teardown'], ['open', 'open the full teardown'], ['close', 'close the teardown'],
             ['cancel', 'cancel the teardown'], ['close', 'tear down the old shed']].map(([k, s]) => Core.TEARDOWN_COMMANDS[k].test(s)),
}));
"""


class Report:
    def __init__(self) -> None:
        self.counts = {PASS: 0, FAIL: 0, WARN: 0}
        try:
            "✓✗".encode(sys.stdout.encoding or "ascii")
            self.marks = {PASS: "✓", FAIL: "✗", WARN: "!"}
        except (UnicodeEncodeError, LookupError):
            self.marks = {PASS: "+", FAIL: "x", WARN: "!"}

    def add(self, verdict: str, name: str, detail: str = "") -> bool:
        self.counts[verdict] += 1
        print(f" {self.marks[verdict]} {verdict.upper():4}  {name}" + (f"\n          {detail}" if detail else ""), flush=True)
        return verdict == PASS

    def summary(self) -> str:
        return f"PREFLIGHT: {self.counts[PASS]} pass, {self.counts[FAIL]} fail, {self.counts[WARN]} warn"


_PAGE_KEY: list[str] = []


# A text that is never a website address (the offline checks prove it). A test that must NOT start a
# teardown sends this. An empty site is not safe: on a Mac it means "the page in the front Chrome tab".
INERT_SITE = "make it pop"


def page_key() -> str:
    """The page key from its file, read once ("" when the server has not made one yet). It never
    leaves this process: every call is SIGNED with it. So a stranger on the port learns nothing."""
    if not _PAGE_KEY:
        _PAGE_KEY.append(server.read_page_key() or "")
    return _PAGE_KEY[0]


_INSTANCE: list[str] = []


def server_instance() -> str:
    """The name of this start of the server, asked once, and taken only from a server that PROVED it
    holds the page key ("" when none did: then nothing is signed). A signature names it, so a signature
    that a stranger on the port catches is worth nothing after Jarviz starts again."""
    if not _INSTANCE:
        holds, instance = server.ask_listener(page_key()) if page_key() else (None, "")
        _INSTANCE.append(instance if holds is True else "")
    return _INSTANCE[0]


def call_full(method: str, path: str, body: dict | None = None, timeout: float = 150,
              extra_headers: dict | None = None, key: bool = True) -> tuple[int, dict, bytes]:
    """One raw HTTP call to the running server. The path is sent exactly as given, and a redirect is
    not followed. The call is signed with the page key (the key itself is not sent), unless `key` is
    False. Returns the status, the response headers (lower-case names) and the body."""
    connection = http.client.HTTPConnection(server.HOST, server.PORT, timeout=timeout)
    try:
        headers, data = dict(extra_headers or {}), None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if key and page_key() and server_instance():
            headers.setdefault(server.SIGN_HEADER,
                               server.sign_request(page_key(), server_instance(), method, path, data or b""))
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        return response.status, {name.lower(): value for name, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def call(method: str, path: str, body: dict | None = None, timeout: float = 150,
         extra_headers: dict | None = None, key: bool = True) -> tuple[int, bytes]:
    status, _, raw = call_full(method, path, body, timeout, extra_headers, key)
    return status, raw


def call_json(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    status, raw = call(method, path, body)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except ValueError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def short(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# -------------------------------------------------------------------- checks

def check_server(report: Report) -> bool:
    try:
        status, page = call("GET", "/", timeout=5)
    except OSError as error:
        return report.add(FAIL, "Server is up and serving the viewer",
                          f"No answer on http://{server.HOST}:{server.PORT} ({error.strerror or error}). Start it: python server.py")
    if status == 403 and b"<title>Jarviz is locked</title>" in page:
        return report.add(FAIL, "Server is up and serving the viewer",
                          "The server did not take a request signed with the page key. " + (
                              "Its key is not the key on disk, or it was started again a moment ago. "
                              if server_instance() else
                              f"The program on port {server.PORT} did not prove that it holds your key, so nothing "
                              "signed was sent to it. ") + "Stop it and start Jarviz again: python server.py")
    ok = status == 200 and b'id="graph"' in page and b"<title>Jarviz</title>" in page
    return report.add(PASS if ok else FAIL, "Server is up and serving the viewer",
                      "" if ok else f"GET / gave {status}, and it is not the Jarviz page. Is another program on port {server.PORT}?")


def check_graph(report: Report, ctx: dict) -> None:
    name = "Graph data loads and the node count is above zero"
    status, raw = call("GET", "/graph-data.js")
    try:
        graph = build.parse_graph_text(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        report.add(FAIL, name, f"GET /graph-data.js gave {status} and it does not parse. Run: python build.py")
        return
    nodes = graph.get("nodes", [])
    ctx["graph"] = graph
    _, live = call_json("GET", "/status")
    in_memory = live.get("graph", {}).get("count")
    if not nodes:
        report.add(FAIL, name, "0 nodes. Check \"notes_dir\" in config.json, then run: python build.py")
    elif any(node.get("id") != index for index, node in enumerate(nodes)):
        report.add(FAIL, name, "A node id does not equal its index in the nodes array.")
    elif in_memory != len(nodes):
        report.add(FAIL, name, f"The file has {len(nodes)} nodes but the server holds {in_memory}. Restart the server.")
    else:
        report.add(PASS, name, f"{len(nodes)} nodes, {len(graph.get('links', []))} links")


def check_chat(report: Report, ctx: dict) -> None:
    name = "/chat returns a well-formed answer with a nodes array"
    nodes = ctx.get("graph", {}).get("nodes", [])
    if not nodes:
        report.add(WARN, name, "Not checked: there are no notes to ask about.")
        return
    subject = max(nodes, key=lambda node: len(node.get("excerpt", "")))["label"]
    question = f"What do my notes say about {subject}?"
    status, body = call_json("POST", "/chat", {"message": question, "session": ctx["session"]})
    shaped = isinstance(body.get("answer"), str) and isinstance(body.get("nodes"), list)
    if status == 200 and body.get("ok") and shaped and body["answer"].strip():
        valid = all(isinstance(i, int) and 0 <= i < len(nodes) for i in body["nodes"])
        if not valid:
            report.add(FAIL, name, f"nodes holds an index that is not in the graph: {body['nodes']}")
        elif not body["nodes"]:
            report.add(WARN, name, f"It answered but named no source note. Asked: {question!r}. Said: {short(body['answer'])}")
        else:
            report.add(PASS, name, f"nodes {body['nodes']}: {short(body['answer'])}")
    elif status == 503 and shaped and body.get("error") == "api_key_missing" and not ctx["key_set"]:
        report.add(WARN, name, "The brain is not connected yet. /chat gave a clean \"no key\" reply, not a crash.")
    else:
        report.add(FAIL, name, f"HTTP {status}, error {body.get('error')!r}: {short(body.get('detail') or body.get('answer') or 'no JSON body')}")


def check_key_and_model(report: Report, ctx: dict) -> None:
    """The active brain: one real call for the key, one for the model."""
    brain = ctx["brain"]
    key_name = f"The {brain['label']} key in config.json is valid (one real call)"
    model_name = f"The {brain['label']} model in config.json is one the key can reach"
    ctx["brain_ok"] = False
    if ctx["config"].get("_error"):
        report.add(FAIL, key_name, f"config.json cannot be read. {ctx['config']['_error']}")
        report.add(WARN, model_name, "Not checked: config.json must be readable first.")
        return
    result = brains.probe(ctx["config"])
    key_ok, key_detail = result["key"]
    model_ok, model_detail = result["model"]
    report.add(PASS if key_ok else FAIL, key_name, short(key_detail, 240))
    if model_ok is None:
        report.add(WARN, model_name, f"Not checked: it needs a valid key first. The model id is {brain['model']!r}.")
    else:
        report.add(PASS if model_ok else FAIL, model_name, short(model_detail, 400))
    ctx["brain_ok"] = bool(key_ok and model_ok)


def check_other_brains(report: Report, ctx: dict) -> None:
    """Every other brain that has a key must answer too, so a switch never lands on a dead brain."""
    others = [b for b in brains.describe_all(ctx["config"]) if b["name"] != ctx["brain"]["name"]]
    keyless = [b["label"] for b in others if not b["has_key"]]
    for brain in (b for b in others if b["has_key"]):
        result = brains.probe(ctx["config"], brain["name"])
        (key_ok, key_detail), (model_ok, model_detail) = result["key"], result["model"]
        works = bool(key_ok and model_ok)
        report.add(PASS if works else FAIL, f"The {brain['label']} brain is ready to switch to",
                   brain["model"] if works else short(model_detail or key_detail, 400))
    if keyless:
        print(f"   note  No key yet for: {', '.join(keyless)}. Add one in config.json to use that brain.", flush=True)


def check_remember(report: Report, ctx: dict) -> None:
    name = "/remember writes a real file and /chat can retrieve it at once"
    notes_dir = build.resolve_notes_dir(ctx["config"]["notes_dir"])
    before = ctx.get("graph", {}).get("nodes", [])
    captures = notes_dir / server.CAPTURES_DIR
    captures_was_there = captures.is_dir()
    token = "zq" + secrets.token_hex(3)
    problems: list[str] = []
    brain_note = ""
    receipt: dict = {}
    try:
        status, body = call_json("POST", "/remember", {"text": f"Preflight probe {token}: the code word is {CODE_WORD}",
                                                       "session": ctx["session"]})
        receipt = (body.get("action") or {}).get("receipt") or {}
        if status != 200 or not receipt:
            problems.append(f"/remember gave HTTP {status}: {short(body.get('answer', 'no receipt'))}")
        else:
            index = receipt["index"]
            note_file = notes_dir / receipt["file"]
            if not note_file.is_file() or token not in note_file.read_text(encoding="utf-8"):
                problems.append("the receipt names a file that is not on disk")
            on_disk = build.read_graph().get("nodes", [])
            if len(on_disk) != len(before) + 1 or on_disk[-1].get("label") != receipt["title"]:
                problems.append("graph-data.js did not gain the new node")
            _, found = call_json("GET", f"/search?q={token}")
            if [hit["index"] for hit in found.get("results", [])][:1] != [index]:
                problems.append("the live search index does not find the new note")
            if not ctx.get("brain_ok"):
                brain_note = "The /chat step was skipped: it needs a working brain."
            else:
                # A fresh session: the code word can then only come from the note, not from chat history.
                status, answer = call_json("POST", "/chat", {"message": f"What is the code word in my preflight probe {token}?",
                                                             "session": "preflight-" + secrets.token_hex(4)})
                if status != 200:
                    problems.append(f"/chat gave HTTP {status} ({answer.get('error')})")
                elif index not in answer.get("nodes", []) and CODE_WORD not in answer.get("answer", "").lower():
                    problems.append("/chat did not use the new note in its very next answer")
    finally:
        if receipt:                                  # tests clean up after themselves
            status, gone = call_json("POST", "/forget", {"index": receipt["index"], "label": receipt["title"],
                                                         "session": ctx["session"]})
            trash_name = (gone.get("action") or {}).get("trash")
            if status != 200 or not trash_name:
                problems.append(f"CLEANUP FAILED: the test note is still at {notes_dir / receipt['file']}")
            else:
                trash_file = notes_dir / trash_name
                trash_file.unlink(missing_ok=True)
                for folder in (trash_file.parent, captures):
                    if folder.is_dir() and not any(folder.iterdir()) and (folder != captures or not captures_was_there):
                        folder.rmdir()
            if len(build.read_graph().get("nodes", [])) != len(before):
                problems.append("the node count did not go back after cleanup")
    if problems:
        report.add(FAIL, name, "; ".join(problems))
    elif brain_note:
        report.add(WARN, name, "File written, indexed, found by search, then removed. " + brain_note)
    else:
        report.add(PASS, name, "written, indexed, used by /chat, then removed")


def check_links_stay_true(report: Report) -> None:
    """A birth or a take-back can change links among the OTHER notes: an 11th note crowds a shared [[link]],
    and a note named "Topic" stops [[Topic]] from joining the notes that share it. The live links must still
    be what a full rebuild gives. Pure: notes in a temporary folder, the steps Galaxy takes. No server."""
    name = "After a note is born or taken back, the links are the same as after a full rebuild"
    trouble: list[str] = []
    mended = checked = 0
    with tempfile.TemporaryDirectory() as folder:
        notes_dir = Path(folder)
        notes: list[build.Note] = []
        links: set[tuple[int, int]] = set()

        def make(title: str, text: str) -> build.Note:
            path = notes_dir / f"{title}.md"
            path.write_text(f"# {title}\n\n{text}\n", encoding="utf-8")
            return build.make_note(path, notes_dir)

        def mend(flipped: set[str]) -> None:
            nonlocal mended
            added, removed = build.relink(notes, links, flipped)
            links.update(added)
            links.difference_update(removed)
            mended += len(added) + len(removed)

        def birth(note: build.Note) -> None:            # as Galaxy.birth_note does it
            flipped = build.flipped_wikilinks(note, notes)
            new_links = build.links_for_new(note, notes)
            notes.append(note)
            links.update(new_links)
            mend(flipped)

        def take_back() -> None:                        # as Galaxy.forget_last and Galaxy.unbirth do it
            index = len(notes) - 1
            note = notes.pop()
            links.difference_update({link for link in links if index in link})
            mend(build.flipped_wikilinks(note, notes))

        def same(step: str) -> None:
            nonlocal checked
            checked += 1
            full = set(build.compute_links(notes))
            if links != full:
                trouble.append(f"{step}: {len(links - full)} links too many, {len(full - links)} missing")

        words = "alpha bravo charlie delta echo foxtrot golf hotel india juliett kilo".split()
        for word in words[:build.MAX_SHARED_LINK_NOTES]:
            birth(make(f"Card {word}", "Linked to [[Shared Idea]]."))
        same(f"{build.MAX_SHARED_LINK_NOTES} notes share [[Shared Idea]]")
        birth(make(f"Card {words[build.MAX_SHARED_LINK_NOTES]}", "Linked to [[Shared Idea]]."))
        same("one more note makes [[Shared Idea]] crowded")
        take_back()
        same("that note is taken back")
        for word in ("lima", "mike"):
            birth(make(f"Card {word}", "Filed under [[Topic]]."))
        same("two notes share [[Topic]]")
        birth(make("Topic", "What the cards are filed under."))
        same("a note named Topic is born")
        take_back()
        same("the note named Topic is taken back")
    if not trouble and not mended:
        trouble.append("no link among the other notes changed, so the check proved nothing")
    report.add(FAIL if trouble else PASS, name,
               "; ".join(trouble) or f"{checked} steps, each equal to a full rebuild; {mended} links among the other notes mended")


def check_served_files(report: Report) -> None:
    name = "The files the browser is served match the files on disk"
    stale, total = [], 0
    for path in sorted(server.VIEWER_ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in server.STATIC_TYPES:
            continue
        total += 1
        relative = path.relative_to(server.VIEWER_ROOT).as_posix()
        status, served = call("GET", "/" + quote(relative))
        if status != 200 or hashlib.sha256(served).digest() != hashlib.sha256(path.read_bytes()).digest():
            stale.append(relative)
    status, root_page = call("GET", "/")
    if status != 200 or root_page != (server.VIEWER_ROOT / "index.html").read_bytes():
        stale.append("/ (the start page)")
    if stale or not total:
        report.add(FAIL, name, "Stale or missing: " + ", ".join(stale or ["no viewer files found"]) +
                   ". An old server may still hold the port. Close it and start again.")
    else:
        report.add(PASS, name, f"{total} files identical")


def check_config_hidden(report: Report, ctx: dict) -> None:
    name = "config.json is NOT reachable from the browser"
    config = ctx["config"]
    keys = [str(config[spec["key"]]).encode("utf-8") for name, spec in brains.PROVIDERS.items()
            if brains.describe(config, name)["has_key"]]
    leaks = []
    for probe in CONFIG_PROBES + TEARDOWN_PROBES:
        try:
            status, body = call("GET", probe, timeout=10)
        except (http.client.HTTPException, OSError, ValueError):
            continue                                 # refused before it was even sent: good
        if b"_api_key" in body or any(key in body for key in keys) or (status == 200 and b"<title>Jarviz</title>" not in body):
            leaks.append(f"{probe} -> {status}")
    # The routes that talk about brains must name them, never show a key.
    for path in ("/status", "/search?q=key"):
        _, body = call("GET", path, timeout=10)
        if any(key in body for key in keys):
            leaks.append(f"{path} shows a key")
    if leaks:
        banner = "!" * 72
        print(f"\n{banner}\n  DANGER: A PRIVATE FILE IS REACHABLE FROM THE BROWSER. STOP THE SERVER NOW.\n"
              f"  {'; '.join(leaks)}\n{banner}\n", flush=True)
        report.add(FAIL, name, "LEAK: " + "; ".join(leaks))
    else:
        report.add(PASS, name, f"{len(CONFIG_PROBES) + len(TEARDOWN_PROBES)} ways tried, all refused")


def check_other_sites(report: Report) -> None:
    """A website open in the same browser must not be able to use, change or read Jarviz."""
    name = "Other websites cannot use the server or read the notes"
    evil = "http://evil.example"
    nothing = {"message": "", "session": "preflight-outsider"}      # empty: it can never reach the brain
    attempts = {
        "a POST from another website": call("POST", "/chat", nothing, extra_headers={"Origin": evil}),
        "a POST marked cross-site": call("POST", "/chat", nothing, extra_headers={"Sec-Fetch-Site": "cross-site"}),
        "a POST from another local port": call("POST", "/remember", {"text": ""}, extra_headers={"Origin": "http://127.0.0.1:9999"}),
        "a request under another host name": call("GET", "/status", extra_headers={"Host": f"evil.example:{server.PORT}"}),
        "a script tag on another website": call("GET", "/graph-data.js", extra_headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "script"}),
        "a form post (not JSON)": call("POST", "/chat", extra_headers={"Content-Type": "text/plain", "Content-Length": "0"}),
        # INERT_SITE is not an address, so this can never start a job, even if the guard were broken.
        "a teardown started by another website": call("POST", "/teardown/start", {"site": INERT_SITE}, extra_headers={"Origin": evil}),
        "a teardown cancelled by another website": call("POST", "/teardown/cancel", {}, extra_headers={"Sec-Fetch-Site": "cross-site"}),
        "a teardown note read by another website": call("GET", "/teardown/last", extra_headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "empty"}),
    }
    let_in = [what for what, (status, _) in attempts.items() if status not in (403, 415)]
    report.add(FAIL if let_in else PASS, name,
               "LET IN: " + "; ".join(let_in) if let_in else f"{len(attempts)} ways tried, all refused")


def check_lock(report: Report) -> None:
    """Another program or another account on this computer must get nothing without the page key.
    This check never sends the key itself anywhere: it signs, and it runs the door and the cookie rule
    in this process with a made-up key."""
    name = "Without your page key, a caller gets nothing"
    key, instance = page_key(), server_instance()
    if not key or not instance:
        report.add(FAIL, name, f"No page key on disk ({server.KEY_FILE}), or the server did not prove that it holds it. "
                               "Stop the server and start it again.")
        return
    if teardown.parse_site(INERT_SITE) is not None:
        report.add(FAIL, name, f"The test text {INERT_SITE!r} is read as a website. The check was not run.")
        return
    secret, wrong = key.encode("ascii"), "x" * len(key)
    nothing = {"message": "", "session": "preflight-outsider"}      # empty: it can never reach the brain
    empty_note = {"text": ""}                                        # empty: it can never be saved

    def outsider(method: str, path: str, body: dict | None = None, headers: dict | None = None):
        return call_full(method, path, body, timeout=10, extra_headers=headers, key=False)

    def signed(value: str) -> dict:
        return {server.SIGN_HEADER: value}

    def sign(method: str, path: str, body: bytes = b"", *, with_key: str = key, start: str = instance, now=None) -> dict:
        return signed(server.sign_request(with_key, start, method, path, body, now))

    def refused(status: int, headers: dict, body: bytes) -> bool:
        """Only the locked answer counts: the locked page, or the one locked line of JSON. No key and no cookie in it."""
        if status != 403 or secret in body or "set-cookie" in headers:
            return False
        if b"<title>Jarviz is locked</title>" in body:
            return b"<title>Jarviz</title>" not in body and b'id="graph"' not in body
        try:
            return json.loads(body.decode("utf-8")) == server.LOCKED_BODY
        except ValueError:
            return False

    # Empty texts, empty names and a site that is no site: none of these could do anything if the lock were broken.
    attempts = {
        "the page": outsider("GET", "/"),
        "the page by its file name": outsider("GET", "/index.html"),
        "the page script": outsider("GET", "/app.js"),
        "the index of the notes": outsider("GET", "/graph-data.js"),
        "the status": outsider("GET", "/status"),
        "a search in the notes": outsider("GET", "/search?q=coffee"),
        "a question to the brain": outsider("POST", "/chat", nothing),
        "a new note": outsider("POST", "/remember", empty_note),
        "a change of brain": outsider("POST", "/brain", {"brain": ""}),
        "a teardown": outsider("POST", "/teardown/start", {"site": INERT_SITE}),
        "the last teardown": outsider("GET", "/teardown/last"),
        "a second try without a key": outsider("GET", f"/?{server.AGAIN_PARAM}=1"),
        "a wrong key in the cookie": outsider("GET", "/status", headers={"Cookie": f"{server.KEY_COOKIE}={wrong}"}),
        "a wrong key in the address": outsider("GET", f"/?{server.KEY_PARAM}={wrong}"),
        "a wrong key in the old header": outsider("GET", "/status", headers={"X-Jarviz-Key": wrong}),
        "a signature made with a wrong key": outsider("GET", "/status", headers=sign("GET", "/status", with_key=wrong)),
        "a signature made for another address": outsider("GET", "/status", headers=sign("GET", "/search?q=x")),
        "a signature made for another method": outsider("GET", "/status", headers=sign("POST", "/status")),
        "a signature that is one hour old": outsider("GET", "/status", headers=sign("GET", "/status", now=time.time() - 3600)),
        "a signature made for another body": outsider("POST", "/remember", empty_note, headers=sign("POST", "/remember", b"{}")),
        "a signature made for another start of the server": outsider("GET", "/status", headers=sign("GET", "/status", start="s" * 16)),
        "a signature with no parts": outsider("GET", "/status", headers=signed("")),
        # A good signature opens the routes, but it is never traded for the cookie (the cookie IS the key).
        "a good signature at the door, to get the cookie": outsider(
            "GET", f"/?{server.KEY_PARAM}={wrong}&mute=1", headers=sign("GET", f"/?{server.KEY_PARAM}={wrong}&mute=1")),
        "a good signature at the second try, to get the cookie": outsider(
            "GET", f"/?{server.AGAIN_PARAM}=1", headers=sign("GET", f"/?{server.AGAIN_PARAM}=1")),
    }
    trouble = ["LET IN: " + what for what, (status, headers, body) in attempts.items() if not refused(status, headers, body)]
    if call("GET", "/status", timeout=10)[0] != 200:
        trouble.append("a call with a good signature is not let in")

    # The door and the cookie rule, run in THIS process with a made-up key. So the real key is never sent
    # in an address or as a cookie by this check. (The real browser proves the live path every day.)
    class Caller:
        command, path = "GET", "/"
        _unlocked, _cookie_ok = server.Handler._unlocked, server.Handler._cookie_ok
        _door, _refuse_locked, _send_json = server.Handler._door, server.Handler._refuse_locked, server.Handler._send_json

        def __init__(self, cookie: str = ""):
            self.headers, self.sent = email.message.Message(), []
            if cookie:
                self.headers["Cookie"] = cookie

        def _send(self, status: int, data: bytes, content_type: str, extra: tuple = ()) -> None:
            self.sent.append((status, dict(extra)))

    def door(pairs: list, cookie: str = "") -> tuple[int, dict]:
        caller = Caller(cookie)
        caller._door(pairs)
        return caller.sent[-1]

    made_up, real, jar = "k" * len(key), server.PAGE_KEY, server.KEY_COOKIE
    server.PAGE_KEY = made_up
    try:
        cookie_rule = (Caller(f"other=1; {jar}={made_up}")._unlocked()
                       and Caller(f"{jar}={wrong}; {jar}={made_up}")._unlocked()       # a planted wrong cookie beside the right one
                       and not Caller(f"{jar}={wrong}; {jar}=")._unlocked()
                       and not Caller(f"{jar}x={made_up}")._unlocked()
                       and not Caller()._unlocked())
        status, sent = door([(server.KEY_PARAM, made_up), ("mute", "1")])
        cookie = sent.get("Set-Cookie", "")
        right_key = (status == 303 and sent.get("Location") == "/?mute=1" and cookie.startswith(f"{jar}={made_up};")
                     and "HttpOnly" in cookie and "SameSite=Strict" in cookie)
        again = door([(server.AGAIN_PARAM, "1")], cookie=f"{jar}={made_up}")
        wrong_key = door([(server.KEY_PARAM, wrong)])
    finally:
        server.PAGE_KEY = real
    if not cookie_rule:
        trouble.append("the cookie rule is wrong (the right cookie must open, also beside a planted wrong one)")
    if not right_key:
        trouble.append("the door does not turn the right key into an HttpOnly, SameSite=Strict cookie and a clean address")
    if again[0] != 303 or again[1].get("Location") != "/":
        trouble.append("the second try of a browser that has the cookie does not go on to the page")
    if wrong_key[0] != 403 or "Set-Cookie" in wrong_key[1]:
        trouble.append("the door gives a cookie for a wrong key")

    # The proof that a tool asks for before it signs, and that `python server.py --link` asks for before it
    # shows the link: right for the key, wrong for any other key, and no key in it. Only a nonce is sent.
    status, _, proof = outsider("GET", f"{server.PROOF_ROUTE}?nonce={'n' * 32}")
    if status != 200 or secret in proof or server.listener_holds(key) is not True or server.listener_holds(wrong) is not False:
        trouble.append("the server's proof that it holds the key does not work")
    if outsider("GET", server.PROOF_ROUTE)[0] != 400 or outsider("GET", f"{server.PROOF_ROUTE}?nonce=short")[0] != 400:
        trouble.append("the proof route takes a bad nonce")

    # The key itself is never inside anything the server sends, and never inside viewer/.
    shown = [path for path in ("/", "/app.js", "/core.js", "/graph-data.js", "/status", "/teardown/diag")
             if secret in call("GET", path, timeout=10)[1]]
    shown += ["viewer/" + path.relative_to(server.VIEWER_ROOT).as_posix()
              for path in sorted(server.VIEWER_ROOT.rglob("*")) if path.is_file() and secret in path.read_bytes()]
    if shown:
        trouble.append("THE KEY IS SHOWN in " + ", ".join(shown))
    report.add(FAIL if trouble else PASS, name,
               "; ".join(trouble) if trouble else f"{len(attempts)} ways tried without it, all refused. With it, the page opens.")


def check_persona_home(report: Report) -> None:
    """The character lives in the persona block of server.py. The page has no butler words of its own."""
    name = "The butler's words live in server.py only: the page gets them with /status"
    trouble = []
    script = (server.VIEWER_ROOT / "app.js").read_text(encoding="utf-8")
    table = re.search(r"const lines = \{(.*?)\n  \};", script, re.DOTALL)
    named = set(re.findall(r"^\s*(\w+):", table.group(1), re.MULTILINE)) if table else set()
    used = set(re.findall(r"\blines\.(\w+)", script))
    if named != set(server.PAGE_LINES):
        trouble.append("`lines` in viewer/app.js and PAGE_LINES in server.py do not name the same lines: "
                       + ", ".join(sorted(named ^ set(server.PAGE_LINES))))
    if used - named:
        trouble.append("the page uses a line that has no words: " + ", ".join(sorted(used - named)))
    for path in sorted(server.VIEWER_ROOT.glob("*")):
        if path.suffix.lower() in (".js", ".html") and path.name != build.GRAPH_PATH.name:
            if re.search(r"\bsir\b", path.read_text(encoding="utf-8", errors="replace"), re.IGNORECASE):
                trouble.append(f"viewer/{path.name} has a butler line of its own (\"sir\")")
    _, status = call_json("GET", "/status")
    if status.get("lines") != server.PAGE_LINES:
        trouble.append("/status does not give the page its lines. Start the server again")
    report.add(FAIL if trouble else PASS, name, "; ".join(trouble) if trouble else f"{len(named)} lines")


def check_pinned_libraries(report: Report) -> None:
    name = "The two outside libraries are pinned and match the security policy"
    _, page = call("GET", "/")
    _, app = call("GET", "/app.js")
    problems = []
    if server.GRAPH_LIB_URL.encode() not in page or b'integrity="sha384-' not in page:
        problems.append("index.html does not load the pinned 3d-force-graph file with its hash")
    if f"'{server.THREE_LIB_DIR}build/three.module.js'".encode() not in app:
        problems.append("app.js does not load three.js from the pinned folder")
    if b"@latest" in page + app:
        problems.append("something loads a \"latest\" version")
    report.add(FAIL if problems else PASS, name,
               "; ".join(problems) + ". Change the version in server.py, index.html and app.js together." if problems else "")


def check_teardown_kit(report: Report) -> None:
    name = "The teardown kit is ready with the pinned REA version"
    _, kit = call_json("GET", "/teardown/diag")
    rea = kit.get("rea") or {}
    ready = kit.get("ready") is True and rea.get("installed") and rea.get("installed") == rea.get("pinned")
    report.add(PASS if ready else FAIL, name,
               f"REA {rea.get('installed')}, Node.js {(kit.get('node') or {}).get('version')}, Chrome found" if ready
               else short("; ".join(kit.get("problems") or ["/teardown/diag did not answer"]), 300))


def check_teardown_offline(report: Report, ctx: dict) -> None:
    name = "The teardown offline checks pass (addresses, fingerprints, tagged evidence)"
    results = teardown.offline_checks()
    failed = [f"{label} ({detail})" for label, passed, detail in results if not passed]
    failed += ctx.get("teardown_routes") or []
    report.add(FAIL if failed else PASS, name,
               short("; ".join(failed), 400) if failed else f"{len(results)} checks, and the voice routes")


def check_no_investigation_chrome(report: Report) -> None:
    name = "No investigation Chrome is running and no job profile is left"
    _, status = call_json("GET", "/teardown/status")
    if (status.get("job") or {}).get("state") == "running":
        report.add(WARN, name, "Not checked: a teardown is running now.")
        return
    chromes, profiles = teardown.investigation_chromes(), teardown.leftover_profiles()
    if chromes is None:         # "could not look" is never a pass
        report.add(WARN, name, "Not checked: the list of running programs could not be read.")
        return
    report.add(FAIL if chromes or profiles else PASS, name,
               f"{len(chromes)} Chrome processes and {len(profiles)} profile folders are left. Restart the server to sweep them."
               if chromes or profiles else "")


def check_voice_rules(report: Report, ctx: dict) -> None:
    mute_name = "A ?mute=1 tab is silent: the one speak function refuses"
    finish_name = "The finish window joins a mid-sentence pause, and \"stop\" skips it"
    sources = {}
    for served in ("core.js", "app.js", "index.html"):
        status, raw = call("GET", "/" + served)
        sources[served] = raw.decode("utf-8", "replace") if status == 200 else ""
    speak_calls = {served: len(re.findall(r"\b(?:synth|speechSynthesis)\s*\.\s*speak\s*\(", text))
                   for served, text in sources.items()}
    other_sound = [served for served, text in sources.items() if re.search(r"new\s+Audio\s*\(|AudioContext|\.play\s*\(", text)]
    problems = []
    if speak_calls != {"core.js": 1, "app.js": 0, "index.html": 0}:
        problems.append(f"there must be exactly one synth.speak call, in core.js; found {speak_calls}")
    if other_sound:
        problems.append(f"another way to make sound exists in {other_sound}")
    if "getSearch: () => window.location.search" not in sources["app.js"]:
        problems.append("app.js does not hand the page address to the speaker")

    node = shutil.which("node")
    if not node:
        verdict = FAIL if problems else WARN
        report.add(verdict, mute_name, "; ".join(problems) or "Read the code only. Install Node.js to run the rule for real.")
        report.add(WARN, finish_name, "Not checked: it needs Node.js.")
        return
    with tempfile.TemporaryDirectory() as folder:
        core_file = Path(folder) / "core.js"
        harness_file = Path(folder) / "harness.js"
        core_file.write_text(sources["core.js"], encoding="utf-8")
        harness_file.write_text(NODE_HARNESS, encoding="utf-8")
        run = subprocess.run([node, str(harness_file), str(core_file)], capture_output=True, text=True, timeout=30)
    try:
        result = json.loads(run.stdout)
    except ValueError:
        report.add(FAIL, mute_name, "The served core.js did not run: " + short(run.stderr or run.stdout or "no output", 200))
        report.add(FAIL, finish_name, "Not checked: core.js did not run.")
        return

    if result["muted"] != 0 or result["mutedAmongOthers"] != 0:
        problems.append("speak() made sound in a ?mute=1 tab")
    if result["open"] != 1 or result["lookalike"] != 1:
        problems.append("speak() stayed silent in a normal tab")
    if result["beforeClick"] != {"early": 0, "afterClick": 1}:
        problems.append("the first line must wait for the first click, then speak once")
    report.add(FAIL if problems else PASS, mute_name, "; ".join(problems))

    finish_ms = result["finishMs"]
    expected = [[800 + finish_ms, "what did we agree with the hotel"]]
    rules = []
    if result["finish"]["sentences"] != expected:
        rules.append(f"a paused sentence was not sent whole, once: {result['finish']['sentences']}")
    if result["finish"]["stops"] != [[5300, "stop"]]:
        rules.append(f"\"stop\" did not act at once: {result['finish']['stops']}")
    if result["remember"] != "the till was short" or result["notRemember"] is not None:
        rules.append("the \"remember that\" route matched the wrong sentences")
    if result["focus"] != ["still", "fly", "cluster"]:
        rules.append(f"the camera plan is wrong: {result['focus']}")
    if result["brains"] != ["Deep Seek", "OpenAI", "claude", None, None]:
        rules.append(f"the \"switch brain\" route matched the wrong sentences: {result['brains']}")
    routes = []
    if result.get("teardowns") != ["igloo.inc", "linear.app", "stripe.com", "example.com", "this", None, None, None,
                                   None, "Example.com/Docs"]:
        routes.append(f"the teardown route matched the wrong sentences: {result.get('teardowns')}")
    if result.get("addresses") != ["igloo.inc", "stripe.com/pricing", "localhost:5001", None, "wikipedia.org"]:
        routes.append(f"spoken addresses are read wrong: {result.get('addresses')}")
    if result.get("commands") != [True, True, True, True, False]:
        routes.append(f"the teardown commands matched the wrong sentences: {result.get('commands')}")
    ctx["teardown_routes"] = routes
    report.add(FAIL if rules else PASS, finish_name, "; ".join(rules) or f"FINISH_MS = {finish_ms}")


def check_tag_intent(report: Report) -> None:
    """A [TEARDOWN:...] tag from the brain runs only when the user's sentence asks for a teardown: a teardown verb,
    or a "how is it built" question that names a site. A sentence that only mentions a website, or a "how" question
    about the notes, is not enough: a note could then lead the brain to start one. Pure: no server."""
    name = "A teardown tag runs only for a sentence that asks for a teardown"
    asks = ("Reverse engineer Stripe's homepage", "tear the Linear site down", "Take igloo.inc apart", "x-ray this site",
            "how does igloo.inc do that", "How is the Stripe homepage built?", "what is linear.app built with",
            "What does Vercel's site use?", "what's under the hood of this site", "What tech stack does linear.app use?",
            "Take apart the old pricing page")
    mentions = ("What did I write about Stripe's site?", "Summarise my notes on the Hotel Marlowe website",
                "Which site did we pick for the farmers market stall?", "Open the site visit note",
                "Is the website launch in my notes?", "Remember that the homepage needs a new photo", "Good morning",
                "What do we use for invoicing?", "How do I make cold brew?", "How does the loan work?",
                "How does the roaster maintenance work?", "What is the Wi-Fi password?")
    intent = server.TAG_INTENT["TEARDOWN"]
    trouble = [f"missed: {said!r}" for said in asks if not intent(said)]
    trouble += [f"let in: {said!r}" for said in mentions if intent(said)]
    if not server.TAG_INTENT["REMEMBER"]("remember that the homepage needs a new photo"):
        trouble.append("the REMEMBER intent no longer matches \"remember that the homepage needs a new photo\"")
    report.add(FAIL if trouble else PASS, name,
               "; ".join(trouble) or f"{len(asks)} requests let in, {len(mentions)} mentions kept out")


def main() -> int:
    report = Report()
    config = server.load_config()
    brain = brains.describe(config)
    ctx = {"config": config, "brain": brain, "key_set": brain["has_key"], "session": "preflight-" + secrets.token_hex(4)}
    print(f"Jarviz preflight  {time.strftime('%Y-%m-%d %H:%M:%S')}  http://{server.HOST}:{server.PORT}"
          f"  brain: {brain['label']} / {brain['model']}\n")
    if check_server(report):
        check_graph(report, ctx)
        check_chat(report, ctx)
        check_key_and_model(report, ctx)
        check_other_brains(report, ctx)
        check_remember(report, ctx)
        check_links_stay_true(report)
        check_served_files(report)
        check_config_hidden(report, ctx)
        check_other_sites(report)
        check_lock(report)
        check_persona_home(report)
        check_pinned_libraries(report)
        check_voice_rules(report, ctx)
        check_tag_intent(report)
        check_teardown_kit(report)
        check_teardown_offline(report, ctx)
        check_no_investigation_chrome(report)
    print("\n" + report.summary())
    return 1 if report.counts[FAIL] else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
