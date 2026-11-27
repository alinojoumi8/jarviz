"""Jarviz voices: one way to turn text into speech with the voice named in config.json.

    "voice": "browser" | "deepgram"

The browser voice is the page's own speech: the server does nothing for it. Deepgram (its Aura text-to-speech) is
spoken on the server: the page sends a text, the server asks Deepgram with the key from config.json, and the page
plays the mp3 that comes back. The key never leaves the server.

A new server voice is one entry in VOICES, its key line and voice line in config.json, and its own call.

    python voices.py     the offline checks (a fake opener: no network)
"""
from __future__ import annotations

import http.client
import io
import json
import random
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
from pathlib import Path
from typing import Any, Protocol

# The same word as brains.KEY_PLACEHOLDER: config.json has one placeholder for every key. It is written here, not
# imported, so that this file stands on its own.
KEY_PLACEHOLDER = "PUT-YOUR-KEY-HERE"
KEY_REMOVED = "[key removed]"
USER_AGENT = "Jarviz/1.0"
SPEAK_TIMEOUT_S = 30
# Deepgram takes at most 2000 characters in one request (more is HTTP 413). The room below that is on purpose:
# Deepgram may count characters in another way than Python does. The caller cuts a longer text with split_text().
MAX_TEXT_CHARS = 1900
MAX_AUDIO_BYTES = 8 * 1024 * 1024       # far more than the mp3 of 1900 characters: a bigger answer is not speech
MIN_AUDIO_BYTES = 200                   # a smaller answer holds no real speech
MAX_ERROR_BYTES = 64 * 1024             # an error body is read only this far
DETAIL_CHARS = 300
AUDIO_MIME = "audio/mpeg"
PROBE_TEXT = "OK."
# Only this plain shape goes into the address: no space, "&", "?", "/" or "%" can change the request.
VOICE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,59}")
# Voice ids that exist (Deepgram's documents). The probe names some when Deepgram does not know the configured one.
KNOWN_VOICES = ("aura-2-draco-en", "aura-2-pandora-en", "aura-2-odysseus-en", "aura-2-thalia-en", "aura-helios-en")
# A sentence end: ". ! ?" (and a closing quote or bracket) with a space after it.
SENTENCE_END_RE = re.compile(r"[.!?]+[\"'\u201d\u2019)\]]*(?=\s)")

# kind "browser": the page speaks, the server does nothing.  kind "deepgram": POST {base_url}/speak.
VOICES = {
    "browser": {"label": "browser", "kind": "browser"},
    "deepgram": {"label": "Deepgram", "kind": "deepgram", "base_url": "https://api.deepgram.com/v1",
                 "key": "deepgram_api_key", "model_key": "deepgram_voice", "default_model": "aura-2-draco-en"},
}
DEFAULT_VOICE = "browser"
# What a person may call a voice, with everything but letters removed.
ALIASES = {"deepgram": "deepgram", "deepgramaura": "deepgram", "aura": "deepgram",
           "browser": "browser", "browsers": "browser", "builtin": "browser", "normal": "browser",
           "default": "browser", "system": "browser", "chrome": "browser"}


class Opener(Protocol):
    """What speak() needs: urllib's own opener, or a fake one in the offline checks."""

    def open(self, request: urllib.request.Request, timeout: float = ...) -> Any: ...


class VoiceError(Exception):
    """The voice could not speak. `code` says why. `detail` is Deepgram's own short message, or ours.

    The key is taken out of the detail BEFORE the detail is cut to one line of 300 characters: a cut first can
    leave a piece of the key at its end (a review finding in brains.py). The exception text is the code only."""

    def __init__(self, code: str, detail: str = "", voice: dict | None = None, key: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = _one_line(scrub_key(str(detail), key))[:DETAIL_CHARS]
        self.label = (voice or {}).get("label", "the")
        self.model = _one_line(scrub_key(str((voice or {}).get("model", "")), key))[:80]


def _one_line(text: str) -> str:
    return " ".join(text.split())


def scrub_key(text: str, key: str) -> str:
    """Every copy of the key in a text becomes "[key removed]": a provider can say the key back in an error text.
    A short key too: its length decides nothing. Call it on the whole text, BEFORE any cut. The placeholder is no
    secret, and it is never sent."""
    text, key = str(text), str(key or "").strip()
    if not key or key == KEY_PLACEHOLDER:
        return text
    return text.replace(key, KEY_REMOVED)


# ------------------------------------------------------------------- choosing

def default_config() -> dict:
    """The voice lines of config.json: which voice is active, and a key and a voice id for each server voice."""
    config: dict = {"voice": DEFAULT_VOICE}
    for spec in VOICES.values():
        if "key" in spec:
            config[spec["key"]] = KEY_PLACEHOLDER
            config[spec["model_key"]] = spec["default_model"]
    return config


def resolve_name(spoken: str) -> str | None:
    """ "Deep Gram", "the browser voice", "built in" -> the name used in config.json, or None."""
    squashed = re.sub(r"[^a-z]", "", str(spoken).lower())
    squashed = re.sub(r"^the|voice$", "", squashed)
    return ALIASES.get(squashed)


def active_name(config: dict) -> str:
    name = str(config.get("voice", "")).strip().lower()
    return name if name in VOICES else DEFAULT_VOICE


def _key_of(config: dict, spec: dict) -> str:
    """The key from config.json, or "" when there is none yet."""
    key = str(config.get(spec["key"]) or "").strip()
    return "" if key == KEY_PLACEHOLDER else key


def _voice_id(config: dict, spec: dict) -> str:
    return str(config.get(spec["model_key"]) or "").strip() or spec["default_model"]


def _voice_of(config: dict, name: str) -> dict:
    """What an error needs to know about a server voice."""
    spec = VOICES[name]
    return {"name": name, "label": spec["label"], "model": _voice_id(config, spec)}


def describe(config: dict, name: str | None = None) -> dict:
    """What the browser may know about a voice. Never the key itself: a key pasted into the voice line shows as
    "[key removed]". The browser voice needs no key, so its "has_key" is True."""
    name = name or active_name(config)
    spec = VOICES[name]
    if spec["kind"] == "browser":
        return {"name": name, "label": spec["label"], "model": "", "has_key": True}
    key = _key_of(config, spec)
    return {"name": name, "label": spec["label"], "model": scrub_key(_voice_id(config, spec), key),
            "has_key": bool(key)}


def describe_all(config: dict) -> list[dict]:
    return [describe(config, name) for name in VOICES]


# ------------------------------------------------------------------- speaking

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None                      # a key must never follow a redirect to another address


_OPENER = urllib.request.build_opener(_NoRedirect)


def speak(config: dict, text: str, name: str = "deepgram", opener: Opener | None = None) -> tuple[bytes, str]:
    """The mp3 audio of `text` in the voice that config.json names, and its MIME type. Raises VoiceError.
    One request: the caller cuts a text longer than MAX_TEXT_CHARS with split_text(). `opener` serves the offline
    checks only."""
    spec = VOICES.get(name) if isinstance(name, str) else None
    if spec is None or spec["kind"] != "deepgram":
        why = "The browser voice is spoken by the page itself." if spec else "Jarviz has no server voice of that name."
        raise VoiceError("voice_not_served", why, spec)
    voice, key = _voice_of(config, name), _key_of(config, spec)
    if not key:
        raise VoiceError("voice_key_missing", _no_key(spec), voice)
    if not isinstance(text, str) or not text.strip():
        raise VoiceError("voice_empty", "There is no text to speak.", voice, key)
    text = text.strip()
    if len(text) > MAX_TEXT_CHARS:
        raise VoiceError("voice_too_long", f"The text has {len(text)} characters. One request takes at most "
                         f"{MAX_TEXT_CHARS}: cut it with split_text().", voice, key)
    trouble = _voice_id_trouble(voice["model"], key, spec)
    if trouble:
        raise VoiceError("voice_not_found", trouble, voice, key)          # no request is sent
    return _deepgram_once(key, voice, spec, text, opener or _OPENER)


def _no_key(spec: dict) -> str:
    return f'config.json has no {spec["label"]} key yet. Type it into "{spec["key"]}".'


def _voice_id_trouble(model: str, key: str, spec: dict) -> str:
    """Why this voice id may not go into the address, or "". A key pasted into the voice line would travel in the
    address, and addresses end up in logs."""
    line = spec["model_key"]
    if key and key in model:
        return f'The "{line}" line of config.json holds the key. The key belongs in "{spec["key"]}".'
    if not VOICE_ID_RE.fullmatch(model):
        return f'The "{line}" line of config.json is not a voice id (letters, digits, ".", "_" and "-", at most 60).'
    return ""


def _deepgram_once(key: str, voice: dict, spec: dict, text: str, opener: Opener) -> tuple[bytes, str]:
    # ALWAYS ask for mp3: Deepgram's documents disagree about the default encoding.
    url = spec["base_url"] + "/speak?" + urllib.parse.urlencode({"model": voice["model"], "encoding": "mp3"})
    request = urllib.request.Request(
        url, method="POST", data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Authorization": f"Token {key}", "Content-Type": "application/json",
                 "Accept": AUDIO_MIME, "User-Agent": USER_AGENT})
    try:
        with opener.open(request, timeout=SPEAK_TIMEOUT_S) as response:
            status = getattr(response, "status", None)
            headers = getattr(response, "headers", None)
            ctype = headers.get("Content-Type", "") if headers is not None else ""
            data = _read_capped(response, MAX_AUDIO_BYTES)
    except urllib.error.HTTPError as error:              # a redirect lands here too: _NoRedirect did not follow it
        raise _http_error(error.code, _json_of(_error_body(error)), voice, key) from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
        # HTTPException: the answer broke off in the middle (IncompleteRead).
        raise VoiceError("voice_unreachable", _why(error), voice, key) from None
    trouble = _audio_trouble(status, ctype if isinstance(ctype, str) else "", data)
    if trouble:
        raise VoiceError("voice_bad_reply", trouble, voice, key)
    return data, AUDIO_MIME


def _read_capped(response: Any, limit: int) -> bytes:
    """At most limit + 1 bytes: one more than allowed is enough to know that the answer is too big."""
    chunks: list[bytes] = []
    size = 0
    while size <= limit:
        chunk = response.read(min(64 * 1024, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _error_body(error: urllib.error.HTTPError) -> bytes:
    try:
        return error.read(MAX_ERROR_BYTES) or b""
    except (OSError, http.client.HTTPException, ValueError, AttributeError):
        return b""                          # the error body did not come (a timeout): the status still says why


def _json_of(raw: bytes) -> dict:
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {}                           # a body cut at MAX_ERROR_BYTES is not JSON: only the status speaks
    return parsed if isinstance(parsed, dict) else {}


def _error_text(payload: dict) -> tuple[str, str]:
    """(Deepgram's own message, its code words) from an error body: err_code/err_msg, or the newer category/message.
    Only text counts: the body is data from a stranger."""
    detail = next((text for text in (payload.get("err_msg"), payload.get("message"))
                   if isinstance(text, str) and text.strip()), "")
    words = " ".join(word for word in (payload.get("err_code"), payload.get("category")) if isinstance(word, str))
    return detail, words


def _http_error(status: int, payload: dict, voice: dict, key: str) -> VoiceError:
    detail, words = _error_text(payload)
    text = f"{detail} {words}".lower()
    if 300 <= status < 400:
        return VoiceError("voice_bad_reply", f"{voice['label']} answered with a redirect (HTTP {status}). Jarviz never "
                          "follows one: the key would go with it.", voice, key)
    if status == 401:
        code = "voice_key_invalid"
    elif status == 402:
        code = "voice_no_credit"
    elif status == 403:
        code = "voice_no_access"
    elif status == 400 and "no such model" in text:
        code = "voice_not_found"
    elif status == 413:
        code = "voice_too_long"
    elif status == 429:
        code = "voice_busy"
    elif status == 408 or status >= 500:
        code = "voice_error"
    elif 400 <= status < 500:
        code = "voice_bad_request"
    else:
        code = "voice_bad_reply"
    return VoiceError(code, detail or f"HTTP {status}", voice, key)


def _why(error: Exception) -> str:
    """The name of what broke, never its text: an error text can hold an address."""
    name = type(error).__name__
    reason = getattr(error, "reason", None)
    if isinstance(error, urllib.error.URLError) and isinstance(reason, BaseException):
        name += f" ({type(reason).__name__})"
    return name


def _audio_trouble(status: Any, ctype: str, data: bytes) -> str:
    """What is wrong with an answer that came back, or "" for good mp3 audio. The answer is data from a stranger: its
    type line alone is not trusted, its first bytes must look like mp3 too."""
    if status != 200:
        return f"The answer was HTTP {status if isinstance(status, int) else '?'}, not 200."
    if not ctype.strip().lower().startswith("audio/"):
        return "The answer is not audio: its type line does not say audio/."
    if len(data) > MAX_AUDIO_BYTES:
        return f"The answer is bigger than {MAX_AUDIO_BYTES} bytes."
    if len(data) < MIN_AUDIO_BYTES:
        return f"The answer has only {len(data)} bytes."
    if not looks_like_mp3(data):
        return "The answer does not start like mp3 audio."
    return ""


def looks_like_mp3(data: bytes) -> bool:
    """Preflight uses it too, on what /voice/speak sends."""
    # An mp3 starts with an ID3 tag ("ID3") or with an MPEG audio frame. A frame starts with eleven set bits (the
    # frame sync): the byte 0xFF, then a byte whose top three bits are set.
    return data[:3] == b"ID3" or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)


# ------------------------------------------------------------------ splitting

def split_text(text: str, limit: int = MAX_TEXT_CHARS) -> list[str]:
    """Cut a long text into pieces that each fit one request: at the last sentence end that fits (". ! ?" with a
    space after it), else at the last space, else hard (a word longer than `limit`). No piece is empty, none is
    longer than `limit`. Joined with spaces, the pieces give the words of the text back; a word that had to be cut
    hard comes back in parts."""
    if not isinstance(text, str):
        return []
    limit = max(1, int(limit))            # a limit of 0 would never end
    pieces: list[str] = []
    rest = text.strip()
    while rest:
        if len(rest) <= limit:
            pieces.append(rest)
            break
        cut = _cut_at(rest, limit)
        pieces.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return pieces


def _cut_at(rest: str, limit: int) -> int:
    """Where to cut `rest` (longer than `limit`, no space at its start) so that the piece before the cut fits."""
    window = rest[:limit + 1]             # the character after the limit can be the space that ends a fitting piece
    ends = [match.end() for match in SENTENCE_END_RE.finditer(window)]
    if ends:
        return ends[-1]
    for at in range(limit, 0, -1):
        if window[at].isspace():
            return at
    return limit


# ---------------------------------------------------------------------- probe

def probe(config: dict, name: str = "deepgram", opener: Opener | None = None) -> dict:
    """One real, very small request on the SAME path as speak(): the text "OK.". A voice list does not prove that the
    key may speak with the voice. Returns {"key": (ok, detail), "voice": (ok, detail)}. ok is True, False, or None
    for "not checked". None is never a pass."""
    spec = VOICES.get(name) if isinstance(name, str) else None
    if spec is None or spec["kind"] != "deepgram":
        why = ("The page speaks with the browser voice. The server has nothing to check." if spec
               else "Jarviz has no server voice of that name.")
        return {"key": (None, why), "voice": (None, why)}
    key, voice = _key_of(config, spec), _voice_of(config, name)
    if not key:
        return {"key": (False, _no_key(spec)), "voice": (None, "")}
    trouble = _voice_id_trouble(voice["model"], key, spec)
    if trouble:
        model = scrub_key(voice["model"], key)
        return {"key": (None, "Not checked: Jarviz sends no request with this voice line."),
                "voice": (False, _voice_fix(spec, model, f"The voice id {model!r} cannot be used. {trouble}"))}
    try:
        data, _ = speak(config, PROBE_TEXT, name, opener)
    except VoiceError as error:
        return _probe_trouble(spec, error)
    return {"key": (True, "the key is good"),
            "voice": (True, f"{voice['model']}: {len(data)} bytes of audio for {PROBE_TEXT!r}")}


def _probe_trouble(spec: dict, error: VoiceError) -> dict:
    label, code, detail = spec["label"], error.code, error.detail
    if code in ("voice_key_invalid", "voice_no_access"):
        return {"key": (False, f"{label} refused the key ({code}). {detail}".strip()), "voice": (None, "")}
    if code == "voice_unreachable":
        return {"key": (False, f"Cannot reach {label}. Check the internet connection. ({detail})"), "voice": (None, "")}
    if code == "voice_no_credit":
        return {"key": (True, "the key is good"),
                "voice": (False, f"The {label} account has no credit. {detail}".strip())}
    if code == "voice_not_found":
        return {"key": (True, "the key is good"),
                "voice": (False, _voice_fix(spec, error.model,
                                            f"{label} does not know the voice id {error.model!r}. {detail}".strip()))}
    return {"key": (None, f"Not proven: {label} did not say whether the key is good ({code})."),
            "voice": (False, f"{label} could not speak the test text ({code}). {detail}".strip())}


def _voice_fix(spec: dict, model: str, text: str) -> str:
    others = [voice for voice in KNOWN_VOICES if voice != model][:3]
    return f"{text} Some ids that exist: {', '.join(others)}. Fix \"{spec['model_key']}\" in config.json."


# ------------------------------------------------------------- offline checks

class _FakeResponse:
    """A stand-in for an HTTPS answer. `broken` breaks the read in the middle."""

    def __init__(self, status: int, body: bytes, ctype: str | None, broken: bool = False):
        self.status = status
        self.headers = http.client.HTTPMessage()
        if ctype is not None:
            self.headers["Content-Type"] = ctype
        self._body, self._broken = io.BytesIO(body), broken

    def read(self, size: int = -1) -> bytes:
        if self._broken:
            raise http.client.IncompleteRead(b"", 1000)
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _FakeOpener:
    """A stand-in for the opener. It sends nothing: `make(request)` gives the answer or raises."""

    def __init__(self, make):
        self.make = make
        self.calls: list[dict] = []

    def open(self, request: urllib.request.Request, timeout: float | None = None):
        self.calls.append({"url": request.full_url, "method": request.get_method(), "body": request.data,
                           "headers": {name.lower(): value for name, value in request.header_items()},
                           "timeout": timeout})
        return self.make(request)


class _FakeHTTPS(urllib.request.HTTPSHandler):
    """urllib's own chain with a fake last step that answers without a network. It proves what an opener built with
    _NoRedirect does with a redirect."""

    def __init__(self, status: int, body: bytes, headers: dict):
        super().__init__()
        self.status, self.body, self.extra = status, body, headers
        self.urls: list[str] = []

    def https_open(self, request):
        self.urls.append(request.full_url)
        headers = http.client.HTTPMessage()
        for name, value in self.extra.items():
            headers[name] = value
        response = urllib.response.addinfourl(io.BytesIO(self.body), headers, request.full_url, self.status)
        response.msg = "fake"
        return response


def offline_checks() -> list[tuple[str, bool, str]]:
    """Fast checks with a fake opener: no network, no file. They prove the logic of the code. The real Deepgram is
    proved by probe() (preflight)."""
    results: list[tuple[str, bool, str]] = []

    def check(label: str, passed, detail: str = "") -> None:
        results.append((label, bool(passed), detail))

    key = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"          # a fake key, 40 characters like a real one
    cfg = {**default_config(), "voice": "deepgram", "deepgram_api_key": key}
    mp3 = b"ID3" + bytes(300)
    frame = bytes([0xFF, 0xFB]) + bytes(300)
    url = "https://api.deepgram.com/v1/speak?model=aura-2-draco-en&encoding=mp3"

    def answer(status: int = 200, body: bytes = mp3, ctype: str | None = "audio/mpeg", broken: bool = False):
        return lambda request: _FakeResponse(status, body, ctype, broken)

    def refuse(status: int, payload: dict | None = None, raw: bytes | None = None):
        body = raw if raw is not None else json.dumps(payload or {}).encode("utf-8")

        def make(request):
            raise urllib.error.HTTPError(request.full_url, status, "error", http.client.HTTPMessage(), io.BytesIO(body))
        return make

    def fails(error: BaseException):
        def make(request):
            raise error
        return make

    def run(make, config: dict = cfg, text=" Good evening. ", name="deepgram", opener=None):
        """(the VoiceError code, or "ok:<mime>:<bytes>", or "CRASH ..."), the opener, the error."""
        opener = opener or _FakeOpener(make)
        try:
            data, mime = speak(config, text, name, opener)
            return f"ok:{mime}:{len(data)}", opener, data
        except VoiceError as error:
            return error.code, opener, error
        except Exception as error:      # noqa: BLE001 - a crash is what these checks look for
            return f"CRASH {type(error).__name__}: {error}", opener, None

    # -- choosing
    check("default_config: the voice lines", default_config()
          == {"voice": "browser", "deepgram_api_key": KEY_PLACEHOLDER, "deepgram_voice": "aura-2-draco-en"},
          str(default_config()))
    try:                                # read, not imported: importing brains.py would start its own machinery
        found = re.search(r'^KEY_PLACEHOLDER = "([^"]*)"', Path(__file__).with_name("brains.py").read_text("utf-8"),
                          re.MULTILINE)
        same, why = (found is not None and found.group(1) == KEY_PLACEHOLDER), "brains.py has another placeholder"
    except (OSError, ValueError) as error:          # a check that cannot look says so
        same, why = False, f"could not look: brains.py could not be read ({type(error).__name__})"
    check("the key placeholder is the same word as in brains.py", same, why)
    check("the default voice id is a known voice", VOICES["deepgram"]["default_model"] in KNOWN_VOICES, "")
    spoken = {"deepgram": "deepgram", "Deep Gram": "deepgram", "the Deepgram voice": "deepgram", "aura": "deepgram",
              "Aura voice": "deepgram", "browser": "browser", "the browser voice": "browser", "built in": "browser",
              "built-in voice": "browser", "normal": "browser", "default": "browser", "the system voice": "browser",
              "Chrome": "browser", "the browser's voice": "browser",
              "eleven labs": None, "": None, "voice": None, "the": None, "None": None}
    heard = {words: resolve_name(words) for words in spoken}
    check("resolve_name: the spoken names", heard == spoken, str({w: h for w, h in heard.items() if spoken[w] != h}))
    check("resolve_name: something that is not text is no voice",
          resolve_name(None) is None and resolve_name(42) is None, "")
    actives = [active_name(c) for c in ({"voice": "deepgram"}, {"voice": " Deepgram "}, {"voice": "nonsense"}, {},
                                        {"voice": None}, {"voice": 5})]
    check("active_name: a known voice, else the browser", actives == ["deepgram", "deepgram"] + ["browser"] * 4,
          str(actives))
    check("describe: the browser voice",
          describe(cfg, "browser") == {"name": "browser", "label": "browser", "model": "", "has_key": True},
          str(describe(cfg, "browser")))
    fresh = describe(default_config(), "deepgram")
    check("describe: no key yet", fresh == {"name": "deepgram", "label": "Deepgram", "model": "aura-2-draco-en",
                                            "has_key": False}, str(fresh))
    check("describe: the active voice when no name is given", describe(cfg)["name"] == "deepgram", "")
    check("describe: a key is there, and never shown", describe(cfg, "deepgram")["has_key"] is True
          and key not in json.dumps(describe_all(cfg)), "")
    pasted = {**cfg, "deepgram_voice": key}
    check("describe: a key pasted into the voice line is not shown",
          key not in json.dumps(describe_all(pasted)) and describe(pasted, "deepgram")["model"] == KEY_REMOVED, "")
    check("describe_all: every voice", [seen["name"] for seen in describe_all(cfg)] == list(VOICES), "")
    check("describe: four fields only",
          all(set(seen) == {"name", "label", "model", "has_key"} for seen in describe_all(cfg)), "")

    # -- the request that is built
    code, opener, data = run(answer())
    call = opener.calls[0] if opener.calls else {}
    headers = call.get("headers", {})
    check("speak: a good mp3 answer comes back as bytes", code == f"ok:{AUDIO_MIME}:{len(mp3)}" and data == mp3, code)
    check("speak: one request", len(opener.calls) == 1, str(len(opener.calls)))
    check("speak: the address names the voice and encoding=mp3", call.get("url") == url, str(call.get("url")))
    check("speak: POST", call.get("method") == "POST", str(call.get("method")))
    check("speak: the Token header", headers.get("authorization") == f"Token {key}", "")
    check("speak: the other headers", headers.get("content-type") == "application/json"
          and headers.get("accept") == "audio/mpeg" and headers.get("user-agent") == USER_AGENT, str(sorted(headers)))
    try:
        body = json.loads(call.get("body") or b"")
    except ValueError:
        body = None
    check("speak: the JSON body holds the stripped text", body == {"text": "Good evening."}, str(body))
    check("speak: the timeout", call.get("timeout") == SPEAK_TIMEOUT_S, str(call.get("timeout")))
    code, opener, _ = run(answer(), {**cfg, "deepgram_voice": " aura-2-thalia-en "})
    check("speak: the voice id from config.json", opener.calls and opener.calls[0]["url"]
          == url.replace("aura-2-draco-en", "aura-2-thalia-en"), code)
    code, opener, _ = run(answer(), {**cfg, "deepgram_voice": "   "})
    check("speak: an empty voice line is the default voice", opener.calls and opener.calls[0]["url"] == url, code)
    code, opener, _ = run(answer(), {**cfg, "deepgram_voice": "a" * 60})
    check("speak: a voice id of 60 characters is sent", code.startswith("ok:"), code)

    # -- refused before any request
    for bad in ("aura 2", "-aura", "aura&encoding=wav", "aura?x=1", "../listen", "aura%20x", "a" * 61, "\u00e4ura",
                "aura\nx", "aura/en", key):
        code, opener, error = run(answer(), {**cfg, "deepgram_voice": bad})
        shown = f"{getattr(error, 'detail', '')} {getattr(error, 'model', '')}"
        what = "the key itself" if bad == key else ascii(bad[:12])
        check(f"speak: the voice id {what} sends no request", code == "voice_not_found" and not opener.calls
              and key not in shown, code)
    for missing in (KEY_PLACEHOLDER, "", "   ", None):
        code, opener, _ = run(answer(), {**cfg, "deepgram_api_key": missing})
        check(f"speak: no key ({missing!r}) is voice_key_missing",
              code == "voice_key_missing" and not opener.calls, code)
    code, opener, _ = run(answer(), {k: v for k, v in cfg.items() if k != "deepgram_api_key"})
    check("speak: no key line is voice_key_missing", code == "voice_key_missing" and not opener.calls, code)
    for name in ("browser", "nonsense", None):
        code, opener, _ = run(answer(), name=name)
        check(f"speak: {name!r} is voice_not_served", code == "voice_not_served" and not opener.calls, code)
    for text in ("", "   \n", None, 42, ["Hello."]):
        code, opener, _ = run(answer(), text=text)
        check(f"speak: the text {text!r} is voice_empty", code == "voice_empty" and not opener.calls, code)
    code, opener, _ = run(answer(), text="a" * (MAX_TEXT_CHARS + 1))
    check("speak: a text over the limit is voice_too_long", code == "voice_too_long" and not opener.calls, code)
    code, _, _ = run(answer(), text="  " + "a" * MAX_TEXT_CHARS + "  ")
    check("speak: a text at the limit is sent", code.startswith("ok:"), code)

    # -- the errors of Deepgram
    statuses = [
        (401, {"err_code": "INVALID_AUTH", "err_msg": "Invalid credentials.", "request_id": "r-1"},
         "voice_key_invalid"),
        (402, {"err_msg": "Payment required."}, "voice_no_credit"),
        (403, {"err_code": "INSUFFICIENT_PERMISSIONS", "err_msg": "No access."}, "voice_no_access"),
        (400, {"err_msg": "No such model/language/tier combination found."}, "voice_not_found"),
        (400, {"category": "x", "message": "No Such Model/language/tier combination found"}, "voice_not_found"),
        (400, {"err_msg": "Bad request."}, "voice_bad_request"),
        (404, {"err_msg": "Not found."}, "voice_bad_request"),
        (413, {"err_msg": "Too long."}, "voice_too_long"),
        (429, {"err_msg": "Slow down."}, "voice_busy"),
        (408, {}, "voice_error"),
        (500, {"err_msg": "Oops."}, "voice_error"),
        (503, {}, "voice_error"),
    ]
    for status, payload, want in statuses:
        code, _, _ = run(refuse(status, payload))
        check(f"speak: HTTP {status} {(payload.get('err_msg') or payload.get('message') or '')[:20]!r} is {want}",
              code == want, code)
    _, _, error = run(refuse(401, {"err_code": "INVALID_AUTH", "err_msg": "Invalid\ncredentials.  Check it."}))
    check("speak: the detail is Deepgram's own err_msg on one line",
          getattr(error, "detail", "") == "Invalid credentials. Check it.", getattr(error, "detail", ""))
    _, _, error = run(refuse(400, {"category": "x", "message": "No such model/language/tier combination found"}))
    check("speak: the newer shape gives its message", getattr(error, "detail", "").startswith("No such model"), "")
    _, _, error = run(refuse(502, raw=b"<html>Bad gateway</html>"))
    check("speak: a body that is not JSON says the status", getattr(error, "code", "") == "voice_error"
          and error.detail == "HTTP 502", getattr(error, "detail", ""))
    _, _, error = run(refuse(400, {"err_msg": {"nested": True}}))
    check("speak: an err_msg that is not text is not used", getattr(error, "detail", "") == "HTTP 400",
          getattr(error, "detail", ""))
    for broken in (urllib.error.URLError(OSError("no route")), TimeoutError(), ConnectionResetError(),
                   http.client.RemoteDisconnected("closed")):
        code, _, _ = run(fails(broken))
        check(f"speak: {type(broken).__name__} is voice_unreachable", code == "voice_unreachable", code)
    code, _, _ = run(answer(broken=True))
    check("speak: an answer that breaks off is voice_unreachable", code == "voice_unreachable", code)

    # -- the key never travels on
    _, _, error = run(refuse(401, {"err_msg": f"Invalid credentials for {key}. Key {key} refused."}))
    detail = getattr(error, "detail", "")
    check("scrub: a key said back in an error text is removed", key not in detail and KEY_REMOVED in detail, detail)
    _, _, error = run(refuse(401, {"err_msg": "x" * 290 + key + " tail"}))
    detail = getattr(error, "detail", "")
    check("scrub: no piece of the key is left at the 300-character cut",
          len(detail) <= DETAIL_CHARS and key[:4] not in detail, detail[-20:])
    short = {**cfg, "deepgram_api_key": "k3y99"}
    _, _, error = run(refuse(401, {"message": "bad k3y99 here, k3y99 again"}), short)
    check("scrub: a key of 5 characters is removed too", "k3y99" not in getattr(error, "detail", "k3y99"),
          getattr(error, "detail", ""))
    _, _, error = run(refuse(401, {"err_msg": "x" * 296 + "k3y99"}), short)
    check("scrub: a key of 5 characters at the cut", "k3" not in getattr(error, "detail", "k3"),
          getattr(error, "detail", ""))
    _, _, error = run(refuse(401, {"err_msg": f"no {key}"}))
    check("scrub: the exception text is the code only", error is not None and str(error) == "voice_key_invalid"
          and error.args == ("voice_key_invalid",), str(error))
    long_error = VoiceError("x", "word\n" * 200)
    check("VoiceError: the detail is one line of at most 300 characters",
          len(long_error.detail) == DETAIL_CHARS and "\n" not in long_error.detail, str(len(long_error.detail)))
    check("scrub_key: every copy, also of a short key", scrub_key("aXa X", "X") == f"a{KEY_REMOVED}a {KEY_REMOVED}", "")
    check("scrub_key: no key and the placeholder change nothing", scrub_key("abc", "") == "abc"
          and scrub_key(f"x {KEY_PLACEHOLDER}", KEY_PLACEHOLDER) == f"x {KEY_PLACEHOLDER}", "")

    # -- a redirect is never followed
    for status in (301, 302, 303, 307, 308):
        code, _, _ = run(refuse(status))
        check(f"speak: HTTP {status} raised by the opener is voice_bad_reply", code == "voice_bad_reply", code)
        fake = _FakeHTTPS(status, b"", {"Location": "https://elsewhere.example/steal"})
        chain = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect, fake)
        code, _, _ = run(None, opener=chain)
        check(f"speak: urllib's chain does not follow HTTP {status}", code == "voice_bad_reply" and fake.urls == [url],
              f"{code} {fake.urls}")
    fake = _FakeHTTPS(200, mp3, {"Content-Type": "audio/mpeg"})
    code, _, _ = run(None, opener=urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect, fake))
    check("speak: urllib's own answer object is read", code == f"ok:{AUDIO_MIME}:{len(mp3)}", code)
    redirects = [h for h in getattr(_OPENER, "handlers", []) if isinstance(h, urllib.request.HTTPRedirectHandler)]
    check("the real opener has the no-redirect handler and no other",
          redirects and all(isinstance(h, _NoRedirect) for h in redirects), str(redirects))

    # -- answers that are not good audio
    bad_replies = {
        "a JSON answer": answer(ctype="application/json", body=b'{"ok": true}' + bytes(300)),
        "an html type line": answer(ctype="text/html"),
        "no type line": answer(ctype=None),
        "html bytes under an audio type line": answer(body=b"<html>" + bytes(300)),
        "wav bytes under the mp3 type line": answer(body=b"RIFF" + bytes(300)),
        "0xFF without the frame bits": answer(body=bytes([0xFF, 0x1F]) + bytes(300)),
        "too small": answer(body=b"ID3" + bytes(MIN_AUDIO_BYTES - 4)),
        "too big": answer(body=b"ID3" + bytes(MAX_AUDIO_BYTES - 2)),
        "HTTP 204": answer(status=204),
        "HTTP 302 as an answer": answer(status=302),
    }
    for what, make in bad_replies.items():
        code, _, _ = run(make)
        check(f"speak: {what} is voice_bad_reply", code == "voice_bad_reply", code)
    good_replies = {
        "a frame sync start": answer(body=frame),
        "exactly the smallest size": answer(body=b"ID3" + bytes(MIN_AUDIO_BYTES - 3)),
        "exactly the biggest size": answer(body=b"ID3" + bytes(MAX_AUDIO_BYTES - 3)),
        "a type line in capitals": answer(ctype="Audio/MPEG"),
    }
    for what, make in good_replies.items():
        code, _, _ = run(make)
        check(f"speak: {what} is good audio", code.startswith("ok:"), code)

    # -- split_text
    check("split_text: a short text is one piece", split_text("  Hello there.  ") == ["Hello there."], "")
    check("split_text: no text is no piece",
          split_text("") == [] and split_text("  \n ") == [] and split_text(None) == [], "")
    story = " ".join(f"Sentence number {i} is here, and it goes on a little." for i in range(120))
    pieces = split_text(story)
    check("split_text: a long text with sentences cuts at sentence ends",
          len(pieces) >= 3 and all(piece.endswith(".") for piece in pieces)
          and all(len(piece) <= MAX_TEXT_CHARS for piece in pieces), str([len(piece) for piece in pieces]))
    check("split_text: the words come back", " ".join(pieces).split() == story.split(), "")
    words = " ".join(["word"] * 1000)
    pieces = split_text(words)
    check("split_text: no sentence end cuts at a space", all(len(piece) <= MAX_TEXT_CHARS and piece == piece.strip()
                                                             for piece in pieces)
          and " ".join(pieces).split() == words.split(), str([len(piece) for piece in pieces]))
    giant = "x" * 5000
    pieces = split_text(giant)
    check("split_text: one very long word is cut hard", [len(piece) for piece in pieces] == [1900, 1900, 1200]
          and "".join(pieces) == giant, str([len(piece) for piece in pieces]))
    check("split_text: a sentence end before a later space",
          split_text("Aaaa bbbb cccc. Dddd eeee ffff gggg hhhh iiii jjjj kkkk llll", 50)[0] == "Aaaa bbbb cccc.", "")
    check("split_text: ? ! and a closing quote end a sentence",
          split_text('He asked "Why?" Then he left! Fine.', 20) == ['He asked "Why?"', "Then he left! Fine."],
          str(split_text('He asked "Why?" Then he left! Fine.', 20)))
    check("split_text: a new line is a space", split_text("Line one.\nLine two.", 12) == ["Line one.", "Line two."], "")
    check("split_text: a limit of 0 ends", split_text("ab c", 0) == ["a", "b", "c"], str(split_text("ab c", 0)))
    rng, broken_rules = random.Random(7), []
    for trial in range(300):
        limit = rng.randint(1, 60)
        text = "".join(rng.choice("abcdefgh") * rng.randint(1, 14) + rng.choice(["", "", ".", "!", "?", ",", '."'])
                       + rng.choice([" ", " ", "  ", "\n", " \t "]) for _ in range(rng.randint(0, 40)))
        pieces = split_text(text, limit)
        if not all(0 < len(piece) <= limit and piece == piece.strip() for piece in pieces):
            broken_rules.append(f"trial {trial}: a piece is empty, too long or has a space at an end")
        if "".join("".join(pieces).split()) != "".join(text.split()):
            broken_rules.append(f"trial {trial}: a character was lost or added")
        if max((len(word) for word in text.split()), default=0) <= limit and " ".join(pieces).split() != text.split():
            broken_rules.append(f"trial {trial}: the words did not come back")
    check("split_text: 300 random texts keep every rule", not broken_rules, "; ".join(broken_rules[:3]))

    # -- probe
    shown: list[str] = []

    def probed(make, config: dict = cfg, name: str = "deepgram"):
        opener = _FakeOpener(make)
        try:
            result = probe(config, name, opener)
        except Exception as error:      # noqa: BLE001 - a crash is what these checks look for
            return {"key": (f"CRASH {type(error).__name__}", ""), "voice": (None, "")}, opener
        shown.append(json.dumps(result))
        return result, opener

    result, opener = probed(answer(), default_config())
    check("probe: no key says where to type it", result["key"][0] is False and '"deepgram_api_key"' in result["key"][1]
          and result["voice"][0] is None and not opener.calls, str(result))
    for status in (401, 403):
        result, _ = probed(refuse(status, {"err_msg": f"Refused {key}."}))
        check(f"probe: HTTP {status} is a refused key", result["key"][0] is False and result["voice"][0] is None,
              str(result))
    result, _ = probed(fails(urllib.error.URLError(OSError("down"))))
    check("probe: no connection says so", result["key"][0] is False and "Cannot reach Deepgram" in result["key"][1],
          str(result))
    result, _ = probed(refuse(402, {"err_msg": "Payment required."}))
    check("probe: no credit is a good key and a voice that cannot speak",
          result["key"][0] is True and result["voice"][0] is False and "no credit" in result["voice"][1], str(result))
    result, _ = probed(refuse(400, {"err_msg": "No such model/language/tier combination found"}),
                       {**cfg, "deepgram_voice": "aura-2-nobody-en"})
    named = [voice for voice in KNOWN_VOICES if voice in result["voice"][1]]
    check("probe: an unknown voice says how to fix it", result["key"][0] is True and result["voice"][0] is False
          and 'Fix "deepgram_voice" in config.json' in result["voice"][1] and len(named) == 3
          and "aura-2-nobody-en" in result["voice"][1], str(result))
    result, opener = probed(answer())
    check("probe: success names the voice id and the audio bytes",
          result["key"][0] is True and result["voice"][0] is True and "aura-2-draco-en" in result["voice"][1]
          and str(len(mp3)) in result["voice"][1], str(result))
    check("probe: one request on the same path as speak", len(opener.calls) == 1 and opener.calls[0]["url"] == url
          and json.loads(opener.calls[0]["body"]) == {"text": PROBE_TEXT}, str(opener.calls[:1]))
    result, _ = probed(refuse(500, {"err_msg": "Oops."}))
    check("probe: a server error proves nothing about the key",
          result["key"][0] is None and result["voice"][0] is False, str(result))
    result, opener = probed(answer(), {**cfg, "deepgram_voice": "aura 2"})
    check("probe: a bad voice id sends no request", not opener.calls and result["key"][0] is None
          and result["voice"][0] is False and 'Fix "deepgram_voice" in config.json' in result["voice"][1], str(result))
    result, opener = probed(answer(), pasted)
    check("probe: a key in the voice line sends no request", not opener.calls and result["voice"][0] is False,
          str(result))
    result, opener = probed(answer(), name="browser")
    check("probe: the browser voice is not checked on the server", not opener.calls
          and result["key"][0] is None and result["voice"][0] is None, str(result))
    check("probe: no answer ever shows the key", shown and not any(key in text for text in shown), "")
    return results


def main() -> int:
    results = offline_checks()
    for name, passed, detail in results:
        print(("ok    " if passed else "FAIL  ") + name + ("" if passed else f"  -> {detail}"))
    failed = sum(1 for _, passed, _ in results if not passed)
    print(f"VOICES OFFLINE: {len(results) - failed} pass, {failed} fail")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
