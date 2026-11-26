"""Jarviz preflight: run every LIVE chain end to end and print a verdict.

    python preflight.py        (the server must be running: python server.py)

Real calls against the running system. No mocks. The exit code is 0 only when nothing failed.
"Done" means this passed. Add a check here every time something breaks.
"""
from __future__ import annotations

import contextlib
import email.message
import hashlib
import io
import http.client
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

import brains
import build
import server
import teardown

PASS, FAIL, WARN = "pass", "fail", "warn"


def new_code_word() -> str:
    """A made-up word that is easy to say, new for every run: "kotabeli". No note holds it before the test."""
    return "".join(secrets.choice("bdfgklmnprstvz") + secrets.choice("aeiou") for _ in range(4))

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
  questions: ['How did my teardown of stripe.com work?', 'What does my last teardown of igloo.inc use?', 'Do not tear down stripe.com',
              'How does stripe.com work?', 'How does the stripe.com homepage work?', 'What is igloo.inc built with?',
              'How does this site work?', 'How does example.com work? Do not tear it down.', 'How does example.com work?',
              'do not tear down stripe.com'].map(s => { const hit = Core.matchTeardown(s); return hit ? hit.site : null; }),
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


def call_json(method: str, path: str, body: dict | None = None, timeout: float = 150) -> tuple[int, dict]:
    status, raw = call(method, path, body, timeout)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except ValueError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def try_probe(send) -> tuple[str, object]:
    """Send one probe. ("answer", what came back), ("not_sent", why) when the client itself would not send it, or
    ("no_answer", why) when it went out and no answer came. Only an answer can show that the server refused:
    a probe with no answer is NOT verified, never "refused"."""
    try:
        return "answer", send()
    except (http.client.InvalidURL, ValueError) as error:       # before sending: a path or header http.client refuses
        return "not_sent", type(error).__name__
    except (OSError, http.client.HTTPException) as error:       # a timeout, a reset, a broken answer
        return "no_answer", type(error).__name__


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
    elif (status == 503 and shaped and not ctx["key_set"]
          and body.get("error") == ("cli_missing" if ctx["brain"].get("sign_in") else "api_key_missing")):
        report.add(WARN, name, "The brain is not connected yet. /chat gave a clean " +
                   ("\"not installed\"" if ctx["brain"].get("sign_in") else "\"no key\"") + " reply, not a crash.")
    else:
        report.add(FAIL, name, f"HTTP {status}, error {body.get('error')!r}: {short(body.get('detail') or body.get('answer') or 'no JSON body')}")


def check_key_and_model(report: Report, ctx: dict) -> None:
    """The active brain: one real call for the key (a sign-in brain: its sign-in), one for the model."""
    brain = ctx["brain"]
    if brain.get("sign_in"):
        key_name = f"The {brain['label']} sign-in works (one real call)"
        model_name = f"The {brain['label']} model is one the sign-in can reach"
    else:
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
        report.add(WARN, model_name, f"Not checked: it needs a {'working sign-in' if brain.get('sign_in') else 'valid key'} "
                                     f"first. The model id is {brain['model']!r}.")
    else:
        report.add(PASS if model_ok else FAIL, model_name, short(model_detail, 400))
    ctx["brain_ok"] = bool(key_ok and model_ok)


def check_other_brains(report: Report, ctx: dict) -> None:
    """Every other brain that has a key (a sign-in brain: whose tool is installed) must answer too, so a switch never
    lands on a dead brain."""
    others = [b for b in brains.describe_all(ctx["config"]) if b["name"] != ctx["brain"]["name"]]
    keyless = [b["label"] for b in others if not b["has_key"] and not b.get("sign_in")]
    missing = [b["label"] for b in others if not b["has_key"] and b.get("sign_in")]
    for brain in (b for b in others if b["has_key"]):
        result = brains.probe(ctx["config"], brain["name"])
        (key_ok, key_detail), (model_ok, model_detail) = result["key"], result["model"]
        works = bool(key_ok and model_ok)
        report.add(PASS if works else FAIL, f"The {brain['label']} brain is ready to switch to",
                   brain["model"] if works else short(model_detail or key_detail, 400))
    if keyless:
        print(f"   note  No key yet for: {', '.join(keyless)}. Add one in config.json to use that brain.", flush=True)
    if missing:
        print(f"   note  Not installed: {', '.join(missing)}. Install the tool and sign in to it to use that brain.",
              flush=True)


def check_brain_locks(report: Report, ctx: dict) -> None:
    """Each installed sign-in brain: a REAL trap proves that this version of its tool, with these flags, cannot read a
    file, write a file or run a command. It runs before any question goes through the server. "Could not look" is
    never a pass."""
    installed = [b for b in brains.describe_all(ctx["config"]) if b.get("sign_in") and b["has_key"]]
    if not installed:
        print("   note  No sign-in brain (Codex, Antigravity) is installed: there is no lock to prove.", flush=True)
        return
    # A proof on file is STRONG when the trap saw a refused attempt (True), WEAK when nothing came back and nothing
    # ran but no refused attempt was seen (False), or there is none (None). Read only when brains.py has it.
    lock_seen = getattr(brains, "lock_seen", None)
    for brain in installed:
        name = f"The {brain['label']} brain is locked: no file read, no file write, no command (a real trap)"
        try:
            held, detail = brains.prove_lock(ctx["config"], brain["name"])
        except Exception as error:      # noqa: BLE001 - a check that stops says so, it never crashes preflight
            held, detail = None, f"could not look: the check stopped ({type(error).__name__})"
        if held is True:
            try:
                seen = lock_seen(brain["name"]) if callable(lock_seen) else None
            except Exception:           # noqa: BLE001 - a proof that cannot be read is not a strong one
                seen = None
            if seen is True:
                report.add(PASS, name, short(detail, 300))
            else:
                report.add(WARN, name, f"{short(detail, 300)}. The lock held, but the trap saw no refused attempt.")
        elif held is False:
            report.add(FAIL, name, short(detail, 300) + ". Jarviz refuses this brain until the lock holds.")
        else:
            report.add(FAIL, name, "NOT VERIFIED: " + short(detail, 300))


def check_brains_offline(report: Report) -> None:
    name = "The brain offline checks pass (sign-in parsers, command lines, the room, the lock proof)"
    try:
        results = brains.offline_checks()
    except Exception as error:          # noqa: BLE001 - a check that stops says so, it never crashes preflight
        report.add(FAIL, name, f"the checks stopped: {type(error).__name__}: {short(str(error), 200)}")
        return
    failed = [f"{label} ({detail})" for label, passed, detail in results if not passed]
    report.add(FAIL if failed else PASS, name, short("; ".join(failed), 400) if failed else f"{len(results)} checks")


def remove_fixture(notes_dir: Path, receipt: dict) -> tuple[bool, str]:
    """Remove a test note by hand, for the one case where /forget refused its receipt. Only when it is CERTAINLY
    the fixture: a regular file inside the notes folder at the receipt's path, whose bytes have the sha256 the receipt
    names ("born"). A file that changed stays. Nothing else is ever removed. (True, "") or (False, why it stays)."""
    relative, born = receipt.get("file"), receipt.get("born")
    if not isinstance(relative, str) or not isinstance(born, str) or not re.fullmatch(r"[0-9a-f]{64}", born):
        return False, "the receipt does not name the test note fully, so nothing was removed"
    root = notes_dir.resolve()
    target = Path(os.path.normpath(root / relative))        # the place itself: a link there is not followed
    if not target.is_relative_to(root) or target.suffix.lower() != ".md":
        return False, "the receipt names a place outside the notes folder, so nothing was removed"
    try:
        if not stat.S_ISREG(target.lstat().st_mode):
            return False, f"the test note at {target} is not a plain file any more, so it stays"
        if hashlib.sha256(target.read_bytes()).hexdigest() != born:
            return False, f"the test note at {target} was changed after it was made, so it stays"
        target.unlink()
    except FileNotFoundError:
        return False, f"the test note is not at {target} any more"
    except OSError as error:
        return False, f"the test note at {target} could not be removed ({error.strerror or error})"
    return True, ""


def check_remember(report: Report, ctx: dict) -> None:
    name = "/remember writes a real file and /chat can retrieve it at once"
    notes_dir = build.resolve_notes_dir(ctx["config"]["notes_dir"])
    before = ctx.get("graph", {}).get("nodes", [])
    captures = notes_dir / server.CAPTURES_DIR
    captures_was_there = captures.is_dir()
    token = "zq" + secrets.token_hex(3)
    code_word = new_code_word()             # new in every run: an answer that holds it can only come from the note
    problems: list[str] = []
    brain_note = ""
    cleanup_note = ""
    sourced = ""
    receipt: dict = {}
    try:
        status, body = call_json("POST", "/remember", {"text": f"Preflight probe {token}: the code word is {code_word}",
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
                elif code_word not in str(answer.get("answer", "")).lower():
                    problems.append("/chat did not use the new note in its very next answer (the code word is not in it)")
                sourced = ("it named the note as a source" if index in (answer.get("nodes") or [])
                           else "it did not name the note as a source")
    finally:
        if receipt:                                  # tests clean up after themselves
            status, gone = call_json("POST", "/forget", {"index": receipt["index"], "label": receipt["title"],
                                                         "build": receipt.get("build"), "born": receipt.get("born"),
                                                         "session": ctx["session"]})
            trash_name = (gone.get("action") or {}).get("trash")
            if status != 200 or not trash_name:
                # /forget rightly refuses a receipt of a galaxy that was built again while the check ran. Then the
                # test note goes by hand, but ONLY when it is certainly the fixture.
                removed, why = remove_fixture(notes_dir, receipt)
                if removed:
                    cleanup_note = (f"/forget refused the test note's receipt (HTTP {status}: "
                                    f"{short(gone.get('answer') or 'no reason', 120)}), as it does when the galaxy is "
                                    "built again while the check runs. So the check removed the test note itself (its "
                                    "bytes were the ones it wrote). The index still names it: run python build.py to "
                                    "bring the index in line.")
                    if captures.is_dir() and not any(captures.iterdir()) and not captures_was_there:
                        captures.rmdir()
                else:
                    problems.append(f"CLEANUP FAILED: /forget refused (HTTP {status}), and {why}")
            else:
                trash_file = notes_dir / trash_name
                trash_file.unlink(missing_ok=True)
                for folder in (trash_file.parent, captures):
                    if folder.is_dir() and not any(folder.iterdir()) and (folder != captures or not captures_was_there):
                        folder.rmdir()
            if not cleanup_note and len(build.read_graph().get("nodes", [])) != len(before):
                problems.append("the node count did not go back after cleanup")
    if problems:
        report.add(FAIL, name, "; ".join(problems + ([cleanup_note] if cleanup_note else [])))
    elif cleanup_note:
        report.add(WARN, name, cleanup_note)
    elif brain_note:
        report.add(WARN, name, "File written, indexed, found by search, then removed. " + brain_note)
    else:
        report.add(PASS, name, f"written, indexed, used by /chat ({sourced}), then removed")


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
        # A title that more notes use than MAX_SHARED_LINK_NOTES is crowded with the note and without it: it
        # changes no link, so it must not make relink() look at every pair of its users.
        for number in range(build.MAX_SHARED_LINK_NOTES + 2):
            birth(make(f"Big card {number}", "Filed under [[Big Topic]]."))
        same(f"{build.MAX_SHARED_LINK_NOTES + 2} notes share [[Big Topic]]")
        big = make("Big Topic", "What the big cards are filed under.")
        if "big topic" in build.flipped_wikilinks(big, notes):
            trouble.append("a note named Big Topic marks the crowded [[Big Topic]] as flipped when it comes")
        birth(big)
        same("a note named Big Topic is born")
        if "big topic" in build.flipped_wikilinks(notes[-1], notes[:-1]):
            trouble.append("a note named Big Topic marks the crowded [[Big Topic]] as flipped when it goes")
        take_back()
        same("the note named Big Topic is taken back")
        birth(make("Big Topic", "What the big cards are filed under. See also [[Big Topic]]."))   # it links to itself
        same("a note named Big Topic that links to [[Big Topic]] is born")
        take_back()
        same("the note named Big Topic that links to itself is taken back")
    if not trouble and not mended:
        trouble.append("no link among the other notes changed, so the check proved nothing")
    report.add(FAIL if trouble else PASS, name,
               "; ".join(trouble) or f"{checked} steps, each equal to a full rebuild; {mended} links among the other notes mended")


def check_take_back_safe(report: Report) -> None:
    """A birth, a take-back (unbirth) or a forget that fails leaves the index in memory as it was, and the note's
    file where it was. A take-back deletes only the bytes that Jarviz wrote, and a put-back never writes over a
    file. Pure: a test Galaxy over a temporary folder, with a fake save. No server. For the length of the check,
    build.write_graph is a recorder: the check fails if anything called it."""
    name = "A note that fails to be born or taken back leaves the index and its file as they were"
    trouble: list[str] = []
    real_write, writes = build.write_graph, []
    build.write_graph = lambda *args, **kwargs: writes.append(args)
    try:
        with tempfile.TemporaryDirectory() as folder:
            _take_back_steps(Path(folder).resolve(), trouble)
        with tempfile.TemporaryDirectory() as folder:
            _vault_steps(Path(folder).resolve(), trouble)
        # "build" and "born" are required, and texts: without one, or with another kind, it is a bad request like
        # a bad index. It is refused before the galaxy is read.
        for payload in ({"index": 0, "label": "x", "born": "f"}, {"index": 0, "label": "x", "build": 5, "born": "f"},
                        {"index": 0, "label": "x", "build": None, "born": "f"}, {"index": 0, "label": "x", "build": "b"},
                        {"index": 0, "label": "x", "build": "b", "born": 5}):
            if server.forget_route(payload)[0] != 400:
                trouble.append(f"a forget with {payload} is not refused as a bad request")
    except Exception as error:                      # noqa: BLE001 - a check that stops says so, it never crashes preflight
        trouble.append(f"the check stopped: {type(error).__name__}: {error}")
    finally:
        build.write_graph = real_write
    if writes:
        trouble.append(f"build.write_graph was called {len(writes)} times: only the fake save may run")
    report.add(FAIL if trouble else PASS, name,
               "; ".join(trouble) or "birth, take-back and forget: the index and the file came back each time; "
                                     "a changed file stays; a new file at the old place is never written over; "
                                     "another notes folder with the same path is not touched")


def _vault_steps(root: Path, trouble: list[str]) -> None:
    """A receipt names its creation fully: the notes folder ("vault") and the bytes ("born"). (k) A take-back after a
    rebuild of the SAME folder still works. (l) The galaxy moved to another folder, and Jarviz made a note there at the
    same path, index and title: the receipt of the first folder takes nothing back there. (m) A birth bound to a
    folder (a teardown) writes nothing when the galaxy shows another one. Each folder is a subfolder of `root`."""
    first, second = root / "first notes", root / "second notes"
    first.mkdir()
    second.mkdir()
    galaxy = server.Galaxy()
    galaxy.notes_dir, galaxy.refresh_if_rebuilt, galaxy._save_graph = first, lambda: None, lambda: None
    text = "# Same place\n\nJarviz wrote this.\n"

    def move_to(folder: Path, build_id: str) -> None:          # as a rebuild of that folder leaves the index
        galaxy.notes_dir, galaxy.build_id, galaxy.rev = folder, build_id, 0
        galaxy.notes[:] = [build.make_note(path, folder) for path in build.note_files(folder)]
        galaxy.links.clear()
        galaxy.links.update(build.compute_links(galaxy.notes))

    receipt = galaxy.birth_note("teardowns/Same place.md", text)
    shown = json.dumps(str(first))[1:-1] in json.dumps(receipt)        # the path as JSON writes it (\\ on Windows)
    if not re.fullmatch(r"[0-9a-f]{16}", str(receipt.get("vault"))) or shown:
        trouble.append(f"(k) the receipt does not carry a short fingerprint of the folder, or it shows the path: "
                       f"{receipt.get('vault')!r}")
    move_to(first, "first, built again")
    try:
        galaxy.unbirth(receipt)
    except (OSError, ValueError) as error:
        trouble.append(f"(k) after a rebuild of the same folder, the take-back was refused: {error}")
    if (first / receipt["file"]).exists() or galaxy.notes:
        trouble.append("(k) after a rebuild of the same folder, the take-back left the note")

    receipt = galaxy.birth_note("teardowns/Same place.md", text)
    move_to(second, "second")
    there = galaxy.birth_note("teardowns/Same place.md", "# Same place\n\nJarviz wrote this in the second folder.\n")
    if (there["index"], there["title"], there["file"]) != (receipt["index"], receipt["title"], receipt["file"]):
        trouble.append("(l) the note in the second folder is not at the same index, title and path")
    kept, before = (second / there["file"]).read_bytes(), [note.path for note in galaxy.notes]
    try:
        galaxy.unbirth(receipt)
        trouble.append("(l) the receipt of the first folder took back the note of the second folder")
    except ValueError:
        pass
    if (not (second / there["file"]).is_file() or (second / there["file"]).read_bytes() != kept
            or [note.path for note in galaxy.notes] != before or not (first / receipt["file"]).is_file()
            or list(root.rglob("*.taken*"))):
        trouble.append("(l) a receipt of another folder moved a file or changed the index")
    galaxy.unbirth(there)                           # its own receipt works

    bound = server.vault_of(first)                  # (m) a teardown bound to the first folder, the galaxy on the second
    try:
        galaxy.birth_note("teardowns/Bound.md", "# Bound\n\nA teardown note.\n", vault=bound)
        trouble.append("(m) a birth bound to another folder was written")
    except server.NotesMoved:
        pass
    if (second / "teardowns" / "Bound.md").exists() or (first / "teardowns" / "Bound.md").exists():
        trouble.append("(m) a refused birth left a file")


def _take_back_steps(notes_dir: Path, trouble: list[str]) -> None:
    """The steps of check_take_back_safe. Each step leaves the folder and the index ready for the next one."""
    galaxy = server.Galaxy()
    galaxy.notes_dir = notes_dir
    galaxy.refresh_if_rebuilt = lambda: None        # Galaxy.load() would run build.build() over the real notes
    save: dict = {"error": None, "first": None}     # "first" runs inside the save, before its error

    def fake_save() -> None:                        # the real one writes viewer/graph-data.js
        if save["first"] is not None:
            save["first"]()
        if save["error"] is not None:
            raise save["error"]

    def failing_save(first=None) -> OSError:
        save["error"], save["first"] = OSError(28, "No space left on device"), first
        return save["error"]

    def working_save() -> None:
        save["error"] = save["first"] = None

    galaxy._save_graph = fake_save
    state = lambda: ([note.path for note in galaxy.notes], set(galaxy.links), galaxy.rev)   # noqa: E731
    waiting = lambda: [file for file in notes_dir.rglob("*.taken*") if file.is_file()]     # noqa: E731

    def expect(step: str, error_type, action) -> BaseException | None:
        try:
            action()
        except error_type as error:
            return error
        except Exception as error:                  # noqa: BLE001 - the wrong error is a finding
            trouble.append(f"{step}: {type(error).__name__} instead of {error_type.__name__}")
            return error
        trouble.append(f"{step}: no {error_type.__name__}")
        return None

    def true_links(step: str) -> None:
        if galaxy.links != set(build.compute_links(galaxy.notes)):
            trouble.append(f"{step}: the links differ from a full rebuild")

    def named(step: str, error, place: str, cause) -> None:
        """The error says where the file is now, and it keeps the first error as its cause."""
        if error is not None and (place not in str(error) or error.__cause__ is not cause):
            trouble.append(f"{step}: the error does not name {place}, or hides the first error: {error!r}")

    def stuck_move_back(staged: Path, home: Path) -> None:
        raise PermissionError(13, "The file is in use")

    galaxy.birth_note("Card one.md", "# Card one\n\nFiled under [[Shared Idea]].\n")
    galaxy.birth_note("Card two.md", "# Card two\n\nFiled under [[Shared Idea]].\n")
    idea = "# Shared Idea\n\nWhat the cards are filed under.\n"     # its birth changes the link between the cards
    intruder = b"# Another program\n\nThis file came while the note was away.\n"
    edit = b"An editor added this.\n"

    # (a) The save fails after the note is in the index: the index is as before, and no file is left.
    for error in (OSError(28, "No space left on device"), RuntimeError("a bug in the index")):
        before, save["error"] = state(), error
        expect(f"(a) a birth whose save fails with {type(error).__name__}", type(error),
               lambda: galaxy.birth_note("captures/Shared Idea.md", idea))
        working_save()
        if state() != before:
            trouble.append(f"(a) after a {type(error).__name__} in the save, the index is not as before")
        if (notes_dir / "captures" / "Shared Idea.md").exists():
            trouble.append(f"(a) after a {type(error).__name__} in the save, the file is still there")
    # ...and when the file cannot be removed either, the error of the save is still the one raised.
    real_unlink = Path.unlink

    def stuck_unlink(self, *args, **kwargs):
        if self.name == "Shared Idea.md":
            raise PermissionError(13, "The file is in use")
        return real_unlink(self, *args, **kwargs)

    before, saved = state(), failing_save()
    Path.unlink = stuck_unlink
    try:
        error = expect("(a) a birth whose save fails and whose file cannot be removed", OSError,
                       lambda: galaxy.birth_note("captures/Shared Idea.md", idea))
    finally:
        Path.unlink = real_unlink
        working_save()
    if state() != before or error is not saved:
        trouble.append(f"(a) a file that cannot be removed changed the index or hid the first error: {error!r}")
    (notes_dir / "captures" / "Shared Idea.md").unlink(missing_ok=True)
    true_links("(a)")

    # (b) A take-back whose save fails: the index is as before, and the file is back with the same bytes.
    receipt = galaxy.birth_note("teardowns/Shared Idea.md", idea)
    path = notes_dir / receipt["file"]
    data, before = path.read_bytes(), state()
    failing_save()
    expect("(b) a take-back whose save fails", OSError, lambda: galaxy.unbirth(receipt))
    working_save()
    if state() != before or not path.is_file() or path.read_bytes() != data or waiting():
        trouble.append("(b) after a failed save, the index or the file is not as before, or a .taken file is left")

    # (c) The save fails AND the file cannot be put back: the index is as before, the error names the place and
    # keeps the save's error as its cause, and the bytes are still in the folder. Only this Galaxy, only this step.
    saved = failing_save()
    galaxy._move_back = stuck_move_back
    try:
        error = expect("(c) a take-back whose save and put-back fail", OSError, lambda: galaxy.unbirth(receipt))
    finally:
        del galaxy._move_back
        working_save()
    if state() != before:
        trouble.append("(c) after a failed save and put-back, the index is not as before")
    named("(c)", error, ".taken", saved)
    kept = [file for file in waiting() if file.read_bytes() == data]
    if len(kept) != 1:
        trouble.append("(c) the bytes of the note are not in one .taken file")
    for file in kept:                               # put the file back by hand, as a person would
        os.replace(file, path)

    # (g) An edit lands after the move aside (an editor that still holds the file). The bytes that would be
    # deleted are the ones checked, so the file goes back with the edit and the index is not touched.
    real_replace = os.replace

    def editing_replace(source, destination, *args, **kwargs):
        real_replace(source, destination, *args, **kwargs)
        if ".taken" in Path(destination).name:
            with open(destination, "ab") as handle:
                handle.write(edit)

    os.replace = editing_replace
    try:
        expect("(g) a take-back of a file edited while it waits aside", ValueError, lambda: galaxy.unbirth(receipt))
    finally:
        os.replace = real_replace
    if state() != before or not path.is_file() or path.read_bytes() != data + edit or waiting():
        trouble.append("(g) a file edited while it waited aside did not go back with the edit")
    path.write_bytes(data)

    # (g) The same, and another program also made a new file at the old place: the new file is not written over,
    # the error names the .taken place, the index is not touched.
    def crowding_replace(source, destination, *args, **kwargs):
        editing_replace(source, destination, *args, **kwargs)
        if ".taken" in Path(destination).name:
            Path(source).write_bytes(intruder)

    os.replace = crowding_replace
    try:
        error = expect("(g) a changed file whose old place was taken", OSError, lambda: galaxy.unbirth(receipt))
    finally:
        os.replace = real_replace
    if state() != before or not path.is_file() or path.read_bytes() != intruder:
        trouble.append("(g) a new file at the old place was written over, or the index changed")
    if error is not None and ".taken" not in str(error):
        trouble.append(f"(g) the error does not name the .taken place: {error!r}")
    staged = waiting()                              # before the reset: the note with its edit must still be there
    if len(staged) != 1 or staged[0].read_bytes() != data + edit:
        trouble.append(f"(g) the edited note is not waiting in ONE .taken file with its edit ({len(staged)} staged)")
    path.unlink()
    for file in staged:
        os.replace(file, path)
    path.write_bytes(data)

    # (g) The old place is taken while the note waits aside, and the save fails: the new file is not written over,
    # the note stays aside, the error names that place, the index is as before.
    saved = failing_save(first=lambda: path.write_bytes(intruder))
    error = expect("(g) a take-back whose old place was taken", OSError, lambda: galaxy.unbirth(receipt))
    working_save()
    if state() != before or not path.is_file() or path.read_bytes() != intruder:
        trouble.append("(g) a new file at the old place was written over, or the index is not as before")
    named("(g)", error, ".taken", saved)
    kept = [file for file in waiting() if file.read_bytes() == data]
    if len(kept) != 1:
        trouble.append("(g) the note is not waiting in one .taken file")
    path.unlink()
    for file in kept:
        os.replace(file, path)

    # (f) A clean take-back: the note is gone, no .taken file is left, the links are a full rebuild's.
    galaxy.unbirth(receipt)
    if path.exists() or waiting() or receipt["file"] in galaxy._born:
        trouble.append("(f) after a clean take-back, the file, a .taken file or its fingerprint is left")
    if receipt["title"] in [note.label for note in galaxy.notes]:
        trouble.append("(f) after a clean take-back, the note is still in the index")
    true_links("(f)")

    # (d) A forget whose save fails: the index is as before, the file is back, the trash is empty.
    capture = galaxy.remember("the till was short on Tuesday")
    path = notes_dir / capture["file"]
    trash = notes_dir / server.CAPTURES_DIR / server.TRASH_DIR
    data, before = path.read_bytes(), state()
    failing_save()
    expect("(d) a forget whose save fails", OSError,
           lambda: galaxy.forget_last(capture["index"], capture["title"], capture["build"], capture["born"]))
    working_save()
    if state() != before or not path.is_file() or path.read_bytes() != data or any(trash.glob("*")):
        trouble.append("(d) after a failed save, the index, the file or the trash is not as before")

    # (h) A forget whose save fails while another program makes a new file at the old place: the new file is not
    # written over, the note stays in the trash, the error names it, the index is as before.
    saved = failing_save(first=lambda: path.write_bytes(intruder))
    error = expect("(h) a forget whose old place was taken", OSError,
                   lambda: galaxy.forget_last(capture["index"], capture["title"], capture["build"], capture["born"]))
    working_save()
    trashed = list(trash.glob("*"))
    if state() != before or not path.is_file() or path.read_bytes() != intruder:
        trouble.append("(h) a new file at the old place was written over, or the index is not as before")
    if len(trashed) != 1 or trashed[0].read_bytes() != data:
        trouble.append("(h) the note is not in the trash")
    else:
        named("(h)", error, trashed[0].name, saved)
        path.unlink()
        os.replace(trashed[0], path)
    galaxy.forget_last(capture["index"], capture["title"], capture["build"], capture["born"])
    true_links("(h)")

    # (e) The file was changed after the birth (an editor plug-in): it stays, with the change, and so does the
    # note. A note this run did not make (no fingerprint) stays too, and is not moved.
    receipt = galaxy.birth_note("teardowns/Teardown edited.md", "# Teardown edited\n\nJarviz wrote this.\n")
    path = notes_dir / receipt["file"]
    path.write_bytes(path.read_bytes() + edit)
    edited, before = path.read_bytes(), state()
    expect("(e) a take-back of a changed file", ValueError, lambda: galaxy.unbirth(receipt))
    if state() != before or not path.is_file() or path.read_bytes() != edited or waiting():
        trouble.append("(e) a changed file or its note did not stay as it was")
    galaxy._born.pop(receipt["file"], None)
    path.write_bytes(b"# Teardown edited\n\nJarviz wrote this.\n")
    expect("(e) a take-back of a note with no fingerprint", ValueError, lambda: galaxy.unbirth(receipt))
    if state() != before or not path.is_file() or waiting():
        trouble.append("(e) a note with no fingerprint did not stay")
    true_links("(e)")

    # (i) The receipt of a capture carries the build id of the galaxy at the moment of the birth, also when a new
    # index comes in right after it (the reply's graph numbers are read later, so they would name the new one).
    # A forget with the receipt of another galaxy (another notes folder, or build.py) may name another note under
    # the same number and title: nothing moves. The receipt of THIS galaxy works.
    galaxy.build_id = "this-galaxy"
    real_remember, real_galaxy = galaxy.remember, server.GALAXY

    def remember_then_reload(*args, **kwargs) -> dict:
        receipt = real_remember(*args, **kwargs)
        galaxy.build_id = "next-galaxy"             # a reload right after the birth
        return receipt

    galaxy.remember, server.GALAXY = remember_then_reload, galaxy
    try:
        _, body = server.do_remember("the till was short on Friday", "Remember that the till was short on Friday",
                                     server.Session())
    finally:
        del galaxy.remember
        server.GALAXY = real_galaxy
    capture = (body.get("action") or {}).get("receipt") or {}
    if capture.get("build") != "this-galaxy" or body.get("graph", {}).get("build_id") != "next-galaxy":
        trouble.append(f"(i) the receipt does not hold the build of the birth: {capture.get('build')!r}")
    path, before, trashed = notes_dir / capture["file"], state(), sorted(trash.glob("*"))
    error = expect("(i) a forget after the galaxy was built again", ValueError,
                   lambda: galaxy.forget_last(capture["index"], capture["title"], capture["build"], capture["born"]))
    if state() != before or not path.is_file() or sorted(trash.glob("*")) != trashed:
        trouble.append("(i) a forget with another galaxy's receipt moved something")
    if error is not None and "built again" not in str(error):
        trouble.append(f"(i) the reason does not say that the galaxy was built again: {error}")
    galaxy.build_id = "this-galaxy"                 # the galaxy of the receipt again
    gone = galaxy.forget_last(capture["index"], capture["title"], capture["build"], capture["born"])
    if path.exists() or gone.get("title") != capture["title"]:
        trouble.append("(i) a forget with this galaxy's receipt did not work")
    true_links("(i)")

    # (j) A receipt names ONE creation of a note. A first capture is forgotten; a second one with the same first
    # words but another end lands on the same title, path and index. The receipt of the first moves nothing; the
    # second's own receipt works. (Two captures with exactly the same bytes count as the same note.)
    first = galaxy.remember("the till was short on Monday morning before we opened")
    galaxy.forget_last(first["index"], first["title"], first["build"], first["born"])
    second = galaxy.remember("the till was short on Monday morning after the lunch rush")
    if (second["title"], second["file"], second["index"]) != (first["title"], first["file"], first["index"]) \
            or second["born"] == first["born"]:
        trouble.append("(j) the second capture did not land on the first one's title, path and index")
    path, before, trashed = notes_dir / second["file"], state(), sorted(trash.glob("*"))
    kept_bytes = path.read_bytes()
    error = expect("(j) a forget with the receipt of the first capture", ValueError,
                   lambda: galaxy.forget_last(first["index"], first["title"], first["build"], first["born"]))
    if state() != before or path.read_bytes() != kept_bytes or sorted(trash.glob("*")) != trashed:
        trouble.append("(j) the receipt of the first capture moved the second")
    if error is not None and "not the note this receipt was made for" not in str(error):
        trouble.append(f"(j) the reason does not say that it is another note: {error}")
    galaxy.forget_last(second["index"], second["title"], second["build"], second["born"])
    if path.exists():
        trouble.append("(j) the second capture's own receipt did not forget it")
    true_links("(j)")


def check_notes_dir_follows_config(report: Report) -> None:
    """After "set notes_dir, run build.py", the running server loads the folder that config.json names NOW, when
    that is a folder. Else the current folder stays. Pure: a test Galaxy, a fake build.build and a fake
    load_config. For the length of the check, build.write_graph is a recorder: nothing may call it."""
    name = "A new index is loaded from the notes folder that config.json names now"
    trouble: list[str] = []
    real_build, real_load, real_write = build.build, server.load_config, build.write_graph
    writes, loaded = [], []

    def fake_build(notes_dir, graph_path=None):         # the real one writes viewer/graph-data.js
        loaded.append(Path(notes_dir))
        return [], [], "test"

    build.write_graph = lambda *args, **kwargs: writes.append(args)
    build.build = fake_build
    try:
        with tempfile.TemporaryDirectory() as old, tempfile.TemporaryDirectory() as new:
            old_dir, new_dir = Path(old).resolve(), Path(new).resolve()
            cases = (("the folder that config.json names now", {"notes_dir": str(new_dir)}, new_dir),
                     ("a folder that is not there", {"notes_dir": str(new_dir / "missing")}, old_dir),
                     ("a config.json that cannot be read", {"notes_dir": str(new_dir), "_error": "A typing mistake."}, old_dir),
                     # Path.expanduser raises RuntimeError for an unknown home account ("~nouser/x" on a Mac or Linux)
                     ("a folder whose home cannot be worked out", {"notes_dir": "~nouser/x"}, old_dir))
            for what, config, wanted in cases:
                galaxy = server.Galaxy()
                galaxy.notes_dir, galaxy._stamp = old_dir, -1      # -1: "graph-data.js changed" (a real stamp is never -1)
                server.load_config = lambda config=config: {**server.DEFAULT_CONFIG, **config}
                loaded.clear()
                real_resolve = build.resolve_notes_dir
                if config["notes_dir"].startswith("~nouser"):      # Windows works out "~nouser": make it raise as POSIX does
                    def unknown_home(value):
                        raise RuntimeError("Could not determine home directory.")
                    build.resolve_notes_dir = unknown_home
                try:
                    galaxy.refresh_if_rebuilt()
                finally:
                    build.resolve_notes_dir = real_resolve
                if loaded != [wanted] or galaxy.notes_dir != wanted:
                    trouble.append(f"{what}: it loaded {[p.name for p in loaded]}, not {wanted.name}")
    except Exception as error:                          # noqa: BLE001 - a check that stops says so, it never crashes preflight
        trouble.append(f"the check stopped: {type(error).__name__}: {error}")
    finally:
        build.build, server.load_config, build.write_graph = real_build, real_load, real_write
    if writes:
        trouble.append(f"build.write_graph was called {len(writes)} times: nothing may write here")
    report.add(FAIL if trouble else PASS, name, "; ".join(trouble) or
               "the folder named now; the old folder when the named one is not there, cannot be worked out, or "
               "config.json cannot be read")


def check_brain_choice_saved(report: Report) -> None:
    """A change of brain changes only the text of the top-level "brain" entry of config.json: every other character
    stays. A file that cannot be changed safely stays as it was. Pure: a TEMPORARY config.json, never the real one."""
    name = "A change of brain changes only the \"brain\" text in config.json"
    trouble: list[str] = []
    cases = {
        "odd spacing and blank lines": ('{\n\n   "brain"  :   "anthropic" ,\n\n\t"model": "x"\n}\n',
                                        '{\n\n   "brain"  :   "deepseek" ,\n\n\t"model": "x"\n}\n'),
        "the word brain in other values": ('{"notes_dir": "brain \\"brain\\": x", "brain": "openai", "extra": {"brain": "venice"}}',
                                           '{"notes_dir": "brain \\"brain\\": x", "brain": "deepseek", "extra": {"brain": "venice"}}'),
        "no brain line": ('{\n  "model": "x",\n  "notes_dir": "notes"\n}\n',
                          '{\n  "brain": "deepseek",\n  "model": "x",\n  "notes_dir": "notes"\n}\n'),
        "CRLF line ends": ('{\r\n  "brain": "anthropic",\r\n\r\n  "model": "x"\r\n}\r\n',
                           '{\r\n  "brain": "deepseek",\r\n\r\n  "model": "x"\r\n}\r\n'),
        "two brain lines": ('{"brain": "openai", "model": "x", "brain": "venice"}',
                            '{"brain": "deepseek", "model": "x", "brain": "deepseek"}'),
        "an empty object": ("{}", '{"brain": "deepseek"}'),
        "a byte order mark": ('﻿{"brain": "openai"}', '﻿{"brain": "deepseek"}'),
    }
    broken = {"a typing mistake": '{"brain": "openai",, "model": "x"}', "a list, not an object": '["brain"]'}
    real_path = server.CONFIG_PATH
    try:
        with tempfile.TemporaryDirectory() as folder:
            server.CONFIG_PATH = Path(folder) / "config.json"
            if server.CONFIG_PATH.resolve() == real_path.resolve() or server.CONFIG_PATH.parent == real_path.parent:
                raise RuntimeError("the test file is the real config.json")
            for what, (before, after) in cases.items():
                server.CONFIG_PATH.write_bytes(before.encode("utf-8"))
                try:
                    server.save_brain_choice("deepseek")
                except (OSError, ValueError) as error:
                    trouble.append(f"{what}: it raised {error}")
                    continue
                if server.CONFIG_PATH.read_bytes() != after.encode("utf-8"):
                    trouble.append(f"{what}: the file became {server.CONFIG_PATH.read_bytes()!r}")
            for what, text in broken.items():
                server.CONFIG_PATH.write_bytes(text.encode("utf-8"))
                try:
                    server.save_brain_choice("deepseek")
                    trouble.append(f"{what}: no error")
                except ValueError:
                    pass
                if server.CONFIG_PATH.read_bytes() != text.encode("utf-8"):
                    trouble.append(f"{what}: the file was changed")
            left = sorted(path.name for path in Path(folder).iterdir() if path.name != "config.json")
            if left:
                trouble.append(f"files left behind: {left}")
            _staging_steps(Path(folder), trouble)
    except Exception as error:                          # noqa: BLE001 - a check that stops says so, it never crashes preflight
        trouble.append(f"the check stopped: {type(error).__name__}: {error}")
    finally:
        server.CONFIG_PATH = real_path
    report.add(FAIL if trouble else PASS, name, "; ".join(trouble) or
               f"{len(cases)} files kept every other character; {len(broken)} broken files were left as they were; "
               "the staging copy (it holds the keys) never writes over a file and never stays; the start-up sweep "
               "removes only a crash's staging copy")


def _staging_steps(folder: Path, trouble: list[str]) -> None:
    """The staging copy of config.json holds the keys. Run inside check_brain_choice_saved, with server.CONFIG_PATH
    in a temporary `folder`: (a) a save whose replace fails leaves no staging copy; (b) a staging file of that name
    that is there already is never written over (the save fails, both files stay as they were); (c) the start-up sweep
    removes a crash's staging copy, and only that: not config.json.bak, config.json.x.tmp, or a folder of that name."""
    config = server.CONFIG_PATH
    original = b'{"brain": "openai", "openai_api_key": "test-only"}'
    staging = folder / f"{config.name}.{os.getpid()}.tmp"
    config.write_bytes(original)
    real_replace = os.replace

    def failing_replace(source, destination, *args, **kwargs):
        if Path(destination) == config:
            raise PermissionError(13, "The file is in use")
        return real_replace(source, destination, *args, **kwargs)

    os.replace = failing_replace
    try:
        server.save_brain_choice("deepseek")
        trouble.append("(a) a save whose replace failed did not raise")
    except OSError:
        pass
    finally:
        os.replace = real_replace
    if config.read_bytes() != original or staging.exists():
        trouble.append("(a) a save whose replace failed changed config.json or left its staging copy")
    staging.write_bytes(b"not ours")
    try:
        server.save_brain_choice("deepseek")
        trouble.append("(b) a save wrote over a staging file that was there already")
    except OSError:
        pass
    if staging.read_bytes() != b"not ours" or config.read_bytes() != original:
        trouble.append("(b) a staging file that was there already, or config.json, was changed")
    keep = ["config.json.bak", "config.json.x.tmp", "config.json.12.tmp.old", "config.json.34.tmp"]   # the last: a folder
    for name in keep[:3]:
        (folder / name).write_bytes(b"keep")
    (folder / keep[3]).mkdir()
    (folder / "config.json.56.tmp").write_bytes(b"a crash's staging copy")
    removed = sorted(server.sweep_config_staging())
    if removed != sorted([staging.name, "config.json.56.tmp"]) or (folder / "config.json.56.tmp").exists() \
            or staging.exists() or not all((folder / name).exists() for name in keep) or config.read_bytes() != original:
        trouble.append(f"(c) the start-up sweep removed {removed}: only the staging copies may go")
    for name in keep[:3]:
        (folder / name).unlink()
    (folder / keep[3]).rmdir()


def check_teardown_one_folder(report: Report) -> None:
    """A teardown writes its picture and its notes into ONE notes folder. The galaxy moves to another folder between
    the picture and the first note (build.py ran for another notes_dir): the birth refuses, the job fails, and no
    picture and no note is left in either folder. Pure: a test Galaxy over two temporary folders (a fake save), the
    real door (server._teardown_birth) and a real Bay with a made-up brain. build.write_graph is a recorder."""
    name = "A teardown writes its picture and its notes into one notes folder, or into none"
    trouble: list[str] = []
    real_galaxy, real_write, writes = server.GALAXY, build.write_graph, []
    reply = ("LABEL: Teardown · a.test\nSPOKEN: It is a plain page, sir.\nSUMMARY: A page.\n" + teardown.MARK
             + "\n> Verdict.\n\n" + "\n\n".join(f"## {section}\nText." for section in teardown.NOTE_SECTIONS))
    build.write_graph = lambda *args, **kwargs: writes.append(args)
    try:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            first, second = root / "first notes", root / "second notes"
            for notes in (first, second):
                notes.mkdir()
                (notes / "Kept.md").write_text("# Kept\n\nA note of the user's own.\n", encoding="utf-8")
            galaxy, looks = server.Galaxy(), []

            def move_to(notes: Path, build_id: str) -> None:       # as a load of that folder leaves the index
                galaxy.notes_dir, galaxy.build_id, galaxy.rev = notes, build_id, 0
                galaxy.notes[:] = [build.make_note(path, notes) for path in build.note_files(notes)]
                galaxy.links.clear()

            def look_for_rebuild() -> None:     # the first look (the job binds its folder) finds nothing new; the
                looks.append(1)                 # second (the first birth) finds build.py ran for the other folder
                if len(looks) == 2:
                    move_to(second, "second")

            move_to(first, "first")
            galaxy._save_graph, galaxy.refresh_if_rebuilt, server.GALAXY = (lambda: None), look_for_rebuild, galaxy
            bay = teardown.Bay(notes_dir=lambda: galaxy.notes_dir, notes_folder=galaxy.current_folder,
                               ask=lambda system, message: reply, birth=server._teardown_birth, unbirth=galaxy.unbirth,
                               note_titles=galaxy.titles, writer_name=lambda: "test", voice="plain",
                               plain_line="{host} is done.")
            job, code = teardown.Job(teardown.Site("https://a.test/", "a.test"), "how is it built", True), None
            try:
                bay._write(job, {"snapshot": teardown.PNG_SIGNATURE + b"picture"})
            except teardown.TeardownError as error:
                code = error.code
            left = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
            if code != "notes_moved" or job.births or left != ["first notes/Kept.md", "second notes/Kept.md"]:
                trouble.append(f"the job ended as {code!r}, births {job.births}, files left {left}")
            if len(looks) != 2:
                trouble.append(f"the galaxy looked for a rebuild {len(looks)} times, not twice (bind, first birth)")
    except Exception as error:                          # noqa: BLE001 - a check that stops says so, it never crashes preflight
        trouble.append(f"the check stopped: {type(error).__name__}: {error}")
    finally:
        server.GALAXY, build.write_graph = real_galaxy, real_write
    if writes:
        trouble.append(f"build.write_graph was called {len(writes)} times: nothing may write here")
    report.add(FAIL if trouble else PASS, name, "; ".join(trouble) or
               "the folder moved between the picture and the first note: the job failed, no picture and no note in "
               "either folder")


def check_build_survives(report: Report) -> None:
    """build.py: one entry that cannot be looked at (Path.is_file can raise on Python 3.13 and older) is skipped and
    said, and the other notes are still found; and after the graph is written, a "notes_dir" in config.json that
    cannot be worked out (Path.expanduser raises RuntimeError) does not fail the build. Pure: a temporary folder, and
    build.write_graph is a recorder (the real one writes viewer/graph-data.js)."""
    name = "build.py goes on past an entry it cannot look at, and past a notes_dir it cannot work out"
    trouble: list[str] = []
    real_is_file, real_configured, real_write, writes = Path.is_file, build.configured_notes_dir, build.write_graph, []

    def stuck_is_file(self, *args, **kwargs):
        if self.name == "Locked.md":
            raise PermissionError(13, "Access is denied")
        return real_is_file(self, *args, **kwargs)

    def unknown_home(*args, **kwargs):
        raise RuntimeError("Could not determine home directory.")

    build.write_graph = lambda *args, **kwargs: writes.append(args)
    try:
        with tempfile.TemporaryDirectory() as folder:
            notes = Path(folder).resolve()
            for title in ("Open", "Locked"):
                (notes / f"{title}.md").write_text(f"# {title}\n\nA note.\n", encoding="utf-8")
            said = io.StringIO()
            Path.is_file = stuck_is_file
            try:
                with contextlib.redirect_stderr(said):
                    found = [path.name for path in build.note_files(notes)]
            finally:
                Path.is_file = real_is_file
            if found != ["Open.md"] or "Locked.md" not in said.getvalue():
                trouble.append(f"an entry that cannot be looked at: found {found}, said {said.getvalue().strip()!r}")
            build.configured_notes_dir = unknown_home
            with contextlib.redirect_stdout(io.StringIO()):
                status = build.main(["build.py", str(notes)])
            if status != 0 or len(writes) != 1:
                trouble.append(f"with a notes_dir that cannot be worked out, the build gave {status} and wrote {len(writes)} times")
    except Exception as error:                          # noqa: BLE001 - a check that stops says so, it never crashes preflight
        trouble.append(f"the check stopped: {type(error).__name__}: {error}")
    finally:
        Path.is_file, build.configured_notes_dir, build.write_graph = real_is_file, real_configured, real_write
    report.add(FAIL if trouble else PASS, name, "; ".join(trouble) or
               "the entry was skipped and said, the other note found; the build kept its exit status 0")


class _FakeBay:
    """For the pure checks: no thread, no browser. start() can refuse, end the job at once, or keep it running."""

    def __init__(self, refuse: Exception | None = None, at_once: bool = False, ends_as: str = "cancelled", during=None):
        self.refuse, self.at_once, self.ends_as = refuse, at_once, ends_as
        self.job, self.on_done, self.asked = None, None, []
        self.during = during                # runs once, inside the first start: another start that overlaps it

    def start(self, site, question, headless=False, on_done=None):
        during, self.during = self.during, None
        if during is not None:
            during()
        if self.refuse is not None:
            raise self.refuse
        self.job, self.on_done = teardown.Job(site, question, headless), on_done
        if self.at_once:
            self.end("failed")
        return self.job

    def end(self, state: str) -> None:
        self.job.error = None if state == "cancelled" else {"code": "site_silent", "detail": "The site did not answer"}
        self.job.state = state
        if self.on_done:
            self.on_done(self.job)

    def cancel(self, job_id=None):
        self.asked.append(job_id)
        job = self.job
        if job is None or job.state != "running" or (job_id is not None and job_id != job.id):
            return None
        job.state = self.ends_as                        # it ends at once: the route does not wait
        job.error = {"code": "site_silent", "detail": "The site did not answer"} if self.ends_as == "failed" else None
        return job


def check_teardown_receipt_turn(report: Report) -> None:
    """A teardown receipt goes on the turn that started the job (a question asked meanwhile does not get it), and
    only while that turn is in the history. A start that BAY refuses leaves no turn: "busy" says busy, any other
    refusal says the kit is not ready. Two refused starts that overlap leave the session exactly as it was, also with
    a full history: a start is only a draft until BAY accepted the job. Pure: a fake bay (no thread, no browser) and
    test sessions."""
    name = "A teardown receipt goes on the turn that started it, and a refused start leaves no turn"
    trouble: list[str] = []
    real_bay, real_diag = server.BAY, teardown.diag
    site = "localhost:1"            # the user's own localhost: no lookup. The fake bay opens nothing anyway.

    def receipts(session) -> list[list[str]]:
        return [list(turn[2]) for turn in session.turns]

    def start(session, bay) -> tuple[int, dict]:
        server.BAY = bay
        if server.BAY is not bay:
            raise RuntimeError("the fake bay is not in place")
        return server.start_teardown(site, "how is it built", session, f"Tear down {site}", headless=True)

    teardown.diag = lambda: {"problems": []}
    try:
        session = server.Session()          # (a) a job that fails at once: both receipts on its own turn
        session.record("Good morning", "Good morning, sir.", ["morning"], [])
        status, _ = start(session, _FakeBay(at_once=True))
        got = receipts(session)
        if status != 200 or len(got) != 2 or got[0] or len(got[1]) != 2 or "did not finish" not in got[1][1]:
            trouble.append(f"(a) a job that failed at once: {got}")
        session, bay = server.Session(), _FakeBay()     # (b) a question asked while the job runs does not get it
        start(session, bay)
        session.record("What is the time?", "Noon, sir.", ["time"], [])
        bay.end("failed")
        got = receipts(session)
        if len(got) != 2 or len(got[0]) != 2 or "did not finish" not in got[0][1] or got[1]:
            trouble.append(f"(b) a question asked meanwhile: {got}")
        session, bay = server.Session(), _FakeBay()     # (c) the turn left the history: the receipt is dropped
        start(session, bay)
        for number in range(server.HISTORY_TURNS):
            session.record(f"Question {number}", "An answer.", [], [])
        bay.end("failed")
        if any("did not finish" in line for lines in receipts(session) for line in lines):
            trouble.append("(c) a turn that left the history still got a receipt")
        for refusal, status_wanted, error_wanted in ((teardown.TeardownError("busy", "A teardown is already running."), 409,
                                                      "teardown_busy"),
                                                     (teardown.TeardownError("not_started", "The teardown could not start."),
                                                      503, "teardown_not_started")):
            session = server.Session()      # (d) a refused start leaves the session as it was, even a full one
            for number in range(server.HISTORY_TURNS):
                session.record(f"Question {number}", "An answer.", ["coffee"], [3], ids={3: ("notes", "Coffee.md", None)})
            before = (receipts(session), [turn[0] for turn in session.turns], session.last_tokens, session.last_sources)
            status, body = start(session, _FakeBay(refuse=refusal))
            after = (receipts(session), [turn[0] for turn in session.turns], session.last_tokens, session.last_sources)
            if (status, body.get("error")) != (status_wanted, error_wanted) or after != before:
                trouble.append(f"(d) a start refused as {refusal.code}: HTTP {status} {body.get('error')}, "
                               f"session {'as before' if after == before else 'changed'}")
            wording = server.LINES["teardown_busy" if refusal.code == "busy" else "teardown_not_ready"].split("{")[0]
            if not str(body.get("answer", "")).startswith(wording):
                trouble.append(f"(d) a start refused as {refusal.code} says {body.get('answer')!r}")
            # (e) Two starts that overlap (the second comes while the first waits for BAY), and both are refused.
            inner: list = []
            bay = _FakeBay(refuse=refusal)
            bay.during = lambda bay=bay: inner.append(start(session, bay))
            status, body = start(session, bay)
            after = (receipts(session), [turn[0] for turn in session.turns], session.last_tokens, session.last_sources)
            if after != before or [answer[0] for answer in inner] != [status_wanted] or status != status_wanted:
                trouble.append(f"(e) two overlapping starts refused as {refusal.code}: HTTP {status} and "
                               f"{[answer[0] for answer in inner]}, session {'as before' if after == before else 'changed'}")
    except Exception as error:                          # noqa: BLE001 - a check that stops says so, it never crashes preflight
        trouble.append(f"the check stopped: {type(error).__name__}: {error}")
    finally:
        server.BAY, teardown.diag = real_bay, real_diag
    report.add(FAIL if trouble else PASS, name, "; ".join(trouble) or
               "a job that fails at once, a question asked meanwhile, a turn that left the history, two refusals, "
               "two refused starts that overlap")


def check_teardown_cancel(report: Report) -> None:
    """/teardown/cancel with a job id cancels only that job; only a MISSING "job" means the job that runs ("cancel the
    teardown"). A "job" that is there but no non-empty text (null, a number, "", a list) stops nothing. A job that
    FAILED while the cancel waited gets the failed line of /teardown/status. Every answer about a job carries its id
    ("job_id"); an answer about no job does not. Pure: the route with a fake bay (no thread, no browser), and for
    "stopping" a made-up clock in the route."""
    name = "A cancel that names a job stops only that job, and a failed job says why"
    trouble: list[str] = []
    real_bay, real_time = server.BAY, server.time
    site = teardown.Site("http://localhost:1/", "localhost:1")
    try:
        for payload, ends_as, wanted in (({"job": "preflight-no-such-job"}, "cancelled", "no_such_job"),
                                         ({"job": 5}, "cancelled", "no_such_job"),
                                         ({"job": ""}, "cancelled", "no_such_job"),
                                         ({"job": None}, "cancelled", "no_such_job"),
                                         ({"job": []}, "cancelled", "no_such_job"),
                                         ({"job": {"id": "x"}}, "cancelled", "no_such_job"),
                                         ({}, "failed", "failed"),
                                         ({}, "cancelled", "cancelled"),
                                         ("own", "cancelled", "cancelled"),
                                         ("own", "done", "too_late"),
                                         ("own", "running", "stopping")):
            server.BAY = bay = _FakeBay(ends_as=ends_as)
            bay.job = teardown.Job(site, "q", True)
            sent = {"job": bay.job.id} if payload == "own" else payload
            if ends_as == "running":            # the job does not stop: the route's 12 s wait runs on a made-up clock
                ticks = iter(range(10_000))
                server.time = type("Clock", (), {"monotonic": staticmethod(lambda: float(next(ticks))),
                                                 "sleep": staticmethod(lambda seconds: None)})
            try:
                _, body = server.teardown_cancel_route(sent)
            finally:
                server.time = real_time
            got = body.get("error") or (body.get("action") or {}).get("type")
            got = "cancelled" if got == "teardown_cancel" else got
            if got == "cancelled" and body.get("answer") == server.LINES["teardown_stopping"]:
                got = "stopping"
            still = bay.job.state == "running"
            if got != wanted or (wanted in ("no_such_job", "stopping")) != still:
                trouble.append(f"{sent}: {got}, the job {'still runs' if still else 'stopped'}")
            if wanted == "failed" and body.get("answer") != server.teardown_line(bay.job.view()):
                trouble.append(f"a failed job: {body.get('answer')!r} is not the line of /teardown/status")
            about_a_job = wanted not in ("no_such_job", "none_running")
            if (body.get("job_id") == bay.job.id) != about_a_job or ("job_id" in body) != about_a_job:
                trouble.append(f"{sent}: the answer ({wanted}) has job_id {body.get('job_id')!r}")
        server.BAY = _FakeBay()
        body = server.teardown_cancel_route({})[1]
        if body.get("error") != "none_running" or "job_id" in body:
            trouble.append("no job: it does not say that none is running, or it names a job")
    except Exception as error:                          # noqa: BLE001 - a check that stops says so, it never crashes preflight
        trouble.append(f"the check stopped: {type(error).__name__}: {error}")
    finally:
        server.BAY, server.time = real_bay, real_time
    report.add(FAIL if trouble else PASS, name, "; ".join(trouble) or
               "another id, a bad id, an empty id and null stop nothing; the own id and a missing id cancel; failed, "
               "done and stopping are said; each answer about a job names it (job_id)")


def check_signature_once(report: Report) -> None:
    """A signature is claimed once, and never when its time window is over: a body that comes late cannot make an
    old signature work, also not after its first claim was pruned. The time is read inside the lock. Pure:
    claim_signature with made-up signatures and a made-up clock, on nonces that only this check uses (they are
    removed afterwards)."""
    name = "A signature is claimed once, and never after its time window"
    trouble: list[str] = []
    window, stamp = server.SIGN_WINDOW_S, 2_000_000_000
    nonces = [f"preflight-once-{secrets.token_hex(6)}" for _ in range(4)]
    old, fresh, expiring, ordered = (f"{stamp}.{nonce}.x.y" for nonce in nonces)

    def at(seconds: float):                         # a made-up clock that always says stamp + seconds
        return lambda: stamp + seconds

    try:
        if server.claim_signature(old, at(window + 5)) or server.claim_signature(old, at(window + 6)):
            trouble.append("a stamp older than the window was claimed")
        if not server.claim_signature(fresh, at(1)):
            trouble.append("a fresh signature was not claimed the first time")
        if server.claim_signature(fresh, at(2)):
            trouble.append("a fresh signature was claimed twice")
        if not server.claim_signature(expiring, at(window - 1)):
            trouble.append("a signature at the end of its window was not claimed the first time")
        if server.claim_signature(expiring, at(window + 1)):
            trouble.append("a signature that expired between two claims was claimed again")
        if server.claim_signature(expiring, at(10 * window)):     # its first claim is pruned by now
            trouble.append("an expired signature was claimed again after its first claim was pruned")
        # The time is read while the lock is held: the time that is checked is the time of the claim.
        held: list[bool] = []

        def watched_clock() -> float:
            held.append(server.USED_LOCK.locked())
            return stamp + 1

        if not server.claim_signature(ordered, watched_clock) or held != [True]:
            trouble.append(f"the time of a claim is not read inside the lock (lock held at each read: {held})")
    finally:
        with server.USED_LOCK:
            for nonce in nonces:
                server.USED_SIGNATURES.pop(nonce, None)
    report.add(FAIL if trouble else PASS, name, "; ".join(trouble) or
               "an old stamp twice, a fresh one twice, one that expires between two claims and after the prune; "
               "the time is read inside the lock")


def check_sources_stay_true(report: Report) -> None:
    """The sources of a reply are the notes the brain was SHOWN, by their identity (note_id: the notes folder, the
    path, the creation), also when a new index comes in: (1) two notes with the same title in two folders that swap
    places are not taken for each other, in the reply and in the session; (2) a reload that comes between the
    search and the render does not change what the brain is sent; (3) the sources and the graph numbers of one
    reply come from one state; (4) so do the results and the numbers of /search; (5) the same relative path in
    another notes folder is another note; (6) a capture that is forgotten and made again with other text at the
    same path and number is another note. Pure: a test Galaxy over a temporary folder (refresh off, a fake save), a
    fake brain, and a thread that tries a reload at the worst moment. For the length of the check,
    build.write_graph is a recorder: nothing may call it."""
    name = "The sources of a reply are the notes the brain was shown, also across a new index"
    trouble: list[str] = []
    real_galaxy, real_ask, real_write, writes = server.GALAXY, server.ask_model, build.write_graph, []
    session_id = "preflight-sources-" + secrets.token_hex(3)
    build.write_graph = lambda *args, **kwargs: writes.append(args)
    try:
        with tempfile.TemporaryDirectory() as folder:
            notes_dir = Path(folder).resolve()

            def make(relative: str, text: str) -> build.Note:
                path = notes_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"# {path.stem}\n\n{text}\n", encoding="utf-8")
                return build.make_note(path, notes_dir)

            foo_a, foo_b = make("shop a/Foo.md", "Foo is the roaster."), make("shop b/Foo.md", "Foo is the grinder.")
            tea = make("shop c/Tea.md", "Tea is sold by the pot.")
            galaxy = server.Galaxy()
            galaxy.notes_dir, galaxy.refresh_if_rebuilt, galaxy._save_graph = notes_dir, lambda: None, lambda: None
            server.GALAXY = galaxy
            sent: list[str] = []

            def brain(during=None):
                def ask(route, messages, system=server.SYSTEM_PROMPT):
                    sent.append(messages[-1]["content"])
                    if during:
                        during()
                    return "Foo is the roaster, sir.\nSOURCES: 0"
                return ask

            unverified: list[str] = []

            def reload_soon(notes: list, build_id: str) -> threading.Thread:
                """Another request that loads a new index: it waits for the lock, as a real one does. The request side
                (it holds the lock here) goes on only once the worker is at the lock: so the reload surely waits for
                the snapshot, and is not just late. A worker that does not get there in time: not verified."""
                trying = threading.Event()

                def run() -> None:
                    trying.set()        # right before the lock: from here on it waits for the snapshot to end
                    with galaxy.lock:
                        galaxy.notes[:] = notes
                        galaxy.build_id = build_id
                worker = threading.Thread(target=run, daemon=True)
                worker.start()
                if not trying.wait(5.0):
                    unverified.append(build_id)
                return worker

            def ask_chat() -> dict:
                return server.chat_route({"message": "What is Foo?", "session": session_id})[1]

            # (1) Foo.md of shop a and of shop b swap places while the brain thinks: index 0 is another note now.
            galaxy.notes[:], galaxy.build_id = [foo_a, foo_b], "one"
            server.ask_model = brain(during=galaxy.notes.reverse)
            body = ask_chat()
            session = server.get_session(session_id)
            if body.get("nodes") != [] or session.last_sources != []:
                trouble.append(f"(1) a note with the same title in another folder was taken as the source: "
                               f"{body.get('nodes')}, session {session.last_sources}")
            session.record("What is Foo?", "The roaster.", ["foo"], [0], ids={0: (str(notes_dir), foo_b.path, None)})
            galaxy.notes.reverse()                      # index 0 is shop a's Foo again
            if session.snapshot()[2] != []:
                trouble.append("(1) the session carried a source whose note is another Foo.md now")
            # (2) A reload comes between the search and the render.
            galaxy.notes[:], galaxy.build_id, workers = [foo_a], "two", []
            real_search = galaxy.search

            def search_then_reload(*args, **kwargs):
                hits = real_search(*args, **kwargs)
                workers.append(reload_soon([foo_b], "two-b"))
                return hits

            galaxy.search, sent[:] = search_then_reload, []
            server.ask_model = brain()
            ask_chat()
            del galaxy.search
            for worker in workers:
                worker.join(2.0)
            if not sent or "roaster" not in sent[-1] or "grinder" in sent[-1]:
                trouble.append("(2) a reload between the search and the render changed the note the brain was sent")
            # (3) A reload comes between the check of the sources and the read of the graph numbers.
            galaxy.notes[:], galaxy.build_id, workers = [foo_a], "three", []
            real_meta = galaxy.meta

            def meta_after_reload():
                if not workers:
                    workers.append(reload_soon([tea], "three-b"))
                return real_meta()

            galaxy.meta = meta_after_reload
            body = ask_chat()
            del galaxy.meta
            for worker in workers:
                worker.join(2.0)
            if body.get("nodes") != [0] or (body.get("graph") or {}).get("build_id") != "three":
                trouble.append(f"(3) the sources and the graph numbers come from two states: nodes {body.get('nodes')}, "
                               f"build {(body.get('graph') or {}).get('build_id')}")
            # (4) /search: its results and its graph numbers come from one state.
            galaxy.notes[:], galaxy.build_id, workers = [foo_a], "four", []
            galaxy.meta = meta_after_reload
            found = server.search_route({"q": ["foo"]})[1]
            del galaxy.meta
            for worker in workers:
                worker.join(2.0)
            labels = [hit.get("label") for hit in found.get("results", [])]
            if labels != ["Foo"] or (found.get("graph") or {}).get("build_id") != "four":
                trouble.append(f"(4) /search mixed two states: {labels}, build {(found.get('graph') or {}).get('build_id')}")
            # (5) notes_dir changes while the brain thinks: the other folder has the SAME relative path shop/Foo.md
            # at the same index. It is another note.
            first_root, second_root = notes_dir / "first notes", notes_dir / "second notes"
            here, there = [], []
            for root, out, text in ((first_root, here, "Foo is the roaster."), (second_root, there, "Foo is the grinder.")):
                (root / "shop").mkdir(parents=True)
                (root / "shop" / "Foo.md").write_text(f"# Foo\n\n{text}\n", encoding="utf-8")
                out.append(build.make_note(root / "shop" / "Foo.md", root))

            def other_folder() -> None:
                galaxy.notes_dir, galaxy.notes[:] = second_root, there

            galaxy.notes_dir, galaxy.notes[:], galaxy.build_id = first_root, here, "five"
            server.ask_model = brain(during=other_folder)
            body = ask_chat()
            session = server.get_session(session_id)
            if body.get("nodes") != [] or session.last_sources != []:
                trouble.append(f"(5) the same shop/Foo.md in another notes folder was taken as the source: "
                               f"{body.get('nodes')}, session {session.last_sources}")
            session.record("What is Foo?", "The roaster.", ["foo"], [0], ids={0: (str(first_root), here[0].path, None)})
            if session.snapshot()[2] != []:
                trouble.append("(5) the session carried a source from another notes folder")
            # (6) While the brain thinks, the capture it was shown is forgotten and a second capture with the same
            # first words takes its title, path and number. Other bytes: another note, also in the session.
            sixth = notes_dir / "sixth notes"
            sixth.mkdir()
            galaxy.notes_dir, galaxy.notes[:], galaxy.build_id = sixth, [], "six"
            galaxy.links.clear()
            first = galaxy.remember("Foo the roaster needs oil on Mondays before we open")
            later: list = []

            def forget_and_capture_again() -> None:
                galaxy.forget_last(first["index"], first["title"], first["build"], first["born"])
                later.append(galaxy.remember("Foo the roaster needs oil on Mondays after the lunch rush"))

            server.ask_model = brain(during=forget_and_capture_again)
            body = ask_chat()
            session = server.get_session(session_id)
            same_place = bool(later) and (later[0]["file"], later[0]["index"]) == (first["file"], first["index"])
            if not same_place or body.get("nodes") != [] or session.last_sources != []:
                trouble.append(f"(6) a later capture at the same place was taken for the one the brain was shown: "
                               f"same place {same_place}, nodes {body.get('nodes')}, session {session.last_sources}")
            first_id = (str(sixth), first["file"], first["born"])
            session.record("What is Foo?", "The roaster.", ["foo"], [first["index"]], ids={first["index"]: first_id})
            if session.snapshot()[2] != []:
                trouble.append("(6) the session carried the source of a capture that was forgotten and made again")
            if unverified:
                trouble.append(f"NOT VERIFIED: the reload of {', '.join(dict.fromkeys(unverified))} did not reach the "
                               "lock in time, so those steps prove nothing")
    except Exception as error:                          # noqa: BLE001 - a check that stops says so, it never crashes preflight
        trouble.append(f"the check stopped: {type(error).__name__}: {error}")
    finally:
        server.GALAXY, server.ask_model, build.write_graph = real_galaxy, real_ask, real_write
        with server.SESSIONS_LOCK:
            server.SESSIONS.pop(session_id, None)
    if writes:
        trouble.append(f"build.write_graph was called {len(writes)} times: nothing may write here")
    report.add(FAIL if trouble else PASS, name, "; ".join(trouble) or
               "two Foo.md swapped, a reload before the render, a reload before the graph numbers, /search, "
               "the same path in another notes folder, a capture made again at the same place")


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


# What the server answers when it refuses one of these probes: 404 for a file it does not serve ({"error":
# "not_found"}: resolve_static, the teardown routes), 403 for the locked page or line. One probe more is "/" with a
# query: it gets the start page itself (200, the Jarviz page), never a file. Any other answer (a 500, a 400) is no
# refusal: it is not verified.
PROBE_REFUSALS = frozenset({403, 404})


def probe_refused(probe: str, status: int, body: bytes) -> bool:
    """True when this answer to a probe is a refusal (PROBE_REFUSALS, or the start page for "/" with a query)."""
    if status in PROBE_REFUSALS:
        return True
    return probe.partition("?")[0] == "/" and status == 200 and b"<title>Jarviz</title>" in body


def check_config_hidden(report: Report, ctx: dict) -> None:
    name = "config.json is NOT reachable from the browser"
    config = ctx["config"]
    keys = [str(config[spec["key"]]).encode("utf-8") for name, spec in brains.PROVIDERS.items()
            if "key" in spec and brains.describe(config, name)["has_key"]]     # a sign-in brain has no key
    leaks, silent, unsent, odd, refused = [], [], [], [], 0
    for probe in CONFIG_PROBES + TEARDOWN_PROBES:
        kind, got = try_probe(lambda probe=probe: call("GET", probe, timeout=10))
        if kind == "not_sent":
            unsent.append(probe)                     # the client would not send it: counted apart, not as refused
            continue
        if kind == "no_answer":
            silent.append(f"{probe} ({got})")        # no answer proves nothing
            continue
        status, body = got
        if b"_api_key" in body or any(key in body for key in keys) or (status == 200 and b"<title>Jarviz</title>" not in body):
            leaks.append(f"{probe} -> {status}")
        elif probe_refused(probe, status, body):
            refused += 1
        else:
            odd.append(f"{probe} -> HTTP {status}")  # no leak seen, but no refusal either: an error proves nothing
    # The routes that talk about brains must name them, never show a key.
    for path in ("/status", "/search?q=key"):
        kind, got = try_probe(lambda path=path: call("GET", path, timeout=10))
        if kind != "answer":
            silent.append(f"{path} ({got})")
        elif any(key in got[1] for key in keys):
            leaks.append(f"{path} shows a key")
    if leaks:
        banner = "!" * 72
        print(f"\n{banner}\n  DANGER: A PRIVATE FILE IS REACHABLE FROM THE BROWSER. STOP THE SERVER NOW.\n"
              f"  {'; '.join(leaks)}\n{banner}\n", flush=True)
        report.add(FAIL, name, "LEAK: " + "; ".join(leaks))
    elif silent or odd:
        report.add(FAIL, name, "NOT VERIFIED: " + "; ".join(
            part for part in (f"{len(silent)} probes got no answer: " + short("; ".join(silent), 300) if silent else "",
                              f"{len(odd)} probes got an answer that is no refusal: " + short("; ".join(odd), 300)
                              if odd else "") if part))
    else:
        report.add(PASS, name, f"{refused} ways tried, all refused" +
                   (f"; {len(unsent)} the client itself would not send: {', '.join(unsent)}" if unsent else ""))


def check_other_sites(report: Report) -> None:
    """A website open in the same browser must not be able to use, change or read Jarviz."""
    name = "Other websites cannot use the server or read the notes"
    evil = "http://evil.example"
    nothing = {"message": "", "session": "preflight-outsider"}      # empty: it can never reach the brain
    attempts = {
        "a POST from another website": lambda: call("POST", "/chat", nothing, extra_headers={"Origin": evil}),
        "a POST marked cross-site": lambda: call("POST", "/chat", nothing, extra_headers={"Sec-Fetch-Site": "cross-site"}),
        "a POST from another local port": lambda: call("POST", "/remember", {"text": ""}, extra_headers={"Origin": "http://127.0.0.1:9999"}),
        "a request under another host name": lambda: call("GET", "/status", extra_headers={"Host": f"evil.example:{server.PORT}"}),
        "a script tag on another website": lambda: call("GET", "/graph-data.js", extra_headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "script"}),
        "a form post (not JSON)": lambda: call("POST", "/chat", extra_headers={"Content-Type": "text/plain", "Content-Length": "0"}),
        # INERT_SITE is not an address, so this can never start a job, even if the guard were broken.
        "a teardown started by another website": lambda: call("POST", "/teardown/start", {"site": INERT_SITE}, extra_headers={"Origin": evil}),
        # A made-up job id: this can never stop a real job, even if the guard were broken.
        "a teardown cancelled by another website": lambda: call("POST", "/teardown/cancel", {"job": "preflight-no-such-job"},
                                                                extra_headers={"Sec-Fetch-Site": "cross-site"}),
        "a teardown note read by another website": lambda: call("GET", "/teardown/last", extra_headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "empty"}),
    }
    let_in, silent, unsent = [], [], []
    for what, send in attempts.items():
        kind, got = try_probe(send)
        if kind == "not_sent":
            unsent.append(what)                         # counted apart: the client itself would not send it
        elif kind == "no_answer":
            silent.append(f"{what} ({got})")            # no answer is not a refusal
        elif got[0] not in (403, 415):
            let_in.append(what)
    if let_in:
        report.add(FAIL, name, "LET IN: " + "; ".join(let_in))
    elif silent:
        report.add(FAIL, name, "NOT VERIFIED, no answer: " + "; ".join(silent))
    else:
        report.add(PASS, name, f"{len(attempts) - len(unsent)} ways tried, all refused" +
                   (f"; the client itself would not send: {', '.join(unsent)}" if unsent else ""))


SIGN_REPLAY_LIMIT_S = server.SIGN_WINDOW_S / 2      # "clearly inside" the life of a signature


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

    # A signature works once: the same signed request a second time is locked out. A newly signed one works. The
    # second send comes right after the first: a refusal proves the rule only while the signature is still young (a
    # refusal after its life of SIGN_WINDOW_S has another reason). So the time from its stamp is measured.
    signed_at, started = time.time(), time.monotonic()
    once = signed(server.sign_request(key, instance, "GET", "/status", now=signed_at))
    first_kind, first = try_probe(lambda: outsider("GET", "/status", headers=once))
    replay_kind, replay = try_probe(lambda: outsider("GET", "/status", headers=once))
    replay_age = max(abs(time.time() - int(signed_at)), time.monotonic() - started)
    # A signed POST is claimed only after its body matched: a caught header sent first with ANOTHER body is refused
    # and does not use up the true request; the true body then works once; a third time it is refused. The empty
    # question changes nothing: /chat answers "empty_message" before anything else.
    post_once = signed(server.sign_request(key, instance, "POST", "/chat", json.dumps(nothing).encode("utf-8")))
    changed = {"message": "", "session": "preflight-changed"}
    posts = [try_probe(lambda body=body: outsider("POST", "/chat", body, headers=post_once)) for body in (changed, nothing, nothing)]
    # Empty texts, empty names and a site that is no site: none of these could do anything if the lock were broken.
    attempts = {
        "the page": lambda: outsider("GET", "/"),
        "the page by its file name": lambda: outsider("GET", "/index.html"),
        "the page script": lambda: outsider("GET", "/app.js"),
        "the index of the notes": lambda: outsider("GET", "/graph-data.js"),
        "the status": lambda: outsider("GET", "/status"),
        "a search in the notes": lambda: outsider("GET", "/search?q=coffee"),
        "a question to the brain": lambda: outsider("POST", "/chat", nothing),
        "a new note": lambda: outsider("POST", "/remember", empty_note),
        "a change of brain": lambda: outsider("POST", "/brain", {"brain": ""}),
        "a teardown": lambda: outsider("POST", "/teardown/start", {"site": INERT_SITE}),
        "the last teardown": lambda: outsider("GET", "/teardown/last"),
        "a second try without a key": lambda: outsider("GET", f"/?{server.AGAIN_PARAM}=1"),
        "a wrong key in the cookie": lambda: outsider("GET", "/status", headers={"Cookie": f"{server.KEY_COOKIE}={wrong}"}),
        "a wrong key in the address": lambda: outsider("GET", f"/?{server.KEY_PARAM}={wrong}"),
        "a wrong key in the old header": lambda: outsider("GET", "/status", headers={"X-Jarviz-Key": wrong}),
        "a signature made with a wrong key": lambda: outsider("GET", "/status", headers=sign("GET", "/status", with_key=wrong)),
        "a signature made for another address": lambda: outsider("GET", "/status", headers=sign("GET", "/search?q=x")),
        "a signature made for another method": lambda: outsider("GET", "/status", headers=sign("POST", "/status")),
        "a signature that is one hour old": lambda: outsider("GET", "/status", headers=sign("GET", "/status", now=time.time() - 3600)),
        "a signature made for another body": lambda: outsider("POST", "/remember", empty_note, headers=sign("POST", "/remember", b"{}")),
        "a signature made for another start of the server": lambda: outsider("GET", "/status", headers=sign("GET", "/status", start="s" * 16)),
        "a signature with no parts": lambda: outsider("GET", "/status", headers=signed("")),
        # A good signature opens the routes, but it is never traded for the cookie (the cookie IS the key).
        "a good signature at the door, to get the cookie": lambda: outsider(
            "GET", f"/?{server.KEY_PARAM}={wrong}&mute=1", headers=sign("GET", f"/?{server.KEY_PARAM}={wrong}&mute=1")),
        "a good signature at the second try, to get the cookie": lambda: outsider(
            "GET", f"/?{server.AGAIN_PARAM}=1", headers=sign("GET", f"/?{server.AGAIN_PARAM}=1")),
    }
    trouble, unsent = [], []
    for what, send in attempts.items():
        kind, got = try_probe(send)
        if kind == "not_sent":
            unsent.append(what)                         # counted apart: the client itself would not send it
        elif kind == "no_answer":
            trouble.append(f"NOT VERIFIED (no answer, {got}): {what}")
        elif not refused(*got):
            trouble.append("LET IN: " + what)
    if first_kind != "answer" or first[0] != 200:
        trouble.append("a newly signed request is not let in the first time")
    elif replay_kind != "answer":
        trouble.append(f"NOT VERIFIED (no answer, {replay}): the same signed request a second time")
    elif replay_age >= SIGN_REPLAY_LIMIT_S:
        trouble.append(f"NOT VERIFIED: the same signed request was sent again {replay_age:.0f} s after it was signed, "
                       f"not clearly inside its life of {server.SIGN_WINDOW_S} s, so its refusal proves nothing")
    elif not refused(*replay):
        trouble.append("LET IN: the same signed request a second time")
    if any(kind != "answer" for kind, _ in posts):
        trouble.append("NOT VERIFIED (no answer): the signed POST sent with another body, then twice with its own")
    else:
        try:
            true_answer = json.loads(posts[1][1][2].decode("utf-8"))
        except ValueError:
            true_answer = {}
        if not refused(*posts[0][1]):
            trouble.append("LET IN: a signed POST with another body")
        if posts[1][1][0] != 400 or true_answer.get("error") != "empty_message":
            trouble.append("a signed POST with its own body is not let in after a try with another body")
        if not refused(*posts[2][1]):
            trouble.append("LET IN: the same signed POST a second time")
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
               "; ".join(trouble) if trouble else
               f"{len(attempts) - len(unsent)} ways tried without it, all refused (a signature works once: sent again "
               f"{replay_age:.1f} s after it was signed, it was refused). With it, the page opens."
               + (f" The client itself would not send: {', '.join(unsent)}." if unsent else ""))


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
    routes = ctx.get("teardown_routes")     # None: the voice routes gave no result (no Node.js). []: they all passed
    failed += routes or []
    report.add(FAIL if failed else PASS, name,
               short("; ".join(failed), 400) if failed
               else f"{len(results)} checks" + (", and the voice routes" if routes is not None else ""))


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
    if profiles is None:
        report.add(WARN, name, "Not checked: the profiles folder could not be listed, so the clean-up is not verified.")
        return
    report.add(FAIL if chromes or profiles else PASS, name,
               f"{len(chromes)} Chrome processes and {len(profiles)} profile folders are left. Restart the server to sweep them."
               if chromes or profiles else "")


NODE_TIMEOUT_S = 30
PAGE_CHECKS = build.ROOT / "page_checks.js"     # the page's own rules, run in Node (check_page_rules)


def run_node(node: str, script, files: dict[str, str]) -> tuple[str | None, str]:
    """Write `files` (name -> text) to a new temporary folder and run there: node <script> <those files, in the
    order given>. `script` is a path, or one of the names in `files` (that one is then not an argument).
    (its output, "") or (None, why not): no folder or files for it, Node did not start, took too long, or exited with
    an error."""
    try:
        room = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    except OSError as error:
        return None, f"no temporary folder for Node.js ({error.strerror or type(error).__name__})"
    with room as folder:
        paths = {}
        try:
            for name, text in files.items():
                paths[name] = Path(folder) / name
                paths[name].write_text(text, encoding="utf-8")
        except (OSError, ValueError) as error:
            return None, f"the files for Node.js could not be written ({getattr(error, 'strerror', None) or type(error).__name__})"
        script_path = paths.get(str(script)) or Path(script)
        arguments = [str(path) for name, path in paths.items() if name != str(script)]
        try:
            run = subprocess.run([node, str(script_path), *arguments], cwd=folder, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=NODE_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            return None, f"Node.js took longer than {NODE_TIMEOUT_S} seconds"
        except (OSError, subprocess.SubprocessError) as error:
            return None, f"Node.js did not start ({type(error).__name__})"
    if run.returncode != 0:
        return None, f"Node.js exited with code {run.returncode}: " + short(run.stderr or run.stdout or "no output", 200)
    return run.stdout, ""


def run_node_harness(node: str, core_text: str) -> tuple[dict | None, str]:
    """Run NODE_HARNESS on one core.js text. (its JSON, "") or (None, why not): Node did not start, took too long,
    exited with an error, or printed no JSON object."""
    out, why = run_node(node, "harness.js", {"harness.js": NODE_HARNESS, "core.js": core_text})
    if out is None:
        return None, why
    try:
        result = json.loads(out)
    except ValueError:
        return None, "it printed no JSON: " + short(out or "no output", 200)
    return (result, "") if isinstance(result, dict) else (None, "it printed JSON that is not an object")


def page_result(out: str) -> tuple[dict | None, str]:
    """The one line of JSON that page_checks.js prints: {"pass": int, "fail": int, "failures": [text, ...]}."""
    lines = out.strip().splitlines()
    if len(lines) != 1:
        return None, f"it printed {len(lines)} lines, not one line of JSON"
    try:
        result = json.loads(lines[0])
    except ValueError:
        return None, "its line is not JSON: " + short(lines[0], 120)
    counts = [result.get(key) for key in ("pass", "fail")] if isinstance(result, dict) else []
    if (len(counts) != 2 or not all(isinstance(count, int) and not isinstance(count, bool) for count in counts)
            or not isinstance(result.get("failures"), list) or not all(isinstance(f, str) for f in result["failures"])):
        return None, "its JSON is not {\"pass\": number, \"fail\": number, \"failures\": [texts]}"
    return result, ""


def check_page_rules(report: Report, ctx: dict) -> None:
    """The page's own rules (replies, the galaxy file, the microphone), run in Node by page_checks.js on the core.js
    and app.js that the SERVER serves. A harness that did not run, or that checked nothing, proves nothing."""
    name = "The page keeps its rules for replies, the galaxy file and the microphone"
    if not PAGE_CHECKS.is_file():
        report.add(FAIL, name, f"{PAGE_CHECKS.name} is missing in the project folder.")
        return
    node = shutil.which("node")
    if not node:
        report.add(WARN, name, "Not checked: it needs Node.js.")
        return
    sources = {}
    for served in ("core.js", "app.js"):
        kind, got = try_probe(lambda served=served: call("GET", "/" + served, timeout=10))
        if kind != "answer" or got[0] != 200:
            report.add(FAIL, name, f"The server did not serve {served} ({got if kind != 'answer' else got[0]}).")
            return
        sources[served] = got[1].decode("utf-8", "replace")
    out, why = run_node(node, PAGE_CHECKS, sources)
    result, why = page_result(out) if out is not None else (None, why)
    if result is None:
        report.add(FAIL, name, f"{PAGE_CHECKS.name} did not run to its end: {why}")
    elif result["pass"] == 0:
        report.add(FAIL, name, f"{PAGE_CHECKS.name} checked no rule.")
    elif result["fail"] or result["failures"]:
        shown = "; ".join(short(failure, 120) for failure in result["failures"][:5])
        report.add(FAIL, name, f"{result['fail']} of {result['pass'] + result['fail']} rules failed: {shown or 'no names given'}")
    else:
        report.add(PASS, name, f"{result['pass']} rules")


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
    result, reason = run_node_harness(node, sources["core.js"])
    if result is None:                  # a harness that did not run proves nothing: both checks fail, with the reason
        ctx["teardown_routes"] = [f"the voice routes were not checked: {reason}"]
        report.add(FAIL, mute_name, "The served core.js did not run: " + reason)
        report.add(FAIL, finish_name, "Not checked: " + reason)
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
    # A question starts one only when its subject IS the site. A question about a teardown goes to the brain, and so
    # does a sentence that refuses one ("How does example.com work? Do not tear it down.").
    if result.get("questions") != [None, None, None, "stripe.com", "stripe.com", "igloo.inc", "this", None,
                                   "example.com", None]:
        routes.append(f"the teardown questions matched the wrong sentences: {result.get('questions')}")
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
    # A sentence ABOUT a teardown (one that is there, or one in the past) does not ask for a new one. A request
    # next to such a clause still counts.
    asks += ("Tear down stripe.com", "tear stripe.com down", "Reverse engineer igloo.inc", "Do a teardown of stripe.com",
             "Teardown stripe.com", "Can you run a teardown on igloo.inc?", "How about a teardown of stripe.com?",
             "Show me a teardown of stripe.com", "I liked the last teardown. Now tear down stripe.com.",
             "Show me the last teardown, then tear down stripe.com", "What does stripe.com use? Tear it down.",
             "Do a teardown of the stripe.com homepage", "Run the teardown on igloo.inc again",
             "What is igloo.inc? Tear it down.", "Show me the last teardown and tear down stripe.com",
             "What is teardown.com built with?", "Redo my teardown of stripe.com", "Do the stripe.com teardown",
             "Tear down stripe.com like the last teardown",
             "How is the stripe.com homepage built compared to my last teardown?")
    about = ("What did my teardown of stripe.com say?", "What does the teardown note for igloo.inc say about the fonts?",
             "Show me the teardown of stripe.com", "Open the last teardown", "Read me my teardown of igloo.inc",
             "When did I tear down stripe.com?", "Have you already reverse engineered igloo.inc?",
             "Did you take igloo.inc apart yet?", "Is the teardown done?", "Did the teardown of stripe.com work?",
             "Cancel the teardown", "What is a teardown?", "What did the stripe.com teardown say?",
             "Open the igloo teardown", "Show me the stripe.com teardown", "Is the stripe.com teardown done?",
             "Stop the igloo.inc teardown", "What does the last stripe.com teardown say about fonts?",
             "Can you summarize my teardown of stripe.com?", "Compare the stripe.com teardown with the linear.app teardown",
             "Delete the teardown of stripe.com", "Explain the last teardown to me",
             "Show me the last teardown and its screenshots", "Tell me about my teardown of igloo.inc")
    # A clause that says NOT to do it does not ask for one. A suggestion ("why don't you") and a request in
    # another clause still count.
    refused = ("Do not tear down stripe.com", "Don't tear down stripe.com", "Never reverse engineer igloo.inc",
               "Please don't take igloo.inc apart", "No teardown of stripe.com today", "I do not want a teardown of stripe.com",
               "You must not x-ray this site", "Tell me about stripe.com without tearing it down")
    asks += ("Why don't you tear down stripe.com?", "Why not tear down stripe.com?", "Can't you tear down stripe.com?",
             "Won't you reverse engineer igloo.inc?", "Don't forget to tear down stripe.com",
             "Don't wait, tear down stripe.com", "Don't open the old note, tear down stripe.com")
    intent = server.TAG_INTENT["TEARDOWN"]
    trouble = [f"missed: {said!r}" for said in asks if not intent(said)]
    trouble += [f"let in: {said!r}" for said in mentions + about + refused if intent(said)]
    # The site of a tag must be named in the part that ASKS, not in a refused part or a part about an old teardown.
    bound = (("Do not tear down stripe.com, then tear down example.com", "example.com", True),
             ("Do not tear down stripe.com, then tear down example.com", "stripe.com", False),
             ("Tear down stripe.com but don't take igloo.inc apart", "stripe.com", True),
             ("Tear down stripe.com but don't take igloo.inc apart", "igloo.inc", False),
             ("Don't x-ray this site, tear down stripe.com", "this", False),
             ("Don't x-ray this site, tear down stripe.com", "stripe.com", True),
             ("x-ray this site", "this", True), ("Tear down stripe.com", "evil.example", False),
             # ...in the clause that REQUESTS it. Another clause counts only when the request points ("tear it down").
             ("Tell me about stripe.com, then tear down example.com", "example.com", True),
             ("Tell me about stripe.com, then tear down example.com", "stripe.com", False),
             ("I like igloo.inc. Take it apart.", "igloo.inc", True),
             ("What is igloo.inc? Tear it down.", "igloo.inc", True),
             ("What does stripe.com use? Tear it down.", "stripe.com", True),
             ("Stripe.com is slow. How is it built?", "stripe.com", True),
             ("Compare linear.app with my notes, then reverse engineer stripe.com", "stripe.com", True),
             ("Compare linear.app with my notes, then reverse engineer stripe.com", "linear.app", False),
             # A colon inside an address (https://, a port) does not end a clause. An ordinary colon still does.
             ("Tear down https://example.com", "https://example.com", True),
             ("Tear down https://example.com", "example.com", True),
             ("tear down http://example.com:8080/x", "http://example.com:8080/x", True),
             ("tear down http://example.com:8080/x", "example.com:8080", True),
             ("note this: stripe.com is nice, tear down example.com", "example.com", True),
             ("note this: stripe.com is nice, tear down example.com", "stripe.com", False))
    trouble += [f"a tag for {site} after {said!r} is {'refused' if wanted else 'accepted'}"
                for said, site, wanted in bound if server.tag_site_named(site, said) != wanted]
    for said in asks:                   # every request that names an address accepts a tag for that address
        named = re.search(r"\b[a-z0-9-]+\.[a-z]{2,}\b", said)
        if named and not server.tag_site_named(named.group(), said):
            trouble.append(f"a tag for {named.group()} after {said!r} is refused")
    if not server.TAG_INTENT["REMEMBER"]("remember that the homepage needs a new photo"):
        trouble.append("the REMEMBER intent no longer matches \"remember that the homepage needs a new photo\"")
    report.add(FAIL if trouble else PASS, name,
               "; ".join(trouble) or f"{len(asks)} requests let in; {len(mentions)} mentions, {len(about)} sentences "
                                     f"about a teardown and {len(refused)} refusals kept out; {len(bound)} tags bound "
                                     "to the part that asks")


def main() -> int:
    report = Report()
    config = server.load_config()
    brain = brains.describe(config)
    ctx = {"config": config, "brain": brain, "key_set": brain["has_key"], "session": "preflight-" + secrets.token_hex(4)}
    print(f"Jarviz preflight  {time.strftime('%Y-%m-%d %H:%M:%S')}  http://{server.HOST}:{server.PORT}"
          f"  brain: {brain['label']} / {brain['model']}\n")
    if check_server(report):
        check_graph(report, ctx)
        check_brains_offline(report)
        check_brain_locks(report, ctx)          # before the first question goes through the server
        check_chat(report, ctx)
        check_key_and_model(report, ctx)
        check_other_brains(report, ctx)
        check_remember(report, ctx)
        check_links_stay_true(report)
        check_take_back_safe(report)
        check_notes_dir_follows_config(report)
        check_brain_choice_saved(report)
        check_build_survives(report)
        check_teardown_one_folder(report)
        check_teardown_receipt_turn(report)
        check_teardown_cancel(report)
        check_signature_once(report)
        check_sources_stay_true(report)
        check_served_files(report)
        check_config_hidden(report, ctx)
        check_other_sites(report)
        check_lock(report)
        check_persona_home(report)
        check_pinned_libraries(report)
        check_voice_rules(report, ctx)
        check_page_rules(report, ctx)
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
