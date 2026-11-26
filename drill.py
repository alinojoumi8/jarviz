"""The teardown drill: prove the whole kit before a camera sees it, and leave no trace.

    python drill.py                    the full drill (the server must be running: python server.py)
    python drill.py --site igloo.inc   use another public page for the live part (default: example.com)
    python drill.py --offline          only the fast checks: no browser, no network, no server
    python drill.py --keep FOLDER      copy the test note and its snapshot to FOLDER before they are removed

The live part runs ONE real teardown with a hidden browser. Before it, the drill records the galaxy
and the teardowns folder. After it, the drill removes the files that its own test made (and only
those) and builds the galaxy again from the notes. So your first real teardown is truly the first.
"""
from __future__ import annotations

import http.client
import re
import secrets
import shutil
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
GOOD_TIME_S = 150           # ...and a healthy one is faster than this
call, call_json = preflight.call, preflight.call_json
SERVER_GONE = (OSError, http.client.HTTPException)


class Report(preflight.Report):
    def summary(self) -> str:
        return f"DRILL: {self.counts[PASS]} pass, {self.counts[FAIL]} fail"


def tree(folder: Path) -> dict[str, int]:
    """Every file under `folder` with its size. Used to prove the folder is the same afterwards."""
    if not folder.is_dir():
        return {}
    return {path.relative_to(folder).as_posix(): path.stat().st_size for path in sorted(folder.rglob("*")) if path.is_file()}


def job_now() -> dict:
    _, status = call_json("GET", "/teardown/status")
    return status.get("job") or {}


def wait_for_end(job_id: str | None, limit: float) -> dict:
    """Poll the job like the panel does. Returns the last view of OUR job ({} when it is not ours or
    the server is gone). A job that is still running at the limit is cancelled."""
    deadline, job = time.monotonic() + limit, {}
    try:
        while time.monotonic() < deadline:
            job = job_now()
            if job.get("id") != job_id:
                return {}
            if job.get("state") != "running":
                return job
            time.sleep(0.5)
        call_json("POST", "/teardown/cancel", {})
    except SERVER_GONE:
        return {}
    return job


def offline(report: Report) -> None:
    for name, passed, detail in teardown.offline_checks():
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
    else:
        report.add(FAIL if chromes or profiles else PASS, name,
                   f"{len(chromes)} Chrome processes, {len(profiles)} profile folders" if chromes or profiles else "")


def record() -> dict:
    """How things are before a test teardown: the notes in the galaxy and the teardowns folder."""
    notes_dir = build.resolve_notes_dir(server.load_config()["notes_dir"])
    folder = notes_dir / teardown.TEARDOWNS_DIR
    watched = (notes_dir / "captures", folder, folder / teardown.ASSETS_DIR)
    return {"notes_dir": str(notes_dir), "labels": [node.get("label") for node in build.read_graph().get("nodes", [])],
            "tree": tree(folder), "folders": {str(path): path.is_dir() for path in watched}}


def made_by(job: dict) -> list[str]:
    """The files one finished test teardown made, as paths inside the notes folder. Nothing else is ever removed."""
    result = job.get("result") or {}
    name = result.get("name")
    if job.get("state") != "done" or not teardown.valid_name(name):
        return []
    files = [f"{teardown.TEARDOWNS_DIR}/{name}.md"]
    if result.get("snapshot"):
        files.append(f"{teardown.TEARDOWNS_DIR}/{teardown.ASSETS_DIR}/{name}.png")
    if any(birth.get("file") == teardown.CONCEPT_PATH for birth in result.get("births") or []):
        files.append(teardown.CONCEPT_PATH)             # the REA note, only when THIS job made it
    return files


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
        _, now = call_json("GET", "/status")            # the server sees the new file and loads the notes again
        count = now.get("graph", {}).get("count")
    except SERVER_GONE:
        count = None
        trouble.append("the server did not answer, so it still holds the old galaxy. Restart it")
    labels = [node.get("label") for node in build.read_graph().get("nodes", [])]
    if labels != state["labels"]:
        trouble.append(f"the galaxy has {len(labels)} notes, it had {len(state['labels'])}")
    elif count is not None and count != len(labels):
        trouble.append(f"the server holds {count} notes, the file has {len(labels)}")
    if tree(folder) != state["tree"]:
        trouble.append("the teardowns folder is not as it was")
    return not trouble, "; ".join(trouble) or f"{len(labels)} notes"


def start(site: str, session: str, question: str = "") -> tuple[int, dict, str]:
    body = {"site": site, "session": session, "headless": True}
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
                call_json("POST", "/teardown/cancel", {})
                made += made_by(wait_for_end(extra, 60))
            report.add(PASS if status == 409 and "already taking one apart" in second.get("answer", "") else FAIL,
                       "A second request while one runs is refused in one line", teardown._clean(second.get("answer"), 120))
        else:
            report.add(FAIL, "A second request while one runs is refused in one line",
                       "Not checked: the first teardown ended before the second request could be sent.")

        job = wait_for_end(first, LIVE_LIMIT_S)
        made += made_by(job)
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
            _, cancelled = call_json("POST", "/teardown/cancel", {})
            stopped = wait_for_end(third, 60)
            made += made_by(stopped)
            report.add(PASS if stopped.get("state") == "cancelled" and tree(folder) == tree_after_run else FAIL,
                       "A cancelled teardown stops and writes nothing",
                       f"state {stopped.get('state')}: {teardown._clean(cancelled.get('answer'), 80)}")
            time.sleep(1.0)
            nothing_left(report, "after a cancelled run")
        else:
            report.add(FAIL, "A cancelled teardown stops and writes nothing", f"it did not start: HTTP {status}")
    finally:
        # -- put everything back, whatever happened above. Only the files of this drill's own jobs go.
        try:
            last = job_now()
            if last.get("id") in ours:
                made += made_by(wait_for_end(last["id"], 90))
        except SERVER_GONE:
            pass
        clean, detail = put_back(before, list(dict.fromkeys(made)))
        report.add(PASS if clean else FAIL, "Everything is put back: the test note is gone and the galaxy is as before", detail)


def main(argv: list[str]) -> int:
    report = Report()
    site = argv[argv.index("--site") + 1] if "--site" in argv[:-1] else "example.com"
    keep = Path(argv[argv.index("--keep") + 1]).resolve() if "--keep" in argv[:-1] else None
    print(f"Jarviz teardown drill  {time.strftime('%Y-%m-%d %H:%M:%S')}\n\nOffline checks")
    offline(report)
    if "--offline" not in argv:
        print(f"\nLive drill: one hidden teardown of {site}")
        if teardown.parse_site(site) is None:
            report.add(FAIL, "The drill site is a website address", site)
        elif not preflight.check_server(report):
            pass
        else:
            _, kit = call_json("GET", "/teardown/diag")
            running = job_now().get("state") == "running"
            ready = kit.get("ready") and kit.get("rea", {}).get("installed") == teardown.REA_PIN and not running
            if report.add(PASS if ready else FAIL, f"The kit is ready with REA {teardown.REA_PIN}",
                          "A teardown is running now. Wait for it." if running
                          else "; ".join(kit.get("problems") or []) or f"Node.js {kit.get('node', {}).get('version')}, Chrome found"):
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
