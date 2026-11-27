"""Jarviz and Ali Memory: the best results of the user's private knowledge base for one question, read-only.

Ali Memory is the user's own knowledge base: a hub on this computer (http://127.0.0.1:8791, loopback only). For a
chat question Jarviz asks it for a few results (POST /api/recall) and hands them to the brain in a block of their
own, ONLY when the active brain is Claude (BRAINS). With any other brain Jarviz answers without Ali Memory. Jarviz
never writes to Ali Memory: its token is a read-only ("ro") token of the hub.

    "ali_memory_api_key"   the hub's bearer token. The name ends in "_api_key" on purpose: every key scan and every
                           scrub of Jarviz covers it (voices.key_lines, voices.configured_keys).
    "ali_memory_url"       the hub: http://127.0.0.1:<port> or http://localhost:<port>, nothing else.

The token travels in the Authorization header of one loopback request: never through a proxy, never after a
redirect, never in an error text, a log, an exception text or a result. Everything that comes back is data from a
stranger: its types and lengths are checked, every text becomes one clean line, and every key of config.json is taken
out of it. A failure is a status code (STATUS), never an exception: the chat then answers without Ali Memory.

    python ali_memory.py     the offline checks (fake openers, and sockets of the checks themselves on 127.0.0.1)
"""
from __future__ import annotations

import http.client
import io
import json
import os
import re
import socket
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import urllib.response
from collections.abc import Callable, Iterable
from typing import Any, Protocol

import voices

KEY_LINE = "ali_memory_api_key"
URL_LINE = "ali_memory_url"
DEFAULT_URL = "http://127.0.0.1:8791"
KEY_PLACEHOLDER = voices.KEY_PLACEHOLDER        # config.json has one placeholder for every key
BRAINS = ("anthropic",)        # the brains that may be shown Ali Memory: Claude only (the user's decision)
RECALL_PATH = "/api/recall"
LIMIT = 3                      # results asked for, and the most that are ever used
MAX_QUERY_CHARS = 2000         # the hub cuts a longer query here too
# ONE budget for the connect, the request and the read (the hub: p50 0.4 s, p95 0.6 s, its worst a few seconds). A
# recall that is not back by then is "timeout", and the chat answers without it. It is ONE deadline (time.monotonic()):
# the connect, every send and every read end by it (_BudgetSocket), and an answer done after it is dropped (_within).
BUDGET_S = 3.0
# A hub that listens completes a loopback handshake at once. Windows takes about 2 s to report a refused one: without
# this, every question would wait that long while the hub is down.
CONNECT_TIMEOUT_S = 0.5
# After the deadline the waiting side waits at most this long more for the helper thread to end. Its socket ends by
# the deadline by itself, so the helper is gone in a moment: no helper thread and no socket outlives a recall.
UNWIND_S = 0.2
MAX_REPLY_BYTES = 256 * 1024   # three compact results are far smaller: a bigger answer is no recall answer
MAX_ERROR_BYTES = 16 * 1024    # an error body is read only this far
TITLE_CHARS, SNIPPET_CHARS, PROJECT_CHARS, SOURCE_CHARS = 120, 1200, 60, 40
MAX_TAINTS = 4
TOKEN_CHARS = (16, 512)        # the hub's own client takes a token of 16 to 512 characters
UNTITLED = "(untitled)"
PROBE_QUERY = "Jarviz"         # preflight's one real recall: a fixed, harmless question
USER_AGENT = voices.USER_AGENT
URL_RE = re.compile(r"http://(127\.0\.0\.1|localhost):([0-9]{1,5})/?", re.IGNORECASE)
WORD_RE = re.compile(r"[a-z0-9_]{1,40}")       # a code word of the hub ("starting"), or a taint ("generated_summary")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# Characters that are never shown: controls, format characters (zero width, bidi), line and paragraph separators,
# lone surrogates. Each becomes a space, so two pieces of a key are never joined into one.
HIDDEN = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})

# Every outcome of recall(), with what it means. "ok" is the only one with results (it may have none).
STATUS = {
    "ok": "Ali Memory answered.",
    "off_brain": "Ali Memory is used with the Claude brain only.",
    "no_key": f'config.json has no Ali Memory key yet ("{KEY_LINE}").',
    "bad_key": (f'The "{KEY_LINE}" line holds a character that cannot be sent (a space, a line break, a letter '
                f"outside plain ASCII), or it is not {TOKEN_CHARS[0]} to {TOKEN_CHARS[1]} characters long."),
    "key_copied": f'The "{KEY_LINE}" line holds the key of another line (or a part of it). It is never sent.',
    "bad_url": f'The "{URL_LINE}" line is not http://127.0.0.1:<port> or http://localhost:<port>.',
    "empty_query": "There was no question to ask.",
    "query_has_key": "The question holds a key of config.json, so it was not sent.",
    "unreachable": "The Ali Memory hub did not answer on that address. Is it running?",
    "timeout": f"The Ali Memory hub did not answer within {BUDGET_S:g} seconds.",
    "unauthorized": "The hub refused the key. It needs a read-only (ro) token of the hub.",
    "host_rejected": "The hub refused the Host header (HTTP 421). Start it with --allowed-host 127.0.0.1:8791.",
    "starting": "The hub is still starting.",
    "unavailable": "The hub could not search now (HTTP 5xx).",
    "no_route": "The hub has no /api/recall route (an older hub?).",
    "bad_request": "The hub refused the request (HTTP 4xx).",
    "redirected": "The hub answered with a redirect. Jarviz never follows one: the key would go with it.",
    "bad_reply": "The hub's answer is not a recall answer (its type, its size or its shape).",
    "not_sent": "The request could not be made.",
    "failed": "The recall stopped.",
}


class Opener(Protocol):
    """What recall() needs: urllib's own opener, or a fake one in the offline checks."""

    def open(self, request: urllib.request.Request, timeout: float = ...) -> Any: ...


# --------------------------------------------------------------------- lines

def default_config() -> dict:
    """The Ali Memory lines of config.json."""
    return {KEY_LINE: KEY_PLACEHOLDER, URL_LINE: DEFAULT_URL}


def token_of(config: dict) -> str:
    """The token from config.json, or "" when there is none yet. Only a text is a token (as in voices.key_lines)."""
    value = config.get(KEY_LINE)
    token = value.strip() if isinstance(value, str) else ""
    return "" if token == KEY_PLACEHOLDER else token


def has_key(config: dict) -> bool:
    return bool(token_of(config))


def brain_allowed(config: dict) -> bool:
    """True when the brain line of config.json names a brain that may be shown Ali Memory (BRAINS)."""
    return str(config.get("brain", "")).strip().lower() in BRAINS


def parse_url(value) -> tuple[str, int] | None:
    """(host, port) for http://127.0.0.1:<port> or http://localhost:<port> (a "/" at the end is fine), else None.
    No other scheme, host, user name, path, query or fragment: the token never leaves this computer."""
    found = URL_RE.fullmatch(value.strip()) if isinstance(value, str) else None
    if found is None:
        return None
    port = int(found.group(2))
    return (found.group(1).lower(), port) if 0 < port < 65536 else None


def trouble(config: dict) -> str:
    """Why no recall may be sent with these lines of config.json (a code of STATUS), or "" when one may. It sends
    nothing. The brain decides first: with a brain that may not see Ali Memory, nothing else matters."""
    if not brain_allowed(config):
        return "off_brain"
    token = token_of(config)
    if not token:
        return "no_key"
    if not (voices.KEY_TEXT_RE.fullmatch(token) and TOKEN_CHARS[0] <= len(token) <= TOKEN_CHARS[1]):
        return "bad_key"
    for line, value in voices.key_lines(config).items():
        if line != KEY_LINE and (value in token or token in value):
            return "key_copied"         # the key of another provider: it never goes to the hub
    if parse_url(config.get(URL_LINE, DEFAULT_URL)) is None:
        return "bad_url"
    return ""


def state(config: dict) -> str:
    """ "on", or the code of STATUS that keeps Ali Memory off (no request is sent)."""
    return trouble(config) or "on"


# -------------------------------------------------------------------- recall

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None                      # the token must never follow a redirect to another address


class _BudgetSocket(socket.socket):
    """A socket that waits at most until `deadline` (time.monotonic()) in EVERY send and read. A plain timeout counts
    each read anew, so a hub that sends its answer a byte at a time could hold a recall (its thread and its socket)
    for as long as it likes, also after the chat gave up on it. A send or a read after the deadline is TimeoutError."""

    deadline = 0.0

    def _wait_left(self) -> None:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("the budget is spent")
        self.settimeout(left)

    def recv_into(self, *args, **kwargs):
        self._wait_left()
        return super().recv_into(*args, **kwargs)

    def recv(self, *args, **kwargs):
        self._wait_left()
        return super().recv(*args, **kwargs)

    def send(self, *args, **kwargs):
        self._wait_left()
        return super().send(*args, **kwargs)

    def sendall(self, *args, **kwargs):
        self._wait_left()
        return super().sendall(*args, **kwargs)


class _LoopbackConnection(http.client.HTTPConnection):
    """A connection to 127.0.0.1 only, with a short connect (CONNECT_TIMEOUT_S), that ends by `deadline`: the deadline
    of its recall (the request carries it), else the timeout of the request from the connect on. The connect, every
    send and every read end by it (_BudgetSocket). A connect that times out is "nothing listens", as a refused one
    is."""

    def __init__(self, host, *, deadline: float | None = None, **kwargs):
        super().__init__(host, **kwargs)
        self.deadline = deadline

    def connect(self) -> None:
        if self.host != "127.0.0.1":                # recall() builds only such addresses: the second net
            raise ConnectionRefusedError("Jarviz asks Ali Memory on 127.0.0.1 only")
        deadline = self.deadline
        if not isinstance(deadline, float):
            wait = self.timeout if isinstance(self.timeout, (int, float)) and self.timeout > 0 else CONNECT_TIMEOUT_S
            deadline = time.monotonic() + wait
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("the budget is spent")
        sock = _BudgetSocket(socket.AF_INET, socket.SOCK_STREAM)
        sock.deadline = deadline
        try:
            sock.settimeout(min(left, CONNECT_TIMEOUT_S))
            sock.connect((self.host, self.port))
        except TimeoutError:
            sock.close()
            raise ConnectionRefusedError("no hub answered the connect") from None
        except BaseException:
            sock.close()
            raise
        self.sock = sock
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass                        # a slower answer, nothing worse


class _LoopbackHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_LoopbackConnection, req, deadline=getattr(req, "deadline", None))


# No proxy (ProxyHandler({})): an HTTP_PROXY of the system would get the token. No redirect. Loopback only.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect, _LoopbackHandler)


def recall(config: dict, query: str, opener: Opener | None = None, *, budget: float = BUDGET_S) -> tuple[list[dict], str]:
    """The best results of Ali Memory for `query`, and a code of STATUS. Results only with "ok" (there may be none):
    at most LIMIT, each {"title", "snippet", "project", "source", "date", "taints"}, every text one clean line with
    every key of config.json taken out. It never raises, and it sends nothing when trouble() names a reason, when the
    query is empty, or when the query holds a key of config.json. ONE budget (seconds) holds for the whole call: one
    deadline, from the start. `opener` serves the offline checks only."""
    deadline = time.monotonic() + max(0.0, budget)
    why = trouble(config)
    if why:
        return [], why
    keys = voices.configured_keys(config)       # every key of config.json, of a brain, a voice or Ali Memory
    text = query.strip() if isinstance(query, str) else ""
    if not text:
        return [], "empty_query"
    if any(key in text for key in keys):        # the WHOLE question: a cut could split a key in two
        return [], "query_has_key"
    host, port = parse_url(config.get(URL_LINE, DEFAULT_URL))
    token = token_of(config)
    return _within(lambda: _ask(token, host, port, text[:MAX_QUERY_CHARS], opener or _OPENER, keys, deadline), deadline)


def _within(work: Callable[[], tuple[list[dict], str]], deadline: float) -> tuple[list[dict], str]:
    """work() in a helper thread, and its answer only when it was done by `deadline` (time.monotonic()), else
    "timeout": an answer done later is dropped, also when it is there before the waiting side looks. The helper's
    I/O ends by the deadline by itself (_BudgetSocket), and the waiting side waits UNWIND_S more for it to end, so no
    helper thread and no socket of a recall outlives it. No exception leaves: its text could hold a header value,
    the token."""
    box: list = []

    def run() -> None:
        try:
            answer = work()
        except Exception:       # noqa: BLE001 - never its text: only that it stopped
            answer = ([], "failed")
        box.append((time.monotonic(), answer))      # when it was done: only an answer in time is used

    helper = threading.Thread(target=run, name="jarviz-ali-memory", daemon=True)
    try:
        helper.start()
    except RuntimeError:        # no thread could be started: no recall, as for any other failure
        return [], "failed"
    helper.join(max(0.0, deadline - time.monotonic()))
    if helper.is_alive():
        helper.join(UNWIND_S)   # its socket ends by the deadline: it closes it and ends in a moment
    if box and box[0][0] <= deadline:
        return box[0][1]
    return [], "timeout"


def _ask(token: str, host: str, port: int, query: str, opener: Opener, keys: Iterable[str],
         deadline: float) -> tuple[list[dict], str]:
    """One POST /api/recall to 127.0.0.1:<port>, its answer checked, all of it by `deadline` (time.monotonic()). The
    query goes in the JSON body, never in the address. Host names what config.json says (127.0.0.1 or localhost):
    the hub checks it."""
    left = deadline - time.monotonic()
    if left <= 0:
        return [], "timeout"
    body = json.dumps({"query": query, "limit": LIMIT, "all_projects": True}).encode("ascii")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{RECALL_PATH}", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json",
                 "Host": f"{host}:{port}", "User-Agent": USER_AGENT})
    request.deadline = deadline     # the connection ends its connect, every send and every read by it
    try:
        with opener.open(request, timeout=left) as response:
            status = getattr(response, "status", None)
            headers = getattr(response, "headers", None)
            ctype = headers.get("Content-Type", "") if headers is not None else ""
            raw = _read_capped(response, MAX_REPLY_BYTES, deadline)
    except urllib.error.HTTPError as error:              # a redirect lands here too: _NoRedirect did not follow it
        code = error.code if isinstance(error.code, int) and not isinstance(error.code, bool) else 0
        return [], _http_code(code, _error_word(_error_body(error)))
    except urllib.error.URLError as error:
        return [], "timeout" if isinstance(error.reason, TimeoutError) else "unreachable"
    except TimeoutError:
        return [], "timeout"
    except (OSError, http.client.HTTPException):         # HTTPException: the answer broke off (IncompleteRead)
        return [], "unreachable"
    except (ValueError, TypeError):                      # never its text: it can hold a header value
        return [], "not_sent"
    if status != 200 or not isinstance(ctype, str) or not ctype.strip().lower().startswith("application/json"):
        return [], "bad_reply"
    if raw is None:
        return [], "bad_reply"                           # bigger than MAX_REPLY_BYTES
    results = _results(_json_of(raw), keys)
    return ([], "bad_reply") if results is None else (results, "ok")


def _read_capped(response: Any, limit: int, deadline: float) -> bytes | None:
    """The body, or None when it is bigger than `limit` (one byte more is read to know). The budget holds between
    reads. An answer that ends before its Content-Length broke off (http.client then counts the missing bytes)."""
    chunks: list[bytes] = []
    size = 0
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("the budget is spent")
        chunk = response.read(min(64 * 1024, limit + 1 - size))
        if not chunk:
            left = getattr(response, "length", None)
            if isinstance(left, int) and not isinstance(left, bool) and left > 0:
                raise http.client.IncompleteRead(b"", left)
            return b"".join(chunks)
        if not isinstance(chunk, bytes):
            raise http.client.HTTPException("the answer is not bytes")
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            return None


def _error_body(error: urllib.error.HTTPError) -> bytes:
    """At most MAX_ERROR_BYTES of an error body. The answer is closed after it, also when the read fails."""
    try:
        return error.read(MAX_ERROR_BYTES) or b""
    except (OSError, http.client.HTTPException, ValueError, AttributeError):
        return b""
    finally:
        try:
            error.close()
        except (OSError, http.client.HTTPException, ValueError, AttributeError):
            pass


def _json_of(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):    # UnicodeDecodeError is a ValueError; RecursionError: JSON nested too deep
        return None


def _error_word(raw: bytes) -> str:
    """The hub's code word of an error ({"error": "starting"}), only when it is a plain word: nothing else of an error
    body is ever kept."""
    payload = _json_of(raw)
    word = payload.get("error") if isinstance(payload, dict) else None
    return word if isinstance(word, str) and WORD_RE.fullmatch(word) else ""


def _http_code(status: int, word: str) -> str:
    if 300 <= status < 400:
        return "redirected"
    if status in (401, 403):
        return "unauthorized"
    if status == 421:
        return "host_rejected"
    if status in (404, 405):
        return "no_route"
    if status == 503 and word == "starting":
        return "starting"
    if 400 <= status < 500:
        return "bad_request"
    if status >= 500:
        return "unavailable"
    return "bad_reply"


def _results(payload: Any, keys: Iterable[str]) -> list[dict] | None:
    """The results of a recall answer, cleaned, or None when it is not one: a JSON object with a list "results" (and
    a "count" that is a whole number, when it is there). A result that is no object, or has no snippet text, is left
    out. At most LIMIT."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return None
    count = payload.get("count", 0)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    keys = list(keys)
    found: list[dict] = []
    for item in payload["results"]:
        if len(found) == LIMIT:
            break
        if not isinstance(item, dict):
            continue
        snippet = _clean(item.get("snippet"), SNIPPET_CHARS, keys)
        if not snippet:
            continue
        found.append({"title": _clean(item.get("title"), TITLE_CHARS, keys) or UNTITLED,
                      "snippet": snippet,
                      "project": _clean(item.get("project"), PROJECT_CHARS, keys),
                      "source": _clean(item.get("source"), SOURCE_CHARS, keys),
                      "date": _date(item.get("timestamp"), keys),
                      "taints": _taints(item.get("taints"), keys)})
    return found


def _clean(value: Any, limit: int, keys: Iterable[str]) -> str:
    """One clean line of at most `limit` characters, with every key taken out, or "" for anything that is no text.
    Hidden characters become spaces FIRST (so no two pieces of a key are joined). The keys go BEFORE the cut (a cut
    first could leave a piece of a key): from the text as it is (a key with whitespace of its own), and again from the
    one line (a key said back with other whitespace, "demo   value", is whole again only there; each key also as one
    line: voices.one_line_keys). And once more after the cut."""
    if not isinstance(value, str):
        return ""
    keys = voices.one_line_keys(keys)
    text = "".join(" " if unicodedata.category(char) in HIDDEN else char for char in value)
    text = voices.scrub_keys(" ".join(voices.scrub_keys(text, keys).split()), keys)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    return voices.scrub_keys(text, keys)


def _date(value: Any, keys: Iterable[str]) -> str:
    text = _clean(value, 40, keys)
    return text[:10] if DATE_RE.match(text) else ""


def _taints(value: Any, keys: Iterable[str]) -> list[str]:
    """The hub's taint words of a result ("generated_summary": an agent wrote it), plain words only, each once."""
    if not isinstance(value, list):
        return []
    words: list[str] = []
    for item in value:
        word = voices.scrub_keys(item, keys) if isinstance(item, str) else ""
        if WORD_RE.fullmatch(word) and word not in words:
            words.append(word)
        if len(words) == MAX_TAINTS:
            break
    return words


# ------------------------------------------------------------- offline checks

class _FakeResponse:
    """A stand-in for an HTTP answer. `broken` breaks the read in the middle, `length` is what http.client would
    still count as missing. `end_delay`: the read that finds the end takes that many seconds."""

    def __init__(self, status: int, body: bytes, ctype: str | None = "application/json", broken: bool = False,
                 length: int | None = None, end_delay: float = 0.0):
        self.status, self.length = status, length
        self.headers = http.client.HTTPMessage()
        if ctype is not None:
            self.headers["Content-Type"] = ctype
        self._body, self._broken, self._end_delay = io.BytesIO(body), broken, end_delay

    def read(self, size: int = -1) -> bytes:
        if self._broken:
            raise http.client.IncompleteRead(b"", 1000)
        chunk = self._body.read(size)
        if not chunk and self._end_delay:
            time.sleep(self._end_delay)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _FakeOpener:
    """A stand-in for the opener. It sends nothing: `make(request)` gives the answer or raises."""

    def __init__(self, make, delay: float = 0.0):
        self.make, self.delay = make, delay
        self.calls: list[dict] = []

    def open(self, request: urllib.request.Request, timeout: float | None = None):
        self.calls.append({"url": request.full_url, "method": request.get_method(), "body": request.data,
                           "headers": {name.lower(): value for name, value in request.header_items()},
                           "timeout": timeout, "deadline": getattr(request, "deadline", None),
                           "opened": time.monotonic()})
        if self.delay:
            time.sleep(self.delay)
        return self.make(request)


class _FakeHTTP(urllib.request.HTTPHandler):
    """urllib's own chain with a fake last step that answers without a socket. It proves what an opener built with
    _NoRedirect does with a redirect."""

    def __init__(self, status: int, body: bytes, headers: dict):
        super().__init__()
        self.status, self.body, self.extra = status, body, headers
        self.urls: list[str] = []

    def http_open(self, request):
        self.urls.append(request.full_url)
        headers = http.client.HTTPMessage()
        for name, value in self.extra.items():
            headers[name] = value
        response = urllib.response.addinfourl(io.BytesIO(self.body), headers, request.full_url, self.status)
        response.msg = "fake"
        return response


class _Stub:
    """A socket of the checks themselves on 127.0.0.1 (no network). `answer` None: it listens and never answers.
    Else it takes each connection, keeps the bytes of the request (`seen`) and sends `answer`. A slow hub: `drip`
    seconds between the bytes of the answer (after its head at once with `head_at_once`). `cut_at` is when a send
    failed (time.monotonic()): the other side closed its socket."""

    def __init__(self, answer: bytes | None, drip: float = 0.0, head_at_once: bool = False):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.answer, self.seen = answer, []
        self.drip, self.head_at_once = drip, head_at_once
        self.cut_at: float | None = None
        self.cut = threading.Event()
        if answer is not None:
            threading.Thread(target=self._serve, name="ali-memory-check-stub", daemon=True).start()

    def _serve(self) -> None:
        self.sock.settimeout(5)
        try:
            while True:
                conn, _ = self.sock.accept()
                with conn:
                    conn.settimeout(2)
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    head, _, rest = data.partition(b"\r\n\r\n")
                    declared = re.search(rb"(?im)^content-length:\s*([0-9]+)", head)
                    while declared and len(rest) < int(declared.group(1)):
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        rest += chunk
                    self.seen.append((head, rest))
                    self._send(conn, self.answer)
        except OSError:
            pass                        # the stub was closed, or nobody came: the check reads `seen`

    def _send(self, conn: socket.socket, answer: bytes) -> None:
        if not self.drip:
            conn.sendall(answer)
            return
        if self.head_at_once:
            at = answer.find(b"\r\n\r\n") + 4
            conn.sendall(answer[:at])
            answer = answer[at:]
        try:
            for at in range(len(answer)):
                conn.sendall(answer[at:at + 1])
                time.sleep(self.drip)
        except OSError:
            self.cut_at = time.monotonic()
            self.cut.set()
            raise

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _http_answer(status: int, reason: str, body: bytes, extra: str = "") -> bytes:
    return (f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            f"{extra}Connection: close\r\n\r\n").encode("ascii") + body


def offline_checks() -> list[tuple[str, bool, str]]:
    """Fast checks with fake openers and sockets of the checks themselves on 127.0.0.1: no network, no file. They
    prove the logic of the code. The real hub is proved by preflight (one recall with a fixed test question)."""
    results: list[tuple[str, bool, str]] = []

    def check(label: str, passed, detail: str = "") -> None:
        results.append((label, bool(passed), detail))

    # Fake keys, made for these checks only.
    token = "fake-ali-memory-token-for-the-offline-checks"
    openai_key = "fake-openai-key-for-the-ali-memory-checks"
    voice_key = "fake-deepgram-key-for-the-ali-memory-checks"
    other_key = "fake-future-key-of-another-line"
    cfg = {"brain": "anthropic", KEY_LINE: token, URL_LINE: DEFAULT_URL}
    every = {**cfg, "openai_api_key": openai_key, "deepgram_api_key": voice_key, "zebra_api_key": other_key}
    url = f"http://127.0.0.1:8791{RECALL_PATH}"

    def hub(results_list, count=None, **more) -> bytes:
        payload = {"results": results_list, "count": len(results_list) if count is None else count,
                   "scope": {"project": None, "origin": "all_projects"}, "retrieval": {}, "notice": "", **more}
        return json.dumps(payload).encode("utf-8")

    def item(title="Kettle descaling", snippet="Descale the kettle every Friday.", **more) -> dict:
        return {"citation": "doc_0001", "title": title, "source": "memory", "project": "home",
                "timestamp": "2026-11-20T08:00:00Z", "score": 0.9, "snippet": snippet, "taints": [], **more}

    good = hub([item(), item("Boiler", "The boiler code is in the blue folder.", taints=["generated_summary"])])

    def answer(status: int = 200, body: bytes = good, ctype: str | None = "application/json", broken: bool = False,
               length: int | None = None, end_delay: float = 0.0):
        return lambda request: _FakeResponse(status, body, ctype, broken, length, end_delay)

    def refuse(status: int, payload: dict | None = None, raw: bytes | None = None):
        body = raw if raw is not None else json.dumps(payload or {}).encode("utf-8")

        def make(request):
            raise urllib.error.HTTPError(request.full_url, status, "error", http.client.HTTPMessage(), io.BytesIO(body))
        return make

    def fails(error: BaseException):
        def make(request):
            raise error
        return make

    def run(make, config: dict = cfg, query: str = "When do I descale the kettle?", opener=None, budget=BUDGET_S):
        opener = opener or _FakeOpener(make)
        found, status = recall(config, query, opener, budget=budget)
        return status, found, opener

    def shown(found, status) -> str:
        return json.dumps(found) + status

    # -- the lines
    check("default_config: the Ali Memory lines, in their order",
          list(default_config().items()) == [(KEY_LINE, KEY_PLACEHOLDER), (URL_LINE, DEFAULT_URL)], str(default_config()))
    check("the key line ends in _api_key, so every key scan of Jarviz covers it", KEY_LINE.endswith("_api_key"), KEY_LINE)
    check("voices.key_lines and configured_keys hold the Ali Memory key", voices.key_lines(cfg) == {KEY_LINE: token}
          and token in voices.configured_keys(every), "")
    check("the placeholder is no key", not has_key({**cfg, KEY_LINE: KEY_PLACEHOLDER})
          and KEY_LINE not in voices.key_lines({**cfg, KEY_LINE: KEY_PLACEHOLDER}), "")
    check("BRAINS: Claude only", BRAINS == ("anthropic",), str(BRAINS))
    check("state: on with a key, the Claude brain and a loopback address", state(cfg) == "on", state(cfg))
    check("state: the default config is off (no key)", state({"brain": "anthropic", **default_config()}) == "no_key", "")

    # -- the request
    status, found, opener = run(answer())
    call = opener.calls[0] if opener.calls else {}
    try:
        sent = json.loads(call.get("body") or b"")
    except ValueError:
        sent = None
    check("recall: one request", len(opener.calls) == 1, str(len(opener.calls)))
    check("recall: POST to http://127.0.0.1:8791/api/recall", call.get("method") == "POST" and call.get("url") == url,
          f"{call.get('method')} {call.get('url')}")
    check("recall: the body is the query, limit 3 and all_projects true, nothing else",
          sent == {"query": "When do I descale the kettle?", "limit": 3, "all_projects": True}, str(sent))
    check("recall: the query is never in the address", "?" not in call.get("url", "?")
          and "kettle" not in call.get("url", "kettle"), call.get("url", ""))
    headers = call.get("headers", {})
    check("recall: the token in the Authorization header as Bearer", headers.get("authorization") == f"Bearer {token}", "")
    check("recall: Host, Content-Type and Accept", headers.get("host") == "127.0.0.1:8791"
          and headers.get("content-type") == "application/json" and headers.get("accept") == "application/json",
          str({name: value for name, value in headers.items() if name != "authorization"}))
    check("recall: the token is in no other header, the address or the body",
          sum(token in str(value) for value in headers.values()) == 1 and token not in call.get("url", "")
          and token.encode() not in (call.get("body") or b""), "")
    timeout, deadline, opened = call.get("timeout"), call.get("deadline"), call.get("opened")
    check("recall: the request carries ONE deadline, and its timeout is what is left of the budget",
          isinstance(deadline, float) and isinstance(timeout, float) and 0 < timeout <= BUDGET_S
          and isinstance(opened, float) and 0 <= deadline - opened <= timeout, f"{timeout} {deadline} {opened}")
    check("recall: ok, two results", status == "ok" and [r["title"] for r in found] == ["Kettle descaling", "Boiler"],
          shown(found, status))
    check("recall: a result has exactly its six fields",
          all(set(r) == {"title", "snippet", "project", "source", "date", "taints"} for r in found)
          and found and found[0]["date"] == "2026-11-20" and found[1]["taints"] == ["generated_summary"], shown(found, status))
    status, _, opener = run(answer(), {**cfg, URL_LINE: "http://LOCALHOST:9001/"})
    call = opener.calls[0] if opener.calls else {}
    check("recall: a localhost address connects to 127.0.0.1 and names localhost in Host",
          status == "ok" and call.get("url") == f"http://127.0.0.1:9001{RECALL_PATH}"
          and call.get("headers", {}).get("host") == "localhost:9001", f"{status} {call.get('url')}")
    long_query = "kettle " * 400
    status, _, opener = run(answer(), query=long_query)
    try:
        sent = json.loads(opener.calls[0]["body"]) if opener.calls else {}
    except ValueError:
        sent = {}
    check("recall: a long question is cut to 2000 characters", status == "ok"
          and sent.get("query") == long_query.strip()[:MAX_QUERY_CHARS], str(len(sent.get("query", ""))))

    # -- nothing is sent
    for what, config, wanted in (
            ("no key line", {"brain": "anthropic", URL_LINE: DEFAULT_URL}, "no_key"),
            ("the placeholder", {**cfg, KEY_LINE: KEY_PLACEHOLDER}, "no_key"),
            ("an empty key", {**cfg, KEY_LINE: "   "}, "no_key"),
            ("a number as the key", {**cfg, KEY_LINE: 12345678901234567890}, "no_key"),
            ("a key with a space", {**cfg, KEY_LINE: "fake token with spaces in it"}, "bad_key"),
            ("a key with a line break", {**cfg, KEY_LINE: "fake-token-line\r\nX-Evil: 1"}, "bad_key"),
            ("a key with a letter outside ASCII", {**cfg, KEY_LINE: "fake-token-\u00e9t\u00e9-for-checks"}, "bad_key"),
            ("a key that is too short", {**cfg, KEY_LINE: "short-token"}, "bad_key"),
            ("a key that is too long", {**cfg, KEY_LINE: "x" * 513}, "bad_key"),
            ("the key of the OpenAI line", {**cfg, KEY_LINE: openai_key, "openai_api_key": openai_key}, "key_copied"),
            ("a part of another key", {**cfg, KEY_LINE: openai_key[:20], "openai_api_key": openai_key}, "key_copied"),
            ("a key that holds another key", {**cfg, KEY_LINE: token, "venice_api_key": token[5:30]}, "key_copied")):
        status, found, opener = run(answer(), config)
        check(f"nothing is sent: {what} ({wanted})", status == wanted and not found and not opener.calls, status)
    for brain in ("openai", "deepseek", "venice", "openrouter", "codex", "antigravity", "", None, "claude"):
        status, found, opener = run(answer(), {**cfg, "brain": brain})
        check(f"nothing is sent: the brain {brain!r} may not see Ali Memory", status == "off_brain"
              and not opener.calls, status)
    status, _, opener = run(answer(), {**cfg, "brain": " Anthropic "})
    check("the brain line is read as load_config gives it (case and spaces)", status == "ok" and len(opener.calls) == 1,
          status)
    for address in ("https://127.0.0.1:8791", "http://example.com:8791", "http://127.0.0.1", "http://127.0.0.1:0",
                    "http://127.0.0.1:65536", "http://127.0.0.1:99999", "http://user@127.0.0.1:8791",
                    "http://127.0.0.1:8791/api", "http://127.0.0.1:8791?x=1", "http://127.0.0.1:8791#x",
                    "http://[::1]:8791", "http://127.0.0.2:8791", "http://localhost.:8791", "http://0x7f.0.0.1:8791",
                    "http://127.1:8791", "http://10.0.0.1:8791", "http://127.0.0.1:8791.evil.example",
                    "http://127.0.0.1:+8791", "file:///C:/config.json", "//127.0.0.1:8791", "", 8791):
        status, found, opener = run(answer(), {**cfg, URL_LINE: address})
        check(f"nothing is sent: the address {address!r} is not loopback http", status == "bad_url"
              and not opener.calls, status)
    for what, query, wanted in (("an empty question", "   ", "empty_query"), ("no text", None, "empty_query"),
                                ("the Ali Memory key", f"my token is {token}", "query_has_key"),
                                ("the OpenAI key", f"is {openai_key} right?", "query_has_key"),
                                ("a key after the cut", "x " * 1200 + other_key, "query_has_key")):
        status, found, opener = run(answer(), every, query=query)
        check(f"nothing is sent: a question with {what} ({wanted})", status == wanted and not opener.calls, status)

    # -- the hub's errors
    for code, payload, wanted in ((401, {"error": "unauthorized"}, "unauthorized"), (403, {}, "unauthorized"),
                                  (421, {"error": "misdirected_request"}, "host_rejected"),
                                  (503, {"error": "starting"}, "starting"),
                                  (503, {"error": "recall_unavailable"}, "unavailable"), (500, {}, "unavailable"),
                                  (400, {"error": "invalid_request", "field": "query"}, "bad_request"),
                                  (413, {}, "bad_request"), (404, {}, "no_route"), (405, {}, "no_route")):
        status, found, _ = run(refuse(code, payload))
        check(f"the hub's HTTP {code} {payload.get('error', '')} is {wanted}".replace("  ", " "),
              status == wanted and not found, status)
    status, found, _ = run(refuse(401, {"error": token}))
    check("an error that says the token back shows nothing of it", status == "unauthorized" and token not in shown(found, status),
          status)
    status, _, _ = run(refuse(503, raw=b"{" * 100000))
    check("an error body that is no JSON: the status still speaks", status == "unavailable", status)
    for error, wanted in ((urllib.error.URLError(ConnectionRefusedError("refused")), "unreachable"),
                          (urllib.error.URLError(TimeoutError("timed out")), "timeout"),
                          (TimeoutError("timed out"), "timeout"), (ConnectionResetError("reset"), "unreachable"),
                          (http.client.RemoteDisconnected("gone"), "unreachable"),
                          (ValueError(f"Invalid header value {token}"), "not_sent"),
                          (TypeError(f"bad {token}"), "not_sent"), (RuntimeError(f"surprise {token}"), "failed")):
        status, found, _ = run(fails(error))
        check(f"{type(error).__name__} is {wanted}, and no exception text travels", status == wanted and not found
              and token not in status, status)
    status, _, _ = run(answer(broken=True))
    check("an answer that breaks off is unreachable", status == "unreachable", status)
    status, _, _ = run(answer(body=good[:40], length=500))
    check("an answer shorter than its Content-Length is unreachable", status == "unreachable", status)

    # -- the budget
    started = time.monotonic()
    status, found, _ = run(answer(), opener=_FakeOpener(answer(), delay=1.0), budget=0.2)
    took = time.monotonic() - started
    check("a hub that is slower than the budget is a timeout, within the budget (and UNWIND_S)", status == "timeout"
          and not found and took < 0.8, f"{status} after {took:.2f} s")
    # The answer is read by 0.15 s, the read that finds its end takes 0.1 s more: done at about 0.25 s. The waiting side
    # waits UNWIND_S for the helper after the deadline, so that answer IS there when it looks: only its time decides.
    late = _FakeOpener(answer(end_delay=0.1), delay=0.15)
    status, found, _ = run(None, opener=late, budget=0.2)
    check("an answer done after the deadline is dropped, also when it is there before the waiting side looks",
          status == "timeout" and not found and len(late.calls) == 1, shown(found, status))
    status, found, _ = run(None, opener=_FakeOpener(answer(end_delay=0.1), delay=0.15), budget=1.0)
    check("the same answer done within the budget is kept", status == "ok" and len(found) == 2, shown(found, status))

    # -- answers that are no recall answer
    nested = b'{"results": ' + b"[" * 50000 + b"]" * 50000 + b"}"
    for what, make in (("a text/html answer", answer(ctype="text/html")), ("no type line", answer(ctype=None)),
                       ("HTTP 204", answer(status=204)), ("HTTP 202", answer(status=202)),
                       ("no JSON", answer(body=b"not json")), ("a JSON list", answer(body=b"[]")),
                       ("JSON nested too deep", answer(body=nested)),
                       ("bytes that are not UTF-8", answer(body=b'{"results": [], "x": "\xff"}')),
                       ("results that are no list", answer(body=b'{"results": {"a": 1}}')),
                       ("no results", answer(body=b'{"count": 0}')),
                       ("a count that is a text", answer(body=b'{"results": [], "count": "3"}')),
                       ("a count that is true", answer(body=b'{"results": [], "count": true}')),
                       ("a count below zero", answer(body=b'{"results": [], "count": -1}')),
                       ("an answer over the size cap", answer(body=b'{"results": [], "pad": "' + b"x" * MAX_REPLY_BYTES
                                                         + b'"}'))):
        status, found, _ = run(make)
        check(f"refused: {what}", status == "bad_reply" and not found, status)
    status, found, _ = run(answer(body=hub([])))
    check("an answer with no results is ok, and empty", status == "ok" and found == [], shown(found, status))

    # -- results from a stranger
    many = hub([item(f"Title {n}", f"Snippet {n}.") for n in range(8)])
    status, found, _ = run(answer(body=many))
    check("at most 3 results are kept", status == "ok" and [r["title"] for r in found] == ["Title 0", "Title 1", "Title 2"],
          shown(found, status))
    odd = hub(["a text", 7, None, {"title": "No snippet"}, item(snippet=12), item("Kept", "The one good result.")])
    status, found, _ = run(answer(body=odd))
    check("a result that is no object, or has no snippet text, is left out", status == "ok"
          and [r["title"] for r in found] == ["Kept"], shown(found, status))
    huge = hub([item("T" * 500, "S" * 5000, project="P" * 300, source="Q" * 300)])
    status, found, _ = run(answer(body=huge))
    first = found[0] if found else {}
    check("title, snippet, project and source are cut to 120, 1200, 60 and 40",
          len(first.get("title", "")) == TITLE_CHARS and len(first.get("snippet", "")) == SNIPPET_CHARS
          and len(first.get("project", "")) == PROJECT_CHARS and len(first.get("source", "")) == SOURCE_CHARS,
          str({name: len(value) for name, value in first.items() if isinstance(value, str)}))
    messy = hub([item("Line\none\u2028two", "a\r\nb\tc\x00d\u200be\u202ef\ud800g", project=None, timestamp="soon",
                      taints=["generated_summary", "Generated Summary", "x" * 50, 7, "generated_summary", "a", "b",
                              "c", "d"], title2="extra")])
    status, found, _ = run(answer(body=messy))
    first = found[0] if found else {}
    check("every text is one line with no hidden character", status == "ok" and first.get("title") == "Line one two"
          and first.get("snippet") == "a b c d e f g", shown(found, status))
    check("a missing project and a date that is no date are empty", first.get("project") == "" and first.get("date") == "",
          shown(found, status))
    check("taints: plain words only, each once, at most 4", first.get("taints") == ["generated_summary", "a", "b", "c"],
          str(first.get("taints")))
    status, found, _ = run(answer(body=hub([item(title=None)])))
    check("a result with no title text is (untitled)", found and found[0]["title"] == UNTITLED, shown(found, status))

    # -- keys in what comes back
    echo = hub([item(f"Title {token}", f"A snippet with {token} in it.", project=f"p{token}")])
    status, found, _ = run(answer(body=echo), every)
    check("the token said back in a result is never shown", status == "ok" and token not in shown(found, status)
          and voices.KEY_REMOVED in found[0]["snippet"], shown(found, status))
    for line, key in (("openai_api_key", openai_key), ("deepgram_api_key", voice_key), ("zebra_api_key", other_key)):
        status, found, _ = run(answer(body=hub([item(f"T {key}", f"Snippet {key}.", source=key)])), every)
        check(f"a key of the {line} line in a result is taken out", status == "ok"
              and not any(known in shown(found, status) for known in voices.configured_keys(every)), shown(found, status))
    cut_key = hub([item(snippet="y" * (SNIPPET_CHARS - 10) + other_key + " tail")])
    status, found, _ = run(answer(body=cut_key), every)
    check("a key across the cut leaves no piece of itself", status == "ok"
          and other_key[:8] not in found[0]["snippet"], found[0]["snippet"][-40:] if found else status)
    split_key = hub([item(snippet=f"{other_key[:10]}\u200b{other_key[10:]} and {other_key[:10]}\x00{other_key[10:]}")])
    status, found, _ = run(answer(body=split_key), every)
    check("a hidden character inside a key becomes a space: the key is not joined", status == "ok"
          and other_key not in shown(found, status), shown(found, status))
    # A key of another line may hold whitespace of its own ("demo value"). Said back with other whitespace, it is whole
    # again only after the text became one line: it goes from there too, BEFORE the cut (the title cut falls inside it).
    spaced = {**every, "zebra_api_key": "demo value"}
    echoes = hub([item("t" * 112 + " demo   value tail", "a demo \t value b", project="demo value")])
    status, found, _ = run(answer(body=echoes), spaced)
    first = found[0] if found else {}
    check("a key said back with other whitespace is taken out before the cut: no piece is left",
          status == "ok" and "demo" not in shown(found, status) and "value" not in shown(found, status)
          and first.get("snippet") == f"a {voices.KEY_REMOVED} b" and len(first.get("title", "")) <= TITLE_CHARS,
          shown(found, status))
    for key, said in (("demo  value", "a demo  value b"), ("demo\tvalue", "a demo\tvalue b")):
        status, found, _ = run(answer(body=hub([item(snippet=said)])), {**every, "zebra_api_key": key})
        check(f"a key with whitespace of its own ({key!r}) said back as it is is taken out", status == "ok"
              and found and found[0]["snippet"] == f"a {voices.KEY_REMOVED} b", shown(found, status))
    word_key = "zebraword4242"                      # a key that has the shape of a taint word
    status, found, _ = run(answer(body=hub([item(taints=[word_key, "fine"])])), {**every, "zebra_api_key": word_key})
    check("a taint that is a key is left out", found and found[0]["taints"] == ["fine"], shown(found, status))

    # -- a redirect is never followed (urllib's own chain)
    for code in (301, 302, 303, 307, 308):
        status, _, _ = run(refuse(code))
        check(f"HTTP {code} raised by the opener is redirected", status == "redirected", status)
        fake = _FakeHTTP(code, b"", {"Location": "http://127.0.0.1:9/steal"})
        chain = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect, fake)
        status, _, _ = run(None, opener=chain)
        check(f"urllib's chain does not follow HTTP {code}", status == "redirected" and fake.urls == [url],
              f"{status} {fake.urls}")
    fake = _FakeHTTP(200, good, {"Content-Type": "application/json"})
    status, found, _ = run(None, opener=urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect, fake))
    check("urllib's own answer object is read", status == "ok" and len(found) == 2, status)
    redirects = [h for h in _OPENER.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
    proxies = [h for h in _OPENER.handlers if isinstance(h, urllib.request.ProxyHandler)]
    plain = [h for h in _OPENER.handlers if type(h) is urllib.request.HTTPHandler or isinstance(h, _LoopbackHandler)]
    check("the real opener has the no-redirect handler and no other", redirects
          and all(isinstance(h, _NoRedirect) for h in redirects), str(redirects))
    # ProxyHandler({}) holds no proxy, so the opener keeps no proxy step at all (and builds no default one)
    check("the real opener has no proxy step", not any(getattr(h, "proxies", None) for h in proxies), str(proxies))
    check("the real opener sends http through the loopback connection only",
          plain and all(isinstance(h, _LoopbackHandler) for h in plain), str(plain))

    # -- the real opener on sockets of these checks (127.0.0.1 only: no network)
    stub, other, proxy = (_Stub(_http_answer(200, "OK", good)) for _ in range(3))
    moved = _Stub(_http_answer(302, "Found", b"", f"Location: http://127.0.0.1:{other.port}/steal\r\n"))
    saved = {name: os.environ.get(name) for name in ("HTTP_PROXY", "http_proxy")}
    try:
        for name in saved:                          # a proxy of the system must never get the token
            os.environ[name] = f"http://127.0.0.1:{proxy.port}"
        found, status = recall({**cfg, URL_LINE: f"http://localhost:{stub.port}"}, "When do I descale the kettle?")
        head, rest = stub.seen[0] if stub.seen else (b"", b"")
        lines = head.decode("latin-1").split("\r\n")
        named = {line.split(":", 1)[0].strip().lower(): line.split(":", 1)[1].strip() for line in lines[1:] if ":" in line}
        check("on the wire: ok, one request to the hub and none to the proxy of the system",
              status == "ok" and len(stub.seen) == 1 and not proxy.seen, f"{status} {len(stub.seen)} {len(proxy.seen)}")
        check("on the wire: POST /api/recall, no query in the request line",
              lines[0] == f"POST {RECALL_PATH} HTTP/1.1", lines[0])
        check("on the wire: Host is localhost:<port>, the token in Authorization only",
              named.get("host") == f"localhost:{stub.port}" and named.get("authorization") == f"Bearer {token}"
              and head.decode("latin-1").count(token) == 1 and token.encode() not in rest, "")
        check("on the wire: the body is the JSON question",
              _json_of(rest) == {"query": "When do I descale the kettle?", "limit": 3, "all_projects": True},
              rest[:120].decode("latin-1"))
        found, status = recall({**cfg, URL_LINE: f"http://127.0.0.1:{moved.port}"}, "kettle")
        check("on the wire: a 302 is not followed: the other address hears nothing",
              status == "redirected" and len(moved.seen) == 1 and not other.seen, f"{status} {len(other.seen)}")
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        for each in (stub, other, proxy, moved):
            each.close()
    closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed.bind(("127.0.0.1", 0))
    free = closed.getsockname()[1]
    closed.close()                                  # nothing listens on this port now
    started = time.monotonic()
    found, status = recall({**cfg, URL_LINE: f"http://127.0.0.1:{free}"}, "kettle")
    took = time.monotonic() - started
    check("on the wire: a hub that is down is unreachable, at once (no 2 s wait on Windows)",
          status == "unreachable" and took < 1.5, f"{status} after {took:.2f} s")
    silent = _Stub(None)
    started = time.monotonic()
    found, status = recall({**cfg, URL_LINE: f"http://127.0.0.1:{silent.port}"}, "kettle", budget=0.4)
    took = time.monotonic() - started
    silent.close()
    check("on the wire: a hub that never answers is a timeout, within the budget", status == "timeout"
          and took < 1.0, f"{status} after {took:.2f} s")
    # A slow hub: a byte every 10 ms never lets one read wait long, so only the ONE deadline can end it. When the recall
    # returns, its helper thread is gone and its socket is closed (the stub's next send fails: `cut_at`).
    slow_body = good + b" " * 400
    for part, head_at_once in (("its head", False), ("its body", True)):
        slow = _Stub(_http_answer(200, "OK", slow_body), drip=0.01, head_at_once=head_at_once)
        before = {each for each in threading.enumerate() if each.name == "jarviz-ali-memory"}
        started = time.monotonic()
        found, status = recall({**cfg, URL_LINE: f"http://127.0.0.1:{slow.port}"}, "kettle", budget=0.4)
        returned = time.monotonic()
        alive = [each for each in threading.enumerate() if each.name == "jarviz-ali-memory" and each not in before]
        slow.cut.wait(1.0)
        slow.close()
        gap = slow.cut_at - returned if slow.cut_at is not None else None
        check(f"on the wire: a hub that sends {part} a byte at a time is a timeout, within the budget",
              status == "timeout" and not found and returned - started < 0.4 + UNWIND_S + 0.2,
              f"{status} after {returned - started:.2f} s")
        check(f"on the wire: a hub that sends {part} a byte at a time: no helper thread outlives the recall",
              not alive, f"{len(alive)} still running")
        check(f"on the wire: a hub that sends {part} a byte at a time: the socket is closed when the recall returns",
              gap is not None and gap < 0.25, "never" if gap is None else f"{gap:.2f} s after the return")
    return results


def main() -> int:
    results = offline_checks()
    for name, passed, detail in results:
        print(("ok    " if passed else "FAIL  ") + name + ("" if passed else f"  -> {detail}"))
    failed = sum(1 for _, passed, _ in results if not passed)
    print(f"ALI MEMORY OFFLINE: {len(results) - failed} pass, {failed} fail")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
