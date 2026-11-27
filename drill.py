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

import contextlib
import errno
import hashlib
import http.client
import io
import json
import os
import re
import secrets
import shutil
import socket
import stat
import sys
import tempfile
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
    A view of our job with a missing or unknown state is no answer: it is tried again like one. When the silence
    budget and the job's time run out together, the named cancel still goes out, and the drill looks for the end as
    after any cancel: until the wait after it ends, with a new silence budget. Another job's id (or none) still ends
    the look at once."""
    job, cancelled, silent_since = {}, False, None
    deadline = clock() + limit

    def send_cancel() -> float:
        """The cancel of OUR job (by its id), with the wait after it as its budget. The end of that wait."""
        until = clock() + CANCEL_WAIT_S             # the wait after the cancel starts at its send
        try:
            ask_cancel(request_timeout(until - clock()))
        except SERVER_GONE:
            pass                        # the next answers say whether it ended
        return until

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
                if cancelled or clock() < deadline:
                    return NOT_CONFIRMED, job
                # Its time ran out in the silence: it must not run on unasked. Then the look for its end goes on.
                cancelled, deadline, silent_since = True, send_cancel(), None
        if view is not None:
            if view.get("id") != job_id:
                return NOT_CONFIRMED, job       # our job is gone: its end was never seen, and its last view counts
            job = view
            if view.get("state") in END_STATES:
                return ENDED, view
        if clock() >= deadline:
            if cancelled:
                return NOT_CONFIRMED, job
            cancelled, deadline = True, send_cancel()
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
    stopped = {"id": "job1", "state": "cancelled"}
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
              (NOT_CONFIRMED, {}, 0)),
             # Polls every 0.5 s until 10 s; the request sent then hangs for the whole 10 s left: the silence budget and
             # the job's time end together. The named cancel still goes out before the drill gives up.
             ("silence that ends with the job's time still sends the named cancel", [good(running)] * 20 + ["hang"], 20,
              (NOT_CONFIRMED, running, 1)),
             ("silence that ends before the job's time sends no cancel", [good(running)] * 4 + ["hang"], 20,
              (NOT_CONFIRMED, running, 0)),
             # After that cancel the drill still looks for the end, as after any cancel: a new silence budget, until the
             # wait after the cancel ends. Another job's id, or none, still ends the look at once.
             ("a cancel sent in the silence, then the job's end, is a confirmed end",
              [good(running)] * 20 + ["hang", good(stopped)], 20, (ENDED, stopped, 1)),
             ("a cancel sent in the silence, then another job's id, is not a confirmed end, with our last view",
              [good(running)] * 20 + ["hang", good(other)], 20, (NOT_CONFIRMED, running, 1)),
             ("a cancel sent in the silence, then no job, is not a confirmed end, with our last view",
              [good(running)] * 20 + ["hang", good(None)], 20, (NOT_CONFIRMED, running, 1)),
             ("a cancel sent in the silence, then still running through the wait, is not a confirmed end",
              [good(running)] * 20 + ["hang", good(running)], 20, (NOT_CONFIRMED, running, 1))]
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
    # The cancel sent in the silence: the look goes on for the wait after it (and no longer), or ends at once when
    # another job answers.
    timing = {}
    for label, last in (("running", good(running)), ("other", good(other))):
        verdict, _, cancels, ended, requests = follow([good(running)] * 20 + ["hang", last], 20)
        sent_cancel = [sent for what, sent, _ in requests if what == "cancel"]
        timing[label] = (verdict, cancels, round(ended - sent_cancel[0], 1) if sent_cancel else None,
                         sum(1 for what, sent, _ in requests if what == "status" and sent_cancel and sent > sent_cancel[0]))
    results.append(("drill: after a cancel sent in the silence the look lasts the wait after it; another job ends it at once",
                    timing["running"][:2] == (NOT_CONFIRMED, 1) and timing["running"][2] is not None
                    and CANCEL_WAIT_S <= timing["running"][2] <= CANCEL_WAIT_S + POLL_S + REQUEST_FLOOR_S
                    and timing["other"][:2] == (NOT_CONFIRMED, 1) and timing["other"][3] == 1, str(timing)))
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


def link_checks(notes: Path, ours: str, place) -> list[tuple[str, bool, str]]:
    """Offline, in the temp notes folder of cleanup_checks: remove_made deletes only the entry that its receipt names,
    and a note only with the bytes the test wrote, read from the very file that goes. `place(relative, text)` writes
    a file there and gives (relative, born). Every link it makes is removed again (never what it points to)."""
    results: list[tuple[str, bool, str]] = []
    links: list[Path] = []
    text = "the test note"

    def link(at: Path, to: Path) -> str:
        """A link at `at` to `to`, and its kind. On Windows a folder gets a junction (it needs no privilege) and a file
        a symlink, or a junction to the file's folder when this account may not make one: a link at the name too."""
        at.parent.mkdir(parents=True, exist_ok=True)
        links.append(at)
        if os.name == "nt":
            import _winapi
            if not to.is_dir():
                try:
                    os.symlink(to, at)
                    return "symlink"
                except OSError:
                    to = to.parent
            _winapi.CreateJunction(str(to), str(at))
            return "junction"
        os.symlink(to, at, target_is_directory=to.is_dir())
        return "symlink"

    def write_in_place(path: Path, data: bytes) -> None:
        """Write `data` over the start of `path`. On Windows as the most open editor does: it lets others read, write
        and delete the file meanwhile (FILE_SHARE_READ | WRITE | DELETE), so only the drill's own handle can refuse it."""
        if os.name != "nt":
            with open(path, "r+b") as handle:
                handle.write(data)
            return
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.WriteFile.argtypes = (wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                     wintypes.LPVOID)
        kernel.WriteFile.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.CreateFileW(str(path), 0x40000000, 0x7, None, WIN_OPEN_EXISTING, 0, None)   # GENERIC_WRITE
        if handle in (None, wintypes.HANDLE(-1).value):
            raise PermissionError(ctypes.get_last_error(), "the file is held")
        try:
            written = wintypes.DWORD()
            if not kernel.WriteFile(handle, data, len(data), ctypes.byref(written), None):
                raise OSError(ctypes.get_last_error(), "the write failed")
        finally:
            kernel.CloseHandle(handle)

    try:
        # The name of the note became a link to another note in the vault with the very same bytes.
        label = "drill: a note whose name became a link to a note with the same bytes: the link and the note stay"
        other, born = place("captures/user-copy.md", text)
        named = f"{teardown.TEARDOWNS_DIR}/t3.md"
        try:
            kind = link(notes / named, notes / other)
        except OSError as error:
            results.append((label, False, f"could not look: no link could be made ({type(error).__name__})"))
        else:
            trouble = remove_made(notes, ours, [(named, ours, born)])
            results.append((label, len(trouble) == 1 and (notes / other).read_bytes() == text.encode("utf-8")
                            and os.path.lexists(notes / named), f"{kind}: {trouble}"))
        # A folder on the way is a link to another folder of the vault, which holds a note with the same bytes.
        label = "drill: a note reached through a linked folder stays, and so does the note behind the link"
        side = notes / "side"
        behind, born = place("side/elsewhere/t3.md", text)
        try:
            kind = link(side / teardown.TEARDOWNS_DIR, side / "elsewhere")
        except OSError as error:
            results.append((label, False, f"could not look: no link could be made ({type(error).__name__})"))
        else:
            trouble = remove_made(side, ours, [(f"{teardown.TEARDOWNS_DIR}/t3.md", ours, born)])
            results.append((label, len(trouble) == 1 and (notes / behind).exists(), f"{kind}: {trouble}"))
        # An editor saves the note while the drill removes it: in place, or by a new file put over it.
        for how in ("in place", "by a replace"):
            named, born = place(f"{teardown.TEARDOWNS_DIR}/t4.md", text)
            path, saved = notes / named, []

            def save(path=path, how=how, saved=saved) -> None:
                try:
                    if how == "in place":
                        write_in_place(path, b"the user's edit")
                    else:
                        fresh = path.with_name("t4.saving")
                        fresh.write_bytes(b"the user's edit")
                        os.replace(fresh, path)
                    saved.append("saved")
                except OSError:
                    saved.append("refused")
            trouble = remove_made(notes, ours, [(named, ours, born)], between=save)
            kept = path.read_bytes() if path.exists() else None
            hidden = [entry.name for entry in path.parent.iterdir() if entry.name.endswith(".drill-gone")]
            # Windows: the handle shuts every writer out, and the unchanged note goes. POSIX: the save goes through,
            # the second read through the same fd sees it, and the note stays with the change.
            fine = (saved == ["refused"] and not trouble and kept is None if os.name == "nt"
                    else saved == ["saved"] and len(trouble) == 1 and kept == b"the user's edit")
            results.append((f"drill: an editor that saves {how} while the drill removes the note loses nothing",
                            fine and not hidden, f"save {saved}, trouble {trouble}, left {kept!r}, hidden {hidden}"))
            path.with_name("t4.saving").unlink(missing_ok=True)
        # A program that held the note open writes in the instant between the second check and the unlink (`last`).
        # Windows: the handle shuts every writer out, and the unchanged note goes. POSIX: the write goes through, the
        # read after the unlink sees it, and what the note holds goes back to its name, or beside it when another
        # file took the name in that instant, never over a file, and the clean-up is not confirmed.
        late = b" and a write in the last instant"
        for taken in (False,) if os.name == "nt" else (False, True):
            named, born = place(f"{teardown.TEARDOWNS_DIR}/t7.md", text)
            path, saved = notes / named, []
            writer = None if os.name == "nt" else os.open(path, os.O_WRONLY | os.O_APPEND)

            def write(path=path, writer=writer, taken=taken, saved=saved) -> None:
                try:
                    if writer is None:
                        write_in_place(path, b"the user's edit")
                    else:
                        os.write(writer, late)
                        if taken:
                            path.write_bytes(b"another file")
                    saved.append("saved")
                except OSError:
                    saved.append("refused")
            try:
                trouble = remove_made(notes, ours, [(named, ours, born)], last=write)
            finally:
                if writer is not None:
                    os.close(writer)
            kept = path.read_bytes() if path.exists() else None
            beside = [entry for entry in path.parent.iterdir() if entry.name.endswith(".drill-kept")]
            hidden = [entry.name for entry in path.parent.iterdir() if entry.name.endswith(".drill-gone")]
            if os.name == "nt":
                fine = saved == ["refused"] and not trouble and kept is None and not beside
            elif taken:
                fine = (saved == ["saved"] and len(trouble) == 1 and kept == b"another file" and len(beside) == 1
                        and beside[0].read_bytes() == text.encode("utf-8") + late and beside[0].name in trouble[0])
            else:
                fine = (saved == ["saved"] and len(trouble) == 1 and f"written back to {named}" in trouble[0]
                        and kept == text.encode("utf-8") + late and not beside)
            results.append(("drill: a write in the instant before the delete is never lost"
                            + (", also when another file took the name" if taken else ""),
                            fine and not hidden, f"write {saved}, trouble {trouble}, left {kept!r}, kept {beside}"))
            for entry in beside:
                entry.unlink()
            path.unlink(missing_ok=True)
        # POSIX: the write in that instant makes the note bigger than the read cap. It is not `born`, and the copy is
        # streamed from the held fd with no cap, over more than one piece: every byte goes back to its name. A small
        # cap keeps the check fast.
        if os.name != "nt":
            named, born = place(f"{teardown.TEARDOWNS_DIR}/t8.md", text)
            path, saved, module, cap = notes / named, [], globals(), 4096
            huge = bytes(range(256)) * 800                  # 200 KiB: 50 times the cap, and more than 3 read pieces
            writer = os.open(path, os.O_WRONLY | os.O_APPEND)

            def grow(writer=writer, saved=saved) -> None:
                try:
                    view = memoryview(huge)
                    while view:
                        view = view[os.write(writer, view):]
                    saved.append("saved")
                except OSError:
                    saved.append("refused")
            real_cap = module["HELD_MAX_BYTES"]
            module["HELD_MAX_BYTES"] = cap
            try:
                trouble = remove_made(notes, ours, [(named, ours, born)], last=grow)
            finally:
                module["HELD_MAX_BYTES"] = real_cap
                os.close(writer)
            kept = path.read_bytes() if path.exists() else None
            beside = [entry for entry in path.parent.iterdir() if entry.name.endswith(".drill-kept")]
            hidden = [entry.name for entry in path.parent.iterdir() if entry.name.endswith(".drill-gone")]
            results.append(("drill: a write in the instant before the delete that makes the note bigger than the read "
                            "cap is never lost, and the kept copy holds every byte",
                            saved == ["saved"] and len(trouble) == 1 and f"written back to {named}" in trouble[0]
                            and kept == text.encode("utf-8") + huge and not beside and not hidden,
                            f"write {saved}, trouble {trouble}, left {None if kept is None else len(kept)} bytes, "
                            f"kept {beside}, hidden {hidden}"))
            for entry in beside:
                entry.unlink()
            path.unlink(missing_ok=True)
        # One receipt whose path cannot be looked at is trouble for it alone; a notes folder that cannot be looked at
        # removes nothing, and the clean-up is not confirmed.
        first, first_born = place(f"{teardown.TEARDOWNS_DIR}/t5.md", text)
        later, later_born = place(f"{teardown.TEARDOWNS_DIR}/t6.md", "another test note")
        module, real = globals(), delete_made_file

        def refusing(root, target, born, between=None, last=None):
            if target.name == "t5.md":
                raise PermissionError(13, "Permission denied", str(target))
            return real(root, target, born, between, last)
        module["delete_made_file"] = refusing
        try:
            trouble = remove_made(notes, ours, [(first, ours, first_born), (later, ours, later_born)])
        finally:
            module["delete_made_file"] = real
        unusable = remove_made(notes / "not-there", ours, [(first, ours, first_born)])
        results.append(("drill: a path that cannot be looked at is trouble for its receipt alone; an unusable notes "
                        "folder removes nothing and is not confirmed",
                        len(trouble) == 1 and "t5.md" in trouble[0] and (notes / first).exists()
                        and not (notes / later).exists() and len(unusable) == 1 and "not confirmed" in unusable[0],
                        f"{trouble} {unusable}"))
    finally:
        for at in reversed(links):          # the link itself, never what it points to
            try:
                os.rmdir(at)                # a junction, or a link to a folder
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(at)
    return results


def cleanup_checks() -> list[tuple[str, bool, str]]:
    """Offline: made_by and remove_made with made-up jobs in a temp folder, and the note scan that put_back builds
    the galaxy from. No server."""
    results = []
    ours, elsewhere = "vault-of-the-drill", "vault-of-another-folder"
    with tempfile.TemporaryDirectory(prefix="jarviz-drill-") as base:
        notes = Path(base)

        def place(relative: str, text: str) -> tuple[str, str]:
            path = notes / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
            return relative, hashlib.sha256(text.encode("utf-8")).hexdigest()

        def job(name: str, *births: dict) -> dict:
            return {"state": "done", "result": {"name": name, "snapshot": f"{name}.png"}, "births": list(births)}

        note, born = place(f"{teardown.TEARDOWNS_DIR}/t1.md", "the test note")
        picture, _ = place(f"{teardown.TEARDOWNS_DIR}/{teardown.ASSETS_DIR}/t1.png", "png")
        concept, concept_born = place(teardown.CONCEPT_PATH, "the REA note")
        made = made_by(job("t1", {"file": note, "vault": ours, "born": born},
                           {"file": concept, "vault": ours, "born": concept_born}))
        results.append(("drill: made_by keeps the vault and the bytes of each file, and the picture its note's vault",
                        made == [(note, ours, born), (concept, ours, concept_born), (picture, ours, "")], str(made)))
        # The receipt of the teardown note counts in every state: a running view has it before `result` is there, and
        # a failed job keeps it when the note could not be taken back. The picture needs `result` (its name).
        primary = {"file": note, "vault": ours, "born": born}
        seen = {state: made_by({"state": state, "result": None, "births": [primary]})
                for state in ("running", "failed", "cancelled", "done")}
        strangers = made_by({"state": "running", "births": [
            {"file": "captures/other.md", "vault": ours, "born": born},
            {"file": f"{teardown.TEARDOWNS_DIR}/{teardown.ASSETS_DIR}/t1.md", "vault": ours, "born": born},
            {"file": f"{teardown.TEARDOWNS_DIR}/CON.md", "vault": ours, "born": born}, {"file": None}, "not a receipt"]})
        results.append(("drill: made_by reads the teardown note from the births in every state, and no other file",
                        all(found == [(note, ours, born)] for found in seen.values()) and strangers == [],
                        f"{seen} {strangers}"))
        trouble = remove_made(notes, ours, made)
        results.append(("drill: the test's own files in the drill's notes folder are removed",
                        not trouble and not any((notes / relative).exists() for relative, _, _ in made), str(trouble)))
        # The galaxy moved to another folder during the drill: the job chose the name t2 there. A note and a picture
        # of that name in the drill's own folder are NOT the test's.
        user_note, user_born = place(f"{teardown.TEARDOWNS_DIR}/t2.md", "the user's own note")
        user_picture, _ = place(f"{teardown.TEARDOWNS_DIR}/{teardown.ASSETS_DIR}/t2.png", "the user's own picture")
        user_concept, _ = place(teardown.CONCEPT_PATH, "the user's own REA note")
        moved = made_by(job("t2", {"file": user_note, "vault": elsewhere, "born": user_born},
                            {"file": user_concept, "vault": elsewhere, "born": "0" * 64}))
        trouble = remove_made(notes, ours, moved)
        results.append(("drill: files made in another notes folder are never removed here, and the clean-up says so",
                        len(trouble) == 3 and all((notes / relative).exists() for relative in (user_note, user_picture,
                                                                                               user_concept)),
                        str(trouble)))
        unnamed = made_by(job("t2", {"file": user_note, "born": user_born}))     # a receipt that names no folder
        changed = [(user_note, ours, hashlib.sha256(b"what the test wrote").hexdigest())]
        trouble = remove_made(notes, ours, unnamed) + remove_made(notes, ours, changed) + remove_made(notes, "", changed)
        results.append(("drill: a receipt with no folder, a note whose bytes differ, or no recorded folder: nothing goes",
                        len(trouble) == 4 and (notes / user_note).exists() and (notes / user_picture).exists(),
                        str(trouble)))
        results += link_checks(notes, ours, place)
        # put_back builds the galaxy from build.note_files: a note that cannot be looked at is said, never hidden
        # (Path.is_file says False in silence since Python 3.14).
        real_stat, said = os.stat, io.StringIO()
        blocked = str(notes / user_note)

        def refusing(path, *args, **kwargs):
            if os.fspath(path) == blocked:
                raise PermissionError(13, "Permission denied", blocked)
            return real_stat(path, *args, **kwargs)
        os.stat = refusing
        try:
            with contextlib.redirect_stderr(said):
                looked = build._regular_file(Path(blocked))
        finally:
            os.stat = real_stat
        results.append(("build: a note that cannot be looked at is skipped and said; a plain file is a note",
                        looked is False and "Skipped t2.md" in said.getvalue()
                        and build._regular_file(notes / user_picture) and not build._regular_file(notes),
                        f"{looked} {said.getvalue().strip()!r}"))
    return results


def offline(report: Report) -> None:
    for name, passed, detail in teardown.offline_checks() + follow_checks() + cleanup_checks():
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
    """How things are before a test teardown: the notes and links in the galaxy, and the teardowns folder. "vault":
    the fingerprint of the notes folder (server.vault_of), as a receipt names it."""
    notes_dir = build.resolve_notes_dir(server.load_config()["notes_dir"])
    folder = notes_dir / teardown.TEARDOWNS_DIR
    watched = (notes_dir / "captures", folder, folder / teardown.ASSETS_DIR)
    graph = build.read_graph()
    return {"notes_dir": str(notes_dir), "vault": server.vault_of(notes_dir),
            "labels": [node.get("label") for node in graph.get("nodes", [])],
            "shape": shape(graph), "tree": tree(folder), "folders": {str(path): path.is_dir() for path in watched}}


def closed_port() -> int:
    """A port on this computer where nothing answers."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def made_by(job: dict) -> list[tuple[str, str, str]]:
    """The files one test teardown made: (the path inside the notes folder, the "vault" it was written in, the
    "born" fingerprint of its bytes), from the job's birth receipts. "" where a receipt gives none; the picture has
    no receipt of its own: it is in the folder of its note, and has no "born". Nothing else is ever removed.
    The notes come from the births in EVERY state: a job that still runs, or that failed and could not take a note
    back, holds the receipt of its teardown note while `result` is not there (yet). The picture's name is known only
    from `result`, so the picture goes only with a done job."""
    result = job.get("result") or {}
    name = result.get("name")
    births = [birth for birth in (job.get("births") or result.get("births") or []) if isinstance(birth, dict)]

    def receipt(file: str) -> tuple[str, str]:
        birth = next((birth for birth in births if birth.get("file") == file), {})
        return tuple(value if isinstance(value, str) else "" for value in (birth.get("vault"), birth.get("born")))

    def teardown_note(file) -> bool:
        folder, _, base = file.rpartition("/") if isinstance(file, str) else ("", "", "")
        return folder == teardown.TEARDOWNS_DIR and base.endswith(".md") and teardown.valid_name(base[:-3])

    files = []
    for birth in births:        # the teardown note, the REA note or the Jarviz note: only when THIS job made it
        file = birth.get("file")
        if (teardown_note(file) or file in (teardown.CONCEPT_PATH, teardown.OWNER_PATH)) and file not in (
                made for made, _, _ in files):
            files.append((file, *receipt(file)))
    if job.get("state") == "done" and teardown.valid_name(name):
        note = f"{teardown.TEARDOWNS_DIR}/{name}.md"
        vault, born = receipt(note)
        if note not in (made for made, _, _ in files):
            files.append((note, vault, born))       # a done job with no receipt for its note: put_back says so
        if result.get("snapshot"):
            files.append((f"{teardown.TEARDOWNS_DIR}/{teardown.ASSETS_DIR}/{name}.png", vault, ""))
    return files


# -- the delete of a file of the test, tied to the very file that was checked (on Windows as preflight's
# delete_fixture_file; on POSIX preflight deletes nothing by hand, and the drill writes back a last-instant change)
# Windows (kernel32 by ctypes): the access, the share, the flags and the info classes that _handle_delete uses.
WIN_READ_AND_DELETE = 0x80000000 | 0x00010000       # GENERIC_READ | DELETE
WIN_SHARE_READ = 0x1                                # others may read it, never write, rename or delete it meanwhile
WIN_OPEN_EXISTING, WIN_OPEN_REPARSE_POINT = 3, 0x00200000   # the entry itself, never what a link there points to
WIN_ATTRIBUTE_TAG_INFO, WIN_DISPOSITION_INFO = 9, 4
WIN_DIRECTORY, WIN_REPARSE_POINT = 0x10, 0x400
WIN_NOT_FOUND = (2, 3)                              # ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND
KEEPS = {"linked": "is reached through a link or a junction",
         "not_plain": "is not a plain file (a link or a folder is at its name)",
         "changed": "is not the file the test wrote (its bytes differ, or another file took its place)",
         "moved": "is in a folder that was swapped for another after it was checked"}


class CleanupUnsure(Exception):
    """The delete stopped half way. The text says where things are now, by their paths in the notes folder."""


def _bytes_are(read, born: str, most: int | None = None) -> bool:
    """True when the bytes that `read()` gives, piece by piece until b"", have the sha256 `born`. More than `most`
    bytes never do (None: no cap)."""
    digest, size = hashlib.sha256(), 0
    while piece := read():
        size += len(piece)
        if most is not None and size > most:
            return False
        digest.update(piece)
    return digest.hexdigest() == born


def _handle_delete(target: Path, born: str | None, between=None, last=None) -> tuple[bool | None, str]:
    """Windows: delete a file of the test through ONE handle on the entry itself. It is opened with no link followed
    at the entry, and while it is held nobody may write, rename or delete the file: an editor that saves meanwhile is
    refused (its text stays in the editor), never deleted. Its real path must be `target` itself (a link or a junction
    on the way shows there), it must be no folder and no reparse point, and for a note its bytes, read through the
    handle, must be `born` (None: the picture, which has none). Then the delete goes through the same handle.
    (True, ""), (False, "linked" | "not_plain" | "changed"), or (None, why there is no safe delete); FileNotFoundError
    when nothing is there. `between`, then `last`: a test runs them after the checks, right before the delete."""
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.GetFinalPathNameByHandleW.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
        kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        for call_name in ("GetFileInformationByHandleEx", "SetFileInformationByHandle"):
            getattr(kernel, call_name).argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
            getattr(kernel, call_name).restype = wintypes.BOOL
        kernel.ReadFile.argtypes = (wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                    wintypes.LPVOID)
        kernel.ReadFile.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
    except (ImportError, OSError, AttributeError) as error:
        return None, f"kernel32 could not be used ({type(error).__name__})"
    handle = kernel.CreateFileW(str(target), WIN_READ_AND_DELETE, WIN_SHARE_READ, None, WIN_OPEN_EXISTING,
                                WIN_OPEN_REPARSE_POINT, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        code = ctypes.get_last_error()
        if code in WIN_NOT_FOUND:
            raise FileNotFoundError(code, "not found")
        return None, f"it could not be opened for a delete (WinError {code})"
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not 0 < size < len(buffer):
            return None, f"its real place could not be read (WinError {ctypes.get_last_error()})"
        real = buffer.value
        real = "\\\\" + real[8:] if real.startswith("\\\\?\\UNC\\") else real[4:] if real.startswith("\\\\?\\") else real
        if os.path.normcase(real) != os.path.normcase(str(target)):
            return False, "linked"
        tag = (wintypes.DWORD * 2)()                   # FILE_ATTRIBUTE_TAG_INFO: the attributes, the reparse tag
        if not kernel.GetFileInformationByHandleEx(handle, WIN_ATTRIBUTE_TAG_INFO, tag, ctypes.sizeof(tag)):
            return None, f"its kind could not be read (WinError {ctypes.get_last_error()})"
        if tag[0] & (WIN_DIRECTORY | WIN_REPARSE_POINT):
            return False, "not_plain"
        if born is not None:
            chunk, got, failed = ctypes.create_string_buffer(65536), wintypes.DWORD(), []

            def read() -> bytes:
                if not kernel.ReadFile(handle, chunk, len(chunk), ctypes.byref(got), None):
                    failed.append(ctypes.get_last_error())
                    return b""
                return chunk.raw[:got.value]

            same = _bytes_are(read, born)
            if failed:
                return None, f"its bytes could not be read (WinError {failed[0]})"
            if not same:
                return False, "changed"
        for hook in (between, last):
            if hook:
                hook()
        delete = ctypes.c_ubyte(1)                      # FILE_DISPOSITION_INFO: DeleteFile = TRUE
        if not kernel.SetFileInformationByHandle(handle, WIN_DISPOSITION_INFO, ctypes.byref(delete), 1):
            return None, f"the delete was refused (WinError {ctypes.get_last_error()})"
    finally:
        kernel.CloseHandle(handle)
    return True, ""


def _put_back(folder: int, hidden: str, name: str) -> bool:
    """Move what sits at `hidden` back to `name` (both in the open `folder`), never over another file: only a hard link
    does that in one step. Where there is none, it stays at the hidden name. True when it is back."""
    try:
        os.link(hidden, name, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
    except (OSError, NotImplementedError):
        return False                                    # the name is taken, or no link here: it stays hidden
    os.unlink(hidden, dir_fd=folder)
    return True


# What a note held in the instant of its delete goes back, whole: streamed from the held fd into a new file.
HELD_MAX_BYTES = 1 << 26           # after the delete, the held file is compared with `born` up to this much only: a
                                   # file that holds more is never `born`, so it is kept, never removed (no cap on it)


def _copy_held(handle: int, out: int) -> None:
    """Stream ALL that the open file `handle` holds now, from its start, into the open file `out`, one piece at a time:
    no cap, and never more than one piece in memory. OSError: a read or a write failed."""
    os.lseek(handle, 0, os.SEEK_SET)
    while piece := os.read(handle, 65536):
        view = memoryview(piece)
        while view:
            view = view[os.write(out, view):]


def _save_held(handle: int, places: list) -> tuple[str | None, list[str]]:
    """Save what the held test note `handle` holds (it changed in the instant of its delete) in the first place that
    takes it, streamed whole (_copy_held). A place is (an open folder, or None for a name that is a whole path; the
    name; the mode; how that name is told). Every file is NEW (O_EXCL, no link followed): never over a file. A place
    that refuses, or whose name is taken, gives the next its turn, and the next copy starts again at the first byte. A
    copy that failed half way stays, and is told. (where it went, what failed before) or (None, all)."""
    failed: list[str] = []
    for folder, name, mode, told in places:
        try:
            out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=folder)
        except OSError as error:
            failed.append(f"not at {told}: " + ("the name is taken" if isinstance(error, FileExistsError)
                                                 else error.strerror or type(error).__name__))
            continue
        broke = None
        try:
            _copy_held(handle, out)
        except OSError as error:
            broke = error
        try:
            os.close(out)
        except OSError as error:
            broke = broke or error
        if broke is None:
            return told, failed
        failed.append(f"not at {told}: {broke.strerror or type(broke).__name__}, and an incomplete copy is there")
    return None, failed


def _change_kept(where: str, handle: int, places: list) -> str:
    """What CleanupUnsure says when the test note at `where`, held open as `handle`, held other bytes in the instant
    of its delete: where they were saved (_save_held, `places` in order), or loudly that they were not."""
    kept, failed = _save_held(handle, places)
    if kept is None:
        return (f"the test note was changed in the instant before its delete and could not be saved (it was at {where}; "
                f"{'; '.join(failed)}): the change made in that instant is lost")
    return (f"the test note at {where} was changed in the instant before its delete, and what it held then was "
            + (f"written back to {kept}" if kept == where else f"saved at {kept}")
            + (f" ({'; '.join(failed)})" if failed else "") + ": look at it, and remove it by hand")


def _folder_delete(root: Path, target: Path, born: str | None, between=None, last=None) -> tuple[bool | None, str]:
    """POSIX: delete a file of the test in folders that stay open.
    1. The vault and each folder below it are checked (lstat: a real folder, no link); their identity is kept.
    2. Every folder from "/" down is opened with O_NOFOLLOW: no link anywhere on the way is followed. The vault and
       each folder below must be the very one that was checked.
    3. The file is opened in the last folder with no link followed; for a note its bytes must be `born` (None: the
       picture). That fd stays open to the end.
    4. It is moved to a new random hidden name in the same folder. 5. What sits there must be the file of that fd, and
       a note's bytes, read again through that fd, must still be `born` (an editor that saved in place after the first
       read shows here). Else it is put back, never over a file, and CleanupUnsure says where things are. 6. The
       hidden name is unlinked. 7. A note's bytes are read once more through the fd: POSIX cannot refuse a write of
       a program that opened the file BEFORE the move, but a write in the instant before the unlink shows there (a
       note that holds more than HELD_MAX_BYTES by then is not `born`). Then all that the note holds is streamed back
       from the fd, with no cap, never over a file: at its name, else at a kept name beside it, at the top of the
       notes folder, in the temp folder (_save_held). And CleanupUnsure names that place, or says that it could not
       be saved. A read that fails there is no success either.
    What remains: such a program can write into the file after the unlink, into a file that has no name any more, as
    after any delete. The same answers as _handle_delete, and "moved". `between`: a test runs it after the first
    check, right before the move; `last`: after the second check, right before the unlink."""
    parts = target.relative_to(root).parts
    inner, name = parts[:-1], parts[-1]
    checked, step = [], root
    for part in (None, *inner):
        step = step if part is None else step / part
        seen = step.lstat()
        if stat.S_ISLNK(seen.st_mode) or not stat.S_ISDIR(seen.st_mode):
            return False, "linked"
        checked.append((seen.st_dev, seen.st_ino))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fds = [os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)]
    try:
        for part in root.parts[1:]:
            fds.append(os.open(part, flags, dir_fd=fds[-1]))
        vault_at = len(fds) - 1
        for part in inner:
            fds.append(os.open(part, flags, dir_fd=fds[-1]))
        if [(found.st_dev, found.st_ino) for found in map(os.fstat, fds[vault_at:])] != checked:
            return False, "moved"                       # another folder than the one checked
        folder = fds[-1]
        seen = os.stat(name, dir_fd=folder, follow_symlinks=False)
        if not stat.S_ISREG(seen.st_mode):
            return False, "not_plain"
        fds.append(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=folder))
        handle = fds[-1]
        opened = os.fstat(handle)

        def unchanged() -> bool:
            """The bytes of the file of the fd, read from its start: still `born`."""
            os.lseek(handle, 0, os.SEEK_SET)
            return born is None or _bytes_are(lambda: os.read(handle, 65536), born)

        if ((opened.st_dev, opened.st_ino) != (seen.st_dev, seen.st_ino) or not stat.S_ISREG(opened.st_mode)
                or not unchanged()):
            return False, "changed"
        if between:
            between()
        hidden = f".{secrets.token_hex(16)}.drill-gone"         # new and random: nobody can have made it before
        try:
            os.stat(hidden, dir_fd=folder, follow_symlinks=False)
            return None, "a new hidden name was taken"
        except FileNotFoundError:
            pass
        os.rename(name, hidden, src_dir_fd=folder, dst_dir_fd=folder)
        where, hidden_at = "/".join((*inner, name)), "/".join((*inner, hidden))
        try:
            now = os.stat(hidden, dir_fd=folder, follow_symlinks=False)
        except OSError:
            now = None
        mine = (now is not None and stat.S_ISREG(now.st_mode)
                and (now.st_dev, now.st_ino) == (opened.st_dev, opened.st_ino))
        try:
            same = mine and unchanged()
        except OSError:
            same = None                                 # it could not be read again: it is not deleted
        if same:
            if last:
                last()
            try:
                os.unlink(hidden, dir_fd=folder)
            except OSError as error:
                trouble, advice = (f"the file at {where} could not be removed after it was moved for the delete "
                                   f"({error.strerror or type(error).__name__})"), "remove it by hand"
            else:
                if born is None:                        # the picture: it has no bytes to compare
                    return True, ""
                try:                                    # a write before the unlink shows in the file that is held
                    os.lseek(handle, 0, os.SEEK_SET)
                    if _bytes_are(lambda: os.read(handle, 65536), born, HELD_MAX_BYTES):
                        return True, ""
                except OSError as error:
                    raise CleanupUnsure(f"the test note at {where} was removed, but the note could not be read again "
                                        f"after the delete ({error.strerror or type(error).__name__}), so whether a "
                                        "write came in that instant is not known") from None
                kept = f".{secrets.token_hex(16)}.drill-kept"
                places = [(folder, name, stat.S_IMODE(opened.st_mode), where),
                          (folder, kept, 0o600, "/".join((*inner, kept)))]
                if inner:
                    places.append((fds[vault_at], kept, 0o600, f"{kept} at the top of the notes folder"))
                try:
                    spare = os.path.join(tempfile.gettempdir(), f"jarviz-{secrets.token_hex(16)}.drill-kept")
                except OSError:
                    spare = ""                          # no temp folder: os.open("") fails, and that is told
                places.append((None, spare, 0o600, spare or "the temp folder"))
                raise CleanupUnsure(_change_kept(where, handle, places))
        elif mine and same is False:
            trouble, advice = f"the file at {where} was changed right before the delete (it was saved meanwhile)", ""
        elif mine or now is None:
            trouble = f"the file at {where} could not be checked again after it was moved for the delete"
            advice = "look at it before you remove it by hand"
        else:
            trouble = f"another file took the place of the file at {where} right before the delete"
            advice = "where the test's file went is not known"
        os.close(fds.pop())                             # the file's fd: what sits at the hidden name goes back
        try:
            back = _put_back(folder, hidden, name)
        except OSError:
            back = False
        if not advice:                                  # the changed file: it is someone's now, and it stays
            advice = "it stays, with the change" if back else "give it its name back by hand: it holds the change"
        raise CleanupUnsure(f"{trouble}, and {'it was put back there' if back else f'it was left at {hidden_at}'}: {advice}")
    finally:
        for fd in reversed(fds):
            os.close(fd)


def delete_made_file(root: Path, target: Path, born: str | None, between=None, last=None) -> tuple[bool | None, str]:
    """The delete that no link and no swap of a folder can redirect, and that checks the bytes of the very file it
    removes: through a handle on the file (Windows) or inside folders opened with no link followed (POSIX). Neither:
    (None, why), and nothing is deleted (fail closed)."""
    if os.name == "nt":
        return _handle_delete(target, born, between, last)
    if ({os.open, os.stat, os.unlink, os.rename} <= os.supports_dir_fd and os.stat in os.supports_follow_symlinks
            and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")):
        return _folder_delete(root, target, born, between, last)
    return None, "this system has no delete tied to the file or to a checked folder"


def remove_made(notes_dir: Path, vault: str, made: list[tuple[str, str, str]], between=None,
                last=None) -> list[str]:
    """Remove the test's own files from the notes folder of the drill (`vault`: its fingerprint). A file goes only
    when its receipt names THAT folder, it is the very entry at the receipt's path (no link or junction on the way and
    none at its name: delete_made_file ties the delete to the entry it checked), and a note only while it holds the
    bytes that were written ("born"), read from the very file that is removed. Anything else stays and is said: a file
    made in another folder (the galaxy moved during the drill) cannot be found from its fingerprint, and a file of the
    same name here is not the test's. A path that cannot be looked at is trouble for its receipt, and the others go
    on; a notes folder that cannot be looked at removes nothing, and the clean-up is not confirmed. Returns the
    trouble; it never raises. `between` and `last`: the hooks of a test, see _folder_delete."""
    if not made:
        return []
    try:
        root = notes_dir.resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(errno.ENOTDIR, "not a folder")
    except (OSError, RuntimeError, ValueError) as error:
        return [f"the notes folder could not be looked at ({type(error).__name__}), so the clean-up is not confirmed: "
                f"none of the {len(made)} files of the test was removed"]
    trouble: list[str] = []
    for relative, made_in, born in made:
        if not vault or made_in != vault:
            trouble.append(f"{relative} was made in another notes folder (or its receipt names none), so it was left alone")
            continue
        note = relative.endswith(".md")
        if note and not born:
            trouble.append(f"{relative} is not the note the test wrote (its receipt names no bytes), so it was left alone")
            continue
        try:
            target = Path(os.path.normpath(root / relative))        # the place itself: a link there is not followed
            if "\0" in relative or target == root or not target.is_relative_to(root):
                trouble.append(f"{relative} is not inside the notes folder, so it was left alone")
                continue
            done, why = delete_made_file(root, target, born if note else None, between, last)
        except FileNotFoundError:
            continue                                    # already gone
        except CleanupUnsure as error:                  # it says where things are now
            trouble.append(f"{relative}: {error}")
            continue
        except (OSError, ValueError) as error:
            if isinstance(error, OSError) and error.errno in (errno.ELOOP, errno.ENOTDIR):
                trouble.append(f"{relative} {KEEPS['linked']}, so it was left alone")
            else:
                trouble.append(f"{relative} could not be removed ({getattr(error, 'strerror', None) or type(error).__name__})")
            continue
        if done is False:
            trouble.append(f"{relative} {KEEPS.get(why, why)}, so it was left alone")
        elif done is None:
            trouble.append(f"{relative} was left alone: there is no safe delete here ({why}). Remove it by hand")
    return trouble


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


def put_back(state: dict, made: list[tuple[str, str, str]]) -> tuple[bool, str]:
    """Remove the files in `made` (the test's own, see remove_made), remove folders that the test made and left
    empty, and build the galaxy again from the notes on disk. It never raises: it reports."""
    notes_dir = Path(state["notes_dir"])
    folder = notes_dir / teardown.TEARDOWNS_DIR
    trouble = remove_made(notes_dir, state.get("vault") or "", made)
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
    made: list[tuple[str, str, str]] = []   # the files those jobs made, with their vault and born (made_by)
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
