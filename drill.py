"""The teardown drill: prove the whole kit before a camera sees it, and leave no trace.

    python drill.py                    the full drill (the server must be running: python server.py)
    python drill.py --site igloo.inc   use another public page for the live part (default: example.com)
    python drill.py --offline          only the fast checks: no browser, no network, no server
    python drill.py --keep FOLDER      copy the test note and its snapshot to FOLDER before they are removed
    python drill.py --visible          the same drill with the browser window on screen (a person starts this one)

The live part runs ONE real teardown with a hidden browser, then one that is cancelled and one of an
address where nothing answers. Before it, the drill records the galaxy (every note and every link)
and the teardowns folder. After it, the drill removes the files that its own test made (and only
those) and builds the galaxy again from the notes. So your first real teardown is truly the first.
Jarviz keeps no log of what was said on disk: the drill talks in a session of its own, and that
session is gone when the server stops.
"""
from __future__ import annotations

import http.client
import json
import re
import secrets
import shutil
import socket
import sys
import time
from pathlib import Path
from urllib.parse import quote

import build
import preflight
import server
import teardown

PASS, FAIL = "pass", "fail"
LIVE_LIMIT_S = 240          # the whole live teardown must end inside this time
CANCEL_WAIT_S = 30          # after a cancel at the limit, how long the job may take to stop
GOOD_TIME_S = 150           # ...and a healthy one is faster than this
call, call_json = preflight.call, preflight.call_json


class BadAnswer(Exception):
    """The server answered, but not with a good status answer. It counts as no answer."""


SERVER_GONE = (OSError, http.client.HTTPException, BadAnswer)
VISIBLE = False             # --visible: the one drill with a browser window on screen


class Report(preflight.Report):
    def summary(self) -> str:
        return f"DRILL: {self.counts[PASS]} pass, {self.counts[FAIL]} fail"


def tree(folder: Path) -> dict[str, int]:
    """Every file under `folder` with its size. Used to prove the folder is the same afterwards."""
    if not folder.is_dir():
        return {}
    return {path.relative_to(folder).as_posix(): path.stat().st_size for path in sorted(folder.rglob("*")) if path.is_file()}


def job_of(status: int, raw: bytes) -> dict:
    """The job in ONE status answer. Only a good answer counts: HTTP 200, a JSON object, "ok" not false, and the key
    "job" with an object or null. Then the job's view, or {} for null (no job). Anything else (an error page, a 500,
    {"ok": false}, {} with no "job") raises BadAnswer: it is no answer, never "no job"."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        body = None
    if status != 200 or not isinstance(body, dict) or body.get("ok") is False or "job" not in body:
        raise BadAnswer(f"HTTP {status}")
    job = body["job"]
    if job is None:
        return {}                       # no job: "not ours"
    if not isinstance(job, dict) or not isinstance(job.get("id"), str) or not job["id"]:
        raise BadAnswer("a job without an id")      # a job must say whose it is
    return job


def job_now(timeout: float = 150) -> dict:
    return job_of(*call("GET", "/teardown/status", timeout=timeout))


def cancel(job_id: str | None, timeout: float = 150) -> dict:
    """Cancel the drill's OWN job. The id goes with it, so the server can never stop another job for the drill."""
    return call_json("POST", "/teardown/cancel", {"job": job_id or "drill-no-job"}, timeout=timeout)[1]


ENDED, NOT_CONFIRMED = "ended", "not confirmed"
END_STATES = ("done", "failed", "cancelled")     # the only states that end a job; "running" goes on
SILENT_RETRY_S = 10         # a status request that gets no answer is tried again for this long, then the drill gives up
POLL_S = 0.5
REQUEST_FLOOR_S = 1.0       # the shortest timeout one request gets, also when its budget is nearly spent


def request_timeout(left: float) -> float:
    """The timeout of one request: never longer than what its budget has left, and never below REQUEST_FLOOR_S."""
    return max(REQUEST_FLOOR_S, left)


def follow_job(job_id: str | None, limit: float, ask, ask_cancel, clock=time.monotonic, sleep=time.sleep) -> tuple[str, dict]:
    """Poll OUR job until its end is CONFIRMED. Pure: `ask(timeout)` gives the status view of the job (it raises
    SERVER_GONE when no answer comes), `ask_cancel(timeout)` asks for the cancel, `clock` and `sleep` are the time.
    Each request gets a timeout that is no longer than its budget has left: a status request the silence budget
    (SILENT_RETRY_S, counted from the send of the first request that got no answer) and the time to the deadline, the
    cancel the wait after it (CANCEL_WAIT_S). Returns (verdict, the LAST view of our job, {} when none came):
      ENDED          a view of our job with an end state (done, failed, cancelled);
      NOT_CONFIRMED  no answer for SILENT_RETRY_S in a row; still running after the cancel at `limit` and
                     CANCEL_WAIT_S more; or another job's id answers (or none): our job is gone and its end was never
                     seen. An end that is not confirmed is NOT an end: the job can still have written. The receipts of
                     the last view still count: they are files the job made.
    A view of our job with a missing or unknown state is no answer: it is tried again like one."""
    job, cancelled, silent_since = {}, False, None
    deadline = clock() + limit
    while True:
        sent = clock()
        left = min(SILENT_RETRY_S - (sent - silent_since if silent_since is not None else 0), deadline - sent)
        try:
            view = ask(request_timeout(left))
            if view.get("id") == job_id and view.get("state") not in ("running", *END_STATES):
                raise BadAnswer(f"the state {view.get('state')!r}")     # an unknown or missing state ends nothing
            silent_since = None
        except SERVER_GONE:
            view, silent_since = None, sent if silent_since is None else silent_since
            if clock() - silent_since >= SILENT_RETRY_S:
                return NOT_CONFIRMED, job
        if view is not None:
            if view.get("id") != job_id:
                return NOT_CONFIRMED, job       # our job is gone: its end was never seen, and its last view counts
            job = view
            if view.get("state") in END_STATES:
                return ENDED, view
        if clock() >= deadline:
            if cancelled:
                return NOT_CONFIRMED, job
            cancelled, deadline = True, clock() + CANCEL_WAIT_S     # the wait after the cancel starts at its send
            try:
                ask_cancel(request_timeout(deadline - clock()))
            except SERVER_GONE:
                pass                    # the next answers say whether it ended
        sleep(POLL_S)


def wait_for_end(job_id: str | None, limit: float) -> tuple[str, dict]:
    """Poll the job like the panel does: (verdict, the last view of OUR job, {} when none came). See follow_job. A
    job that is still running at the limit is cancelled (with its own id) and polled again for CANCEL_WAIT_S. The
    receipts in a view that is not a confirmed end still count: they are files the job made."""
    return follow_job(job_id, limit, job_now, lambda timeout: cancel(job_id, timeout))


def follow_checks() -> list[tuple[str, bool, str]]:
    """Offline: follow_job with made-up status answers and a made-up clock. No server."""
    results = []

    def follow(answers: list, limit: float, cancel_hangs: bool = False) -> tuple[str, dict, int, float, list]:
        """(verdict, view, number of cancels, the time at the end, [(request, sent at, timeout)]). "gone": no answer
        at once. "hang": no answer until the request's timeout (150 s, preflight's default, when it has none)."""
        now, cancels, asked, requests = [0.0], [], [0], []

        def hang(timeout) -> None:
            now[0] += 150 if timeout is None else timeout
            raise TimeoutError("timed out")

        def ask(timeout=None) -> dict:      # as job_now: one (status, body) answer, read by job_of
            requests.append(("status", now[0], timeout))
            answer = answers[min(asked[0], len(answers) - 1)]
            asked[0] += 1
            if answer == "gone":
                raise ConnectionResetError("no answer")
            if answer == "hang":
                hang(timeout)
            return job_of(*answer)

        def ask_cancel(timeout=None) -> None:
            requests.append(("cancel", now[0], timeout))
            cancels.append(now[0])
            if cancel_hangs:
                hang(timeout)

        def sleep(seconds: float) -> None:
            now[0] += seconds

        verdict, view = follow_job("job1", limit, ask, ask_cancel, lambda: now[0], sleep)
        return verdict, view, len(cancels), now[0], requests

    def good(job) -> tuple[int, bytes]:
        return 200, json.dumps({"ok": True, "job": job}).encode("utf-8")

    running, done, other = {"id": "job1", "state": "running"}, {"id": "job1", "state": "done"}, {"id": "job2", "state": "running"}
    made = {"id": "job1", "state": "running", "births": [{"file": teardown.CONCEPT_PATH, "title": teardown.CONCEPT_NAME}]}
    error_500 = (500, b'{"ok": false, "error": "server_error"}')
    html = (502, b"<html><body>Bad gateway</body></html>")
    refused = (200, b'{"ok": false}')
    cases = [("a status request that fails once, then done, is a confirmed end", ["gone", good(done)], 60, (ENDED, done, 0)),
             ("status requests that fail until the retry limit are not a confirmed end", [good(running), "gone"], 60,
              (NOT_CONFIRMED, running, 0)),
             ("a job that still runs after the cancel and its wait is not a confirmed end", [good(running)], 5,
              (NOT_CONFIRMED, running, 1)),
             ("a job that ends after the cancel is a confirmed end", [good(running)] * 12 + [good(done)], 5, (ENDED, done, 1)),
             # Our job ended between two polls and another one started: its end was never seen. Not confirmed, and
             # the last view of OUR job comes back, so its receipts are still removed.
             ("our job gone and another job's id answering is not a confirmed end, with our last view",
              [good(made), good(other)], 60, (NOT_CONFIRMED, made, 0)),
             ("our job gone and a good answer with no job is not a confirmed end, with our last view",
              [good(running), good(None)], 60, (NOT_CONFIRMED, running, 0)),
             ("another job's id from the first answer is not a confirmed end, with no view", [good(other)], 60,
              (NOT_CONFIRMED, {}, 0))]
    no_job_key = (200, b"{}")                       # a good answer has the key "job"
    stateless, weird = good({"id": "job1"}), good({"id": "job1", "state": "weird"})
    for what, bad in (("an HTTP 500", error_500), ("an HTML error page", html), ('{"ok": false}', refused),
                      ("{} with HTTP 200", no_job_key), ("a job with no state", stateless),
                      ('a job with the state "weird"', weird), ('{"job": {}}', good({})),
                      ('a job with no id', good({"state": "running"})), ("a job with an empty id", good({"id": ""}))):
        cases.append((f"{what} once, then done, is a confirmed end", [bad, good(done)], 60, (ENDED, done, 0)))
        cases.append((f"{what} until the retry limit is not a confirmed end", [good(running), bad], 60,
                      (NOT_CONFIRMED, running, 0)))
    for name, answers, limit, wanted in cases:
        got = follow(answers, limit)[:3]
        results.append((f"drill: {name}", got == wanted, f"got {got}"))
    # Each request gets a timeout no longer than its budget has left, so a server that does not answer cannot stretch
    # the 10 s silence budget, or the 30 s wait after a cancel, by much (at most the floor of one request).
    verdict, _, _, ended, requests = follow([good(running), "hang"], 60)
    silent_from = next((sent for what, sent, _ in requests[1:2]), 0.0)
    within = all(isinstance(timeout, (int, float)) and REQUEST_FLOOR_S <= timeout <= SILENT_RETRY_S
                 for _, _, timeout in requests)
    results.append(("drill: a status request that hangs gets a timeout inside the silence budget, and the budget holds",
                    verdict == NOT_CONFIRMED and within and ended - silent_from <= SILENT_RETRY_S + REQUEST_FLOOR_S,
                    f"{verdict} after {ended - silent_from:.1f} s of silence, timeouts {[t for _, _, t in requests]}"))
    verdict, _, cancels, ended, requests = follow([good(running)], 5, cancel_hangs=True)
    sent_cancel = [(sent, timeout) for what, sent, timeout in requests if what == "cancel"]
    after = [(sent, timeout) for what, sent, timeout in requests if what == "status" and sent_cancel and sent > sent_cancel[0][0]]
    # After the wait, one last look may still go out (a poll pause later, with the floor timeout).
    within = (len(sent_cancel) == 1 and isinstance(sent_cancel[0][1], (int, float)) and sent_cancel[0][1] <= CANCEL_WAIT_S
              and all(isinstance(timeout, (int, float))
                      and sent + timeout <= sent_cancel[0][0] + CANCEL_WAIT_S + POLL_S + REQUEST_FLOOR_S
                      for sent, timeout in after))
    results.append(("drill: a cancel that hangs gets a timeout inside the wait after it, and the wait holds",
                    verdict == NOT_CONFIRMED and cancels == 1 and within
                    and ended <= 5 + POLL_S + CANCEL_WAIT_S + REQUEST_FLOOR_S,
                    f"{verdict} at {ended:.1f} s, cancel {sent_cancel}, status after it {after[:3]}"))
    # The put-back counts only a good status answer with valid graph numbers: anything else proves nothing about
    # what the server holds.
    graph = {"build_id": "b1", "rev": 0, "count": 3}
    bad = [(500, {"ok": False, "error": "server_error"}), (200, {"ok": True}), (200, {"ok": False, "graph": graph}),
           (200, {"ok": True, "graph": {**graph, "count": None}}), (200, {"ok": True, "graph": {**graph, "count": True}}),
           (200, {"ok": True, "graph": {**graph, "build_id": ""}}), (0, {}), (200, [])]
    taken = [answer for answer in bad if served_graph(*answer) is not None]
    results.append(("drill: the put-back takes only a good status answer with valid graph numbers",
                    not taken and served_graph(200, {"ok": True, "graph": graph}) == graph, f"taken: {taken}"))
    return results


def offline(report: Report) -> None:
    for name, passed, detail in teardown.offline_checks() + follow_checks():
        report.add(PASS if passed else FAIL, name, "" if passed else detail)


def safety(report: Report, name: str | None) -> None:
    """The endpoints that serve the note and its picture refuse every path outside the teardowns folder."""
    probes = list(preflight.TEARDOWN_PROBES)
    leaks = []
    for probe in probes:
        status, body = call("GET", probe, timeout=10)
        if status == 200 or b"_api_key" in body:
            leaks.append(f"{probe} -> {status}")
    report.add(FAIL if leaks else PASS, "The note and picture endpoints refuse paths outside the teardowns folder",
               "LEAK: " + "; ".join(leaks) if leaks else f"{len(probes)} ways tried, all refused")
    if name:
        status, picture = call("GET", f"/teardown/image?name={quote(name)}.png")
        fine = status == 200 and picture.startswith(teardown.PNG_SIGNATURE)
        report.add(PASS if fine else FAIL, "The snapshot of the test teardown is served as a picture",
                   f"{len(picture) // 1024} KB" if fine else f"HTTP {status}")


def nothing_left(report: Report, when: str) -> None:
    chromes, profiles = teardown.investigation_chromes(), teardown.leftover_profiles()
    name = f"No investigation Chrome and no profile folder is left {when}"
    if chromes is None:         # "could not look" is never a pass
        report.add(FAIL, name, "The list of running programs could not be read, so this is not proven.")
    elif profiles is None:
        report.add(FAIL, name, "The profiles folder could not be listed, so this is not proven.")
    else:
        report.add(FAIL if chromes or profiles else PASS, name,
                   f"{len(chromes)} Chrome processes, {len(profiles)} profile folders" if chromes or profiles else "")


def shape(graph: dict) -> tuple[list[str], list[tuple[str, str]]]:
    """The galaxy without its numbers: every note by folder and name, every link by its two notes.
    A galaxy that is built again can number the same notes in another order. The shape stays."""
    nodes = graph.get("nodes", [])
    names = [f"{node.get('group')}/{node.get('label')}" for node in nodes]

    def end(value) -> str:
        return names[value] if isinstance(value, int) and 0 <= value < len(names) else "?"

    links = sorted(tuple(sorted((end(link.get("source")), end(link.get("target"))))) for link in graph.get("links", []))
    return sorted(names), links


def record() -> dict:
    """How things are before a test teardown: the notes and links in the galaxy, and the teardowns folder."""
    notes_dir = build.resolve_notes_dir(server.load_config()["notes_dir"])
    folder = notes_dir / teardown.TEARDOWNS_DIR
    watched = (notes_dir / "captures", folder, folder / teardown.ASSETS_DIR)
    graph = build.read_graph()
    return {"notes_dir": str(notes_dir), "labels": [node.get("label") for node in graph.get("nodes", [])],
            "shape": shape(graph), "tree": tree(folder), "folders": {str(path): path.is_dir() for path in watched}}


def closed_port() -> int:
    """A port on this computer where nothing answers."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def made_by(job: dict) -> list[str]:
    """The files one test teardown made, as paths inside the notes folder. Nothing else is ever removed.
    A job that failed half way can have made the Jarviz note or the REA note: the job lists every birth."""
    result = job.get("result") or {}
    name = result.get("name")
    files = []
    if job.get("state") == "done" and teardown.valid_name(name):
        files.append(f"{teardown.TEARDOWNS_DIR}/{name}.md")
        if result.get("snapshot"):
            files.append(f"{teardown.TEARDOWNS_DIR}/{teardown.ASSETS_DIR}/{name}.png")
    births = [birth for birth in (job.get("births") or result.get("births") or []) if isinstance(birth, dict)]
    for concept in (teardown.CONCEPT_PATH, teardown.OWNER_PATH):
        if any(birth.get("file") == concept for birth in births):
            files.append(concept)                       # the REA note or the Jarviz note, only when THIS job made it
    return files


def served_graph(status: int, body) -> dict | None:
    """The graph numbers in ONE /status answer, or None. Only a good answer counts: HTTP 200, a JSON object, "ok" not
    false, and "graph" with a text build_id and whole numbers for rev and count. Anything else is no answer: it
    proves nothing about the galaxy the server holds."""
    graph = body.get("graph") if isinstance(body, dict) else None
    if status != 200 or not isinstance(graph, dict) or body.get("ok") is False:
        return None
    numbers = (graph.get("rev"), graph.get("count"))
    if not (isinstance(graph.get("build_id"), str) and graph["build_id"]
            and all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in numbers)):
        return None
    return graph


def put_back(state: dict, made: list[str]) -> tuple[bool, str]:
    """Remove the files in `made` (the test's own), remove folders that the test made and left empty,
    and build the galaxy again from the notes on disk. It never raises: it reports."""
    notes_dir = Path(state["notes_dir"])
    folder = notes_dir / teardown.TEARDOWNS_DIR
    trouble: list[str] = []
    for relative in made:
        target = (notes_dir / relative).resolve()
        try:
            if target.is_relative_to(notes_dir.resolve()):
                target.unlink(missing_ok=True)
        except OSError as error:
            trouble.append(f"{relative} could not be removed ({error.strerror or error})")
    for path in (folder / teardown.ASSETS_DIR, folder, notes_dir / "captures"):
        try:
            if not state["folders"].get(str(path)) and path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except OSError:
            pass
    try:
        build.build(notes_dir)                          # the galaxy file, made again from the notes that are there
    except (OSError, ValueError) as error:
        trouble.append(f"the galaxy file could not be written ({error})")
    try:
        status, now = call_json("GET", "/status")       # the server sees the new file and loads the notes again
    except SERVER_GONE:
        status, now = 0, {}
    served = served_graph(status, now)
    if served is None:                                  # no good answer: the server may still hold the old galaxy
        trouble.append(f"the server gave no good status answer ({f'HTTP {status}' if status else 'no answer'}), so it is "
                       "not proven that it holds the galaxy as it was put back. Restart it")
    graph = build.read_graph()
    labels = [node.get("label") for node in graph.get("nodes", [])]
    notes, links = shape(graph)
    if notes != state["shape"][0]:
        trouble.append(f"the galaxy does not hold the same notes ({len(labels)} now, {len(state['labels'])} before)")
    elif links != state["shape"][1]:
        trouble.append(f"the galaxy does not hold the same links ({len(links)} now, {len(state['shape'][1])} before)")
    elif served is not None and (served["count"] != len(labels) or served["build_id"] != graph.get("build_id")):
        trouble.append(f"the server holds {served['count']} notes of build {served['build_id']}, the file has "
                       f"{len(labels)} of build {graph.get('build_id')}. Restart it")
    if tree(folder) != state["tree"]:
        trouble.append("the teardowns folder is not as it was")
    return not trouble, "; ".join(trouble) or f"{len(labels)} notes and {len(links)} links, as before"


def start(site: str, session: str, question: str = "") -> tuple[int, dict, str]:
    body = {"site": site, "session": session, "headless": not VISIBLE}
    if question:
        body["question"] = question
    status, started = call_json("POST", "/teardown/start", body)
    return status, started, ((started.get("action") or {}).get("job") or {}).get("id") or ""


def live(report: Report, site: str, keep: Path | None) -> None:
    before = record()
    folder = Path(before["notes_dir"]) / teardown.TEARDOWNS_DIR
    nodes_before = len(before["labels"])
    session = "drill-" + secrets.token_hex(4)
    ours: list[str] = []            # the ids of the jobs this drill started
    made: list[str] = []            # the files those jobs made
    ends: dict[str, str] = {}       # job id -> the verdict of its newest follow: only ENDED is a confirmed end

    def follow(job_id: str, limit: float) -> dict:
        """wait_for_end. The files in the last view of the job are the drill's own, also when its end was not seen."""
        verdict, view = wait_for_end(job_id, limit)
        ends[job_id] = verdict
        made.extend(made_by(view))
        return view

    try:
        status, started, first = start(site, session, f"How does {site} work?")
        if not report.add(PASS if status == 200 and first else FAIL, "A teardown starts from one request",
                          teardown._clean(started.get("answer"), 120)):
            return
        ours.append(first)
        if job_now().get("state") == "running":         # ask for a second one only while the first still runs
            status, second, extra = start("example.org", session)
            if extra:                                   # it was let in: stop it, and count its files as ours
                ours.append(extra)
                cancel(extra)
                follow(extra, 60)
            report.add(PASS if status == 409 and "already taking one apart" in second.get("answer", "") else FAIL,
                       "A second request while one runs is refused in one line", teardown._clean(second.get("answer"), 120))
        else:
            report.add(FAIL, "A second request while one runs is refused in one line",
                       "Not checked: the first teardown ended before the second request could be sent.")

        job = follow(first, LIVE_LIMIT_S)
        result = job.get("result") or {}
        name = result.get("name")
        done = job.get("state") == "done" and bool(name)
        report.add(PASS if done else FAIL, "The teardown finishes and writes a note",
                   f"{result.get('label')} in {job.get('seconds')} s" if done
                   else f"state {job.get('state') or 'unknown'}: {(job.get('error') or {}).get('detail') or job.get('say') or 'no answer'}")
        if not done:
            return

        timeline = job.get("timeline") or []
        in_order = len(timeline) == len(teardown.STEPS) and all(isinstance(t, (int, float)) for t in timeline) \
            and timeline == sorted(timeline)
        report.add(PASS if in_order and job.get("steps") == list(teardown.STEPS) else FAIL,
                   "The panel showed all 8 steps, in order", " > ".join(f"{t}s" for t in timeline if t is not None))
        seconds = job.get("seconds") or 0
        report.add(PASS if 0 < seconds <= GOOD_TIME_S else FAIL, f"It took less than {GOOD_TIME_S} s", f"{seconds} s")

        _, note = call_json("GET", f"/teardown/note?name={quote(name)}")
        text = note.get("text", "")
        headings = {teardown._heading_key(h) for h in re.findall(r"^##\s+(.+?)\s*$", text, re.MULTILINE)}
        missing = [s for s in (*teardown.NOTE_SECTIONS, "Evidence") if teardown._heading_key(s) not in headings]
        linked = f"[[{teardown.CONCEPT_NAME}]]" in text and "REA evidence ids: " in text and teardown.CAUTION in text
        report.add(PASS if not missing and linked else FAIL,
                   "The note has every section, the evidence, the caution line, and the [[REA]] link",
                   f"missing: {missing}" if missing else ("" if linked else "the Evidence section or the caution line is not complete"))
        doubts = result.get("doubts") or []
        report.add(PASS if not doubts else FAIL, "The note names no version, file, address or command that the evidence does not hold",
                   f"not in the evidence: {doubts}" if doubts else "")

        spoken = result.get("spoken", "")
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", spoken) if s]
        # "Three.js" is a library name and may be said. An address, a path or a file is not for the ear.
        speakable = 0 < len(sentences) <= 4 and not teardown.risky_spoken(spoken) and not re.search(
            r"https?://|www\.|[/\\]|[*_#`]|\.(?:m?js|wasm|glb|gltf|ktx2|drc|png|jpe?g|ogg|mp[34]|css|json)\b(?<!Three\.js)(?<!\w\w\w\.js)"
            r"|\b[\w.-]+-[0-9a-f]{6,}\.\w+", spoken)
        report.add(PASS if speakable else FAIL, "The spoken line is short, asks for nothing, and has no addresses or file names",
                   preflight.short(spoken, 160))

        births = result.get("births") or []
        graph = build.read_graph()
        labels = [node.get("label") for node in graph.get("nodes", [])]
        born = len(labels) == nodes_before + len(births) and labels[-1:] == [name]
        rea_index = labels.index(teardown.CONCEPT_NAME) if teardown.CONCEPT_NAME in labels else -1
        joined = any({link["source"], link["target"]} == {rea_index, len(labels) - 1} for link in graph.get("links", []))
        _, found = call_json("GET", f"/search?q={quote(name)}")
        searchable = any(hit.get("label") == name for hit in found.get("results", []))
        report.add(PASS if born and joined and searchable else FAIL,
                   "The note is born in the galaxy, linked to REA, and found by search",
                   f"{len(births)} new nodes" if born and joined and searchable
                   else f"born {born}, linked to REA {joined}, found by search {searchable}")
        if keep is not None:
            keep.mkdir(parents=True, exist_ok=True)
            for source in (folder / f"{name}.md", folder / teardown.ASSETS_DIR / f"{name}.png"):
                if source.is_file():
                    shutil.copy2(source, keep / source.name)
        safety(report, name if result.get("snapshot") else None)
        nothing_left(report, "after the run")
        tree_after_run = tree(folder)

        # -- a job that is stopped half way must close its browser too
        status, started, third = start(site, session)
        if status == 200 and third:
            ours.append(third)
            time.sleep(4.0)
            cancelled = cancel(third)
            stopped = follow(third, 60)
            report.add(PASS if stopped.get("state") == "cancelled" and tree(folder) == tree_after_run else FAIL,
                       "A cancelled teardown stops and writes nothing",
                       f"state {stopped.get('state')}: {teardown._clean(cancelled.get('answer'), 80)}")
            time.sleep(1.0)
            nothing_left(report, "after a cancelled run")
        else:
            report.add(FAIL, "A cancelled teardown stops and writes nothing", f"it did not start: HTTP {status}")

        # -- a job that fails half way must close its browser too. Nothing answers at this address.
        failing = "A teardown of an address where nothing answers fails with a reason and writes nothing"
        status, started, fourth = start(f"localhost:{closed_port()}", session)
        if status == 200 and fourth:
            ours.append(fourth)
            failed = follow(fourth, 90)
            error = failed.get("error") or {}
            report.add(PASS if failed.get("state") == "failed" and error.get("code") == "site_silent"
                       and tree(folder) == tree_after_run else FAIL, failing,
                       f"state {failed.get('state')}: {teardown._clean(error.get('detail'), 90)}")
            time.sleep(1.0)
            nothing_left(report, "after a failed run")
        else:
            report.add(FAIL, failing, f"it did not start: HTTP {status}")
    finally:
        # -- put everything back, whatever happened above. Only the files of this drill's own jobs go.
        # The newest job of the drill is the only one that can still run: a later start was let in only after the
        # one before it ended. Its end must be CONFIRMED; a status request that fails is no end, and neither is
        # another job in its place. A job whose end was never seen may have made files after its last view.
        if ours and ends.get(ours[-1]) != ENDED:
            follow(ours[-1], 90)
        unseen = [job_id for job_id in ours if ends.get(job_id) != ENDED]
        clean, detail = put_back(before, list(dict.fromkeys(made)))
        if unseen:
            clean, detail = False, (f"the end of {', '.join(unseen)} was not seen, so the clean-up may be incomplete: "
                                    f"only the files in the last view of each job were removed. {detail}")
        report.add(PASS if clean else FAIL, "Everything is put back: the test note is gone and the galaxy is as before", detail)


def main(argv: list[str]) -> int:
    global VISIBLE
    VISIBLE = "--visible" in argv
    report = Report()
    site = argv[argv.index("--site") + 1] if "--site" in argv[:-1] else "example.com"
    keep = Path(argv[argv.index("--keep") + 1]).resolve() if "--keep" in argv[:-1] else None
    print(f"Jarviz teardown drill  {time.strftime('%Y-%m-%d %H:%M:%S')}\n\nOffline checks")
    offline(report)
    if "--offline" not in argv:
        print(f"\nLive drill: one {'visible' if VISIBLE else 'hidden'} teardown of {site}")
        if teardown.parse_site(site) is None:
            report.add(FAIL, "The drill site is a website address", site)
        elif not preflight.check_server(report):
            pass
        else:
            _, kit = call_json("GET", "/teardown/diag")
            try:
                running = job_now().get("state") == "running"
                unread = ""
            except SERVER_GONE as error:        # a status that cannot be read is not "nothing runs"
                running, unread = True, f"The teardown status could not be read ({error})."
            ready = kit.get("ready") and kit.get("rea", {}).get("installed") == teardown.REA_PIN and not running
            if report.add(PASS if ready else FAIL, f"The kit is ready with REA {teardown.REA_PIN}",
                          unread or ("A teardown is running now. Wait for it." if running
                                     else "; ".join(kit.get("problems") or [])
                                     or f"Node.js {kit.get('node', {}).get('version')}, Chrome found")):
                try:
                    live(report, site, keep)
                except SERVER_GONE as error:
                    report.add(FAIL, "The server stays up during the drill", f"It stopped answering ({error}).")
    print("\n" + report.summary())
    return 1 if report.counts[FAIL] else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main(sys.argv))
