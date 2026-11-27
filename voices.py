"""Jarviz voices: one way to turn text into speech with the voice named in config.json.

    "voice": "browser" | "deepgram" | "elevenlabs"

The browser voice is the page's own speech: the server does nothing for it. Deepgram (its Aura text-to-speech) and
ElevenLabs are server voices: the page sends a text and names the voice, the server asks that provider with the key
from config.json, and the page plays the mp3 that comes back. The key never leaves the server.

A new server voice is one entry in VOICES, its key line and voice line(s) in config.json, and its own call (_CALLS).

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
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

# The same word as brains.KEY_PLACEHOLDER: config.json has one placeholder for every key. It is written here, not
# imported, so that this file stands on its own.
KEY_PLACEHOLDER = "PUT-YOUR-KEY-HERE"
KEY_REMOVED = "[key removed]"
# A key goes into a header (Deepgram "Authorization: Token", ElevenLabs "xi-api-key"). Only printable ASCII with no
# space: a line break, a tab, a space or another letter would break the header, and http.client's error text for
# that holds the header value, the key.
KEY_TEXT_RE = re.compile(r"[\x21-\x7e]+")
# A line name that may be shown in a text (a key line of config.json).
LINE_NAME_RE = re.compile(r"[a-z_]{1,40}")
USER_AGENT = "Jarviz/1.0"
SPEAK_TIMEOUT_S = 30
# One request of ANY server voice takes at most this many characters. Deepgram takes at most 2000 (more is HTTP 413).
# Every ElevenLabs model takes more (eleven_v3 5000, eleven_multilingual_v2 10,000, eleven_flash_v2_5 40,000). The
# room below 2000 is on purpose: a provider may count characters in another way than Python does. The caller cuts a
# longer text with split_text().
MAX_TEXT_CHARS = 1900
MAX_AUDIO_BYTES = 8 * 1024 * 1024       # far more than the mp3 of 1900 characters: a bigger answer is not speech
MIN_AUDIO_BYTES = 200                   # a smaller answer holds no real speech
MAX_ERROR_BYTES = 64 * 1024             # an error body is read only this far
DETAIL_CHARS = 300
AUDIO_MIME = "audio/mpeg"
PROBE_TEXT = "OK."
# A voice id goes into the address (Deepgram: the query, ElevenLabs: the PATH). Only this plain shape goes there: no
# space, "&", "?", "/" or "%" can change the request.
VOICE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,59}")
# An ElevenLabs model id ("eleven_flash_v2_5"). It goes into the JSON body, and still only this plain shape is sent.
ENGINE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,59}")
# ElevenLabs is ALWAYS asked for this mp3 (a higher bit rate needs a paid plan).
ELEVENLABS_FORMAT = "mp3_44100_128"
# A sentence end: ". ! ?" (and a closing quote or bracket) with a space after it.
SENTENCE_END_RE = re.compile(r"[.!?]+[\"'\u201d\u2019)\]]*(?=\s)")

# kind "browser": the page speaks, the server does nothing.  kind "deepgram": POST {base_url}/speak.
# kind "elevenlabs": POST {base_url}/text-to-speech/{voice id}.
# For every server voice, "model_key" is the config line of its VOICE id and "default_model" the id for an empty line
# (Deepgram's aura id is its voice). "known" holds voice ids that exist: the probe names some when the provider does
# not know the one in config.json. ElevenLabs also has a speech model: its line is "engine_key", with
# "default_engine" and "known_engines" (ElevenLabs' documents, the "models" page).
VOICES = {
    "browser": {"label": "browser", "kind": "browser"},
    "deepgram": {"label": "Deepgram", "kind": "deepgram", "base_url": "https://api.deepgram.com/v1",
                 "key": "deepgram_api_key", "model_key": "deepgram_voice", "default_model": "aura-2-draco-en",
                 "known": ("aura-2-draco-en", "aura-2-pandora-en", "aura-2-odysseus-en", "aura-2-thalia-en",
                           "aura-helios-en")},
    "elevenlabs": {"label": "ElevenLabs", "kind": "elevenlabs", "base_url": "https://api.elevenlabs.io/v1",
                   "key": "elevenlabs_api_key", "model_key": "elevenlabs_voice",
                   # Eldrin, an Infinity voice, not George: ElevenLabs' Default voices expire on 2026-12-31 (and only
                   # accounts made before March 2026 have them). Its replacement table maps George to Eldrin, and an
                   # Infinity voice stays in the Voice Library with no end date.
                   "default_model": "6WwXjDDEMyNmFG95zycZ",
                   "known": ("6WwXjDDEMyNmFG95zycZ", "fnYMz3F5gMEDGMWcH1ex", "gOupLcAkjEnguROwi4oS",
                             "jSuBIjxMKhqIfb0wCK1F"),
                   "known_names": {"6WwXjDDEMyNmFG95zycZ": "Eldrin", "fnYMz3F5gMEDGMWcH1ex": "Finley",
                                   "gOupLcAkjEnguROwi4oS": "Darian", "jSuBIjxMKhqIfb0wCK1F": "Baxter"},
                   "find_ids": 'Or copy a voice id from "My Voices" in ElevenLabs.',
                   "engine_key": "elevenlabs_model", "default_engine": "eleven_flash_v2_5",
                   "known_engines": ("eleven_flash_v2_5", "eleven_multilingual_v2", "eleven_v3", "eleven_v4")},
}
DEFAULT_VOICE = "browser"
# The voices that the server speaks with (/voice/speak). The browser voice is the page's own.
SERVER_VOICES = tuple(name for name, spec in VOICES.items() if spec["kind"] != "browser")
# What a person may call a voice, with everything but letters removed ("11" is read as "eleven" first).
ALIASES = {"deepgram": "deepgram", "deepgramaura": "deepgram", "aura": "deepgram",
           "elevenlabs": "elevenlabs", "eleven": "elevenlabs",
           "browser": "browser", "browsers": "browser", "builtin": "browser", "normal": "browser",
           "default": "browser", "system": "browser", "chrome": "browser"}


class Opener(Protocol):
    """What speak() needs: urllib's own opener, or a fake one in the offline checks."""

    def open(self, request: urllib.request.Request, timeout: float = ...) -> Any: ...


class VoiceError(Exception):
    """The voice could not speak. `code` says why. `detail` is the provider's own short message, or ours. `name` is
    the voice ("elevenlabs"), `label` its name for people, `model` its voice id and `engine` its speech model id
    (ElevenLabs only, else "").

    `keys` are every key of config.json (configured_keys), or one key as a text. Each of them is taken out of the
    detail, the voice id and the model id BEFORE they are cut to one line: a cut first can leave a piece of a key at
    its end (a review finding in brains.py). The exception text is the code only."""

    def __init__(self, code: str, detail: str = "", voice: dict | None = None, keys: str | Iterable[str] = ()):
        super().__init__(code)
        voice = voice or {}
        keys = (keys,) if isinstance(keys, str) else tuple(keys)
        self.code = code
        self.detail = _one_line(scrub_keys(str(detail), keys))[:DETAIL_CHARS]
        self.name = str(voice.get("name", ""))
        self.label = voice.get("label", "the")
        self.model = _one_line(scrub_keys(str(voice.get("model", "")), keys))[:80]
        self.engine = _one_line(scrub_keys(str(voice.get("engine", "")), keys))[:80]


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


def scrub_keys(text: str, keys: Iterable[str]) -> str:
    """scrub_key for each of `keys`, the longest first: a key that holds a shorter key is removed whole, so no piece
    of it is left. Call it on the whole text, BEFORE any cut."""
    text = str(text)
    for key in sorted({str(key or "").strip() for key in keys}, key=len, reverse=True):
        text = scrub_key(text, key)
    return text


def key_lines(config: dict) -> dict[str, str]:
    """Each key line of config.json with its key: each line whose name ends in "_api_key" (the brains and the voices),
    and the key line of every server voice, when it is a text that is not empty and not the placeholder. Stripped."""
    names = {name for name in config if isinstance(name, str) and name.endswith("_api_key")}
    names |= {spec["key"] for spec in VOICES.values() if "key" in spec}
    lines = {}
    for name in sorted(names):
        value = config.get(name)
        if isinstance(value, str) and value.strip() and value.strip() != KEY_PLACEHOLDER:
            lines[name] = value.strip()
    return lines


def configured_keys(config: dict) -> list[str]:
    """Every key in config.json, of a brain or of a voice (key_lines), each once, the longest first. A key of
    ANOTHER line pasted into a voice line must not travel either: not into an address, a body, the page or a line."""
    return sorted(set(key_lines(config).values()), key=len, reverse=True)


# ------------------------------------------------------------------- choosing

def default_config() -> dict:
    """The voice lines of config.json: which voice is active, and a key and a voice id for each server voice
    (ElevenLabs also a model id)."""
    config: dict = {"voice": DEFAULT_VOICE}
    for spec in VOICES.values():
        if "key" in spec:
            config[spec["key"]] = KEY_PLACEHOLDER
            config[spec["model_key"]] = spec["default_model"]
        if "engine_key" in spec:
            config[spec["engine_key"]] = spec["default_engine"]
    return config


def resolve_name(spoken: str) -> str | None:
    """ "Deep Gram", "the browser voice", "built in", "11 Labs" -> the name used in config.json, or None."""
    text = re.sub(r"(?<!\d)11(?!\d)", "eleven", str(spoken).lower())     # "11 Labs" is said "eleven labs"
    squashed = re.sub(r"[^a-z]", "", text)
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


def _engine_id(config: dict, spec: dict) -> str:
    """The speech model id of a voice that has one (ElevenLabs), from its config line, else its default."""
    return str(config.get(spec["engine_key"]) or "").strip() or spec["default_engine"]


def _voice_of(config: dict, name: str) -> dict:
    """What a request and an error need to know about a server voice."""
    spec = VOICES[name]
    voice = {"name": name, "label": spec["label"], "model": _voice_id(config, spec)}
    if "engine_key" in spec:
        voice["engine"] = _engine_id(config, spec)
    return voice


def describe(config: dict, name: str | None = None) -> dict:
    """What the browser may know about a voice. Never a key: a key of ANY line pasted into the voice line shows as
    "[key removed]". The browser voice needs no key, so its "has_key" is True."""
    name = name or active_name(config)
    spec = VOICES[name]
    if spec["kind"] == "browser":
        return {"name": name, "label": spec["label"], "model": "", "has_key": True}
    return {"name": name, "label": spec["label"], "model": scrub_keys(_voice_id(config, spec), configured_keys(config)),
            "has_key": bool(_key_of(config, spec))}


def describe_all(config: dict) -> list[dict]:
    return [describe(config, name) for name in VOICES]


# ------------------------------------------------------------------- speaking

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None                      # a key must never follow a redirect to another address


_OPENER = urllib.request.build_opener(_NoRedirect)


def speak(config: dict, text: str, name: str, opener: Opener | None = None) -> tuple[bytes, str]:
    """The mp3 audio of `text` in the server voice `name` ("deepgram", "elevenlabs") with its lines of config.json,
    and its MIME type. Raises VoiceError. One request: the caller cuts a text longer than MAX_TEXT_CHARS with
    split_text(). `opener` serves the offline checks only."""
    spec = VOICES.get(name) if isinstance(name, str) else None
    if spec is None or spec["kind"] not in _CALLS:
        why = "The browser voice is spoken by the page itself." if spec else "Jarviz has no server voice of that name."
        raise VoiceError("voice_not_served", why, spec)
    voice, key, keys = _voice_of(config, name), _key_of(config, spec), configured_keys(config)
    if not key:
        raise VoiceError("voice_key_missing", _no_key(spec), voice, keys)
    trouble = _key_trouble(config, spec, key)
    if trouble:
        raise VoiceError(trouble[0], trouble[1], voice, keys)             # no request is sent
    if not isinstance(text, str) or not text.strip():
        raise VoiceError("voice_empty", "There is no text to speak.", voice, keys)
    text = text.strip()
    if len(text) > MAX_TEXT_CHARS:
        raise VoiceError("voice_too_long", f"The text has {len(text)} characters. One request takes at most "
                         f"{MAX_TEXT_CHARS}: cut it with split_text().", voice, keys)
    trouble = _voice_id_trouble(voice["model"], keys, spec)
    if trouble:
        raise VoiceError("voice_not_found", trouble, voice, keys)         # no request is sent
    trouble = _engine_trouble(voice.get("engine", ""), keys, spec)
    if trouble:
        raise VoiceError("voice_model_not_found", trouble, voice, keys)   # no request is sent
    return _CALLS[spec["kind"]](key, voice, spec, text, opener or _OPENER, keys)


def _no_key(spec: dict) -> str:
    return f'config.json has no {spec["label"]} key yet. Type it into "{spec["key"]}".'


def _key_trouble(config: dict, spec: dict, key: str) -> tuple[str, str] | None:
    """(code, why) when the key of this voice may not be sent, else None. It never shows the key or another key:
    only line names. A key that is no header-safe text (KEY_TEXT_RE) would break the request. A key that equals,
    holds or is held by the key of ANOTHER line was copied: it belongs to another provider, and it is never sent."""
    line = spec["key"]
    if not KEY_TEXT_RE.fullmatch(key):
        return "voice_key_bad_text", (f'The "{line}" line of config.json holds a character that cannot be sent: a '
                                      "space, a line break, a tab or a letter outside plain ASCII. Type the key again.")
    for other, value in key_lines(config).items():
        if other != line and (value in key or key in value):
            named = f'"{other}"' if LINE_NAME_RE.fullmatch(other) else "another key line"
            return "voice_key_copied", (f'The "{line}" line of config.json holds the same key as {named} (or a part '
                                        "of it). Jarviz sends no key to the wrong provider.")
    return None


def _holds_a_key(line: str) -> str:
    """The trouble text for a line that holds a key. It never says which key, and never shows it."""
    return f'The "{line}" line of config.json holds a key. A key belongs only on its own "..._api_key" line.'


def _voice_id_trouble(model: str, keys: Iterable[str], spec: dict) -> str:
    """Why this voice id may not go into the address, or "". A key of ANY line (configured_keys) pasted into the
    voice line would travel in the address, and addresses end up in logs."""
    line = spec["model_key"]
    if any(key in model for key in keys):
        return _holds_a_key(line)
    if not VOICE_ID_RE.fullmatch(model):
        return f'The "{line}" line of config.json is not a voice id (letters, digits, ".", "_" and "-", at most 60).'
    return ""


def _engine_trouble(engine: str, keys: Iterable[str], spec: dict) -> str:
    """Why this speech model id may not be sent, or "". A key of ANY line in it is refused like a bad id. A voice
    with no model line (Deepgram) has no trouble here."""
    if "engine_key" not in spec:
        return ""
    line = spec["engine_key"]
    if any(key in engine for key in keys):
        return _holds_a_key(line)
    if not ENGINE_ID_RE.fullmatch(engine):
        return (f'The "{line}" line of config.json is not a model id (small letters, digits, ".", "_" and "-", '
                'at most 60).')
    return ""


def _deepgram_once(key: str, voice: dict, spec: dict, text: str, opener: Opener,
                   keys: Iterable[str]) -> tuple[bytes, str]:
    # ALWAYS ask for mp3: Deepgram's documents disagree about the default encoding.
    def build() -> urllib.request.Request:
        url = spec["base_url"] + "/speak?" + urllib.parse.urlencode({"model": voice["model"], "encoding": "mp3"})
        return urllib.request.Request(
            url, method="POST", data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Authorization": f"Token {key}", "Content-Type": "application/json",
                     "Accept": AUDIO_MIME, "User-Agent": USER_AGENT})
    return _fetch_audio(build, voice, keys, opener, _deepgram_error)


def _elevenlabs_once(key: str, voice: dict, spec: dict, text: str, opener: Opener,
                     keys: Iterable[str]) -> tuple[bytes, str]:
    # The voice id is in the address PATH: VOICE_ID_RE let no "/", "?", "%" or space through, and quote() is a second
    # net. ALWAYS ask for ELEVENLABS_FORMAT, and ALWAYS name the model: the API would pick one of its own. The key
    # goes in the xi-api-key header only.
    def build() -> urllib.request.Request:
        url = (f'{spec["base_url"]}/text-to-speech/{urllib.parse.quote(voice["model"], safe="")}?'
               + urllib.parse.urlencode({"output_format": ELEVENLABS_FORMAT}))
        return urllib.request.Request(
            url, method="POST", data=json.dumps({"text": text, "model_id": voice["engine"]}).encode("utf-8"),
            headers={"xi-api-key": key, "Content-Type": "application/json",
                     "Accept": AUDIO_MIME, "User-Agent": USER_AGENT})
    return _fetch_audio(build, voice, keys, opener, _elevenlabs_error)


# The call of each kind of server voice. speak() and probe() serve only these kinds. `key` is the key of the voice
# (its header), `keys` every key of config.json: each is taken out of what an error says.
_CALLS = {"deepgram": _deepgram_once, "elevenlabs": _elevenlabs_once}


def _fetch_audio(build: Callable[[], urllib.request.Request], voice: dict, keys: Iterable[str], opener: Opener,
                 errors: Callable[[int, dict, dict, Iterable[str]], VoiceError]) -> tuple[bytes, str]:
    """One request of a server voice (`build` makes it), and its answer checked: mp3 bytes, or a VoiceError. `errors`
    turns the provider's HTTP error into a VoiceError (_deepgram_error, _elevenlabs_error). No other exception
    leaves: the text of a ValueError or TypeError from building or sending can hold a header value, the key, so
    only its name travels on (_why)."""
    try:
        with opener.open(build(), timeout=SPEAK_TIMEOUT_S) as response:
            status = getattr(response, "status", None)
            headers = getattr(response, "headers", None)
            ctype = headers.get("Content-Type", "") if headers is not None else ""
            data = _read_capped(response, MAX_AUDIO_BYTES)
    except urllib.error.HTTPError as error:              # a redirect lands here too: _NoRedirect did not follow it
        raise errors(error.code, _json_of(_error_body(error)), voice, keys) from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
        # HTTPException: the answer broke off in the middle (IncompleteRead).
        raise VoiceError("voice_unreachable", _why(error), voice, keys) from None
    except (ValueError, TypeError) as error:            # never its text: it can hold the key
        raise VoiceError("voice_not_sent", f"The request could not be made ({_why(error)}).", voice, keys) from None
    trouble = _audio_trouble(status, ctype if isinstance(ctype, str) else "", data)
    if trouble:
        raise VoiceError("voice_bad_reply", trouble, voice, keys)
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


def _redirect_error(status: int, voice: dict, keys: Iterable[str]) -> VoiceError:
    return VoiceError("voice_bad_reply", f"{voice['label']} answered with a redirect (HTTP {status}). Jarviz never "
                      "follows one: the key would go with it.", voice, keys)


def _deepgram_error(status: int, payload: dict, voice: dict, keys: Iterable[str]) -> VoiceError:
    detail, words = _error_text(payload)
    text = f"{detail} {words}".lower()
    if 300 <= status < 400:
        return _redirect_error(status, voice, keys)
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
    return VoiceError(code, detail or f"HTTP {status}", voice, keys)


# ElevenLabs' code words (its documents, and the older words of its help pages) -> the VoiceError code. A word decides
# before the HTTP status: an old answer says quota_exceeded with HTTP 401, and that is no bad key.
ELEVENLABS_CODES = {
    "invalid_api_key": "voice_key_invalid", "missing_api_key": "voice_key_invalid",
    "invalid_authorization_header": "voice_key_invalid", "unauthorized": "voice_key_invalid",
    "authentication_error": "voice_key_invalid",
    "quota_exceeded": "voice_no_credit", "insufficient_credits": "voice_no_credit", "payment_required": "voice_no_credit",
    # a key that may not make speech (a restricted key without that permission)
    "insufficient_permissions": "voice_no_access", "forbidden": "voice_no_access",
    # the plan or the owner of the voice does not allow it: no bad key, and no lack of credit
    "voice_access_denied": "voice_not_allowed", "model_access_denied": "voice_not_allowed",
    "subscription_required": "voice_not_allowed", "feature_not_available": "voice_not_allowed",
    "voice_not_found": "voice_not_found", "invalid_voice_id": "voice_not_found",
    "model_not_found": "voice_model_not_found", "unsupported_model": "voice_model_not_found",
    "text_too_long": "voice_too_long", "max_character_limit_exceeded": "voice_too_long",
    "request_too_large": "voice_too_long",
}
# Without a code word that is known, the HTTP status decides.
ELEVENLABS_STATUSES = {401: "voice_key_invalid", 402: "voice_no_credit", 403: "voice_no_access",
                       404: "voice_not_found", 413: "voice_too_long"}


def _elevenlabs_error_text(payload: dict) -> tuple[str, list[str]]:
    """(ElevenLabs' own message, its code words) from an error body {"detail": ...}. The newer detail has "code" and
    "type", the older one "status" for the code word; both have "message". A validation error (HTTP 422) has a LIST
    of {"loc", "msg", "type"}. Only text counts: the body is data from a stranger."""
    detail = payload.get("detail")
    words: list = []
    if isinstance(detail, list):
        message = next((item for item in detail if isinstance(item, dict)), {}).get("msg")
    elif isinstance(detail, dict):
        code = detail.get("code")
        if not isinstance(code, str) or not code.strip():
            code = detail.get("status")             # the older shape: "status" holds the code word
        message, words = detail.get("message"), [code, detail.get("type")]
    else:
        message = detail                            # {"detail": "Not Found"}: a plain text
    message = message if isinstance(message, str) and message.strip() else ""
    return message, [word.strip().lower() for word in words if isinstance(word, str) and word.strip()]


def _elevenlabs_error(status: int, payload: dict, voice: dict, keys: Iterable[str]) -> VoiceError:
    detail, words = _elevenlabs_error_text(payload)
    if 300 <= status < 400:
        return _redirect_error(status, voice, keys)
    if status == 429:                               # rate_limit_exceeded, concurrent_limit_exceeded, system_busy
        code = "voice_busy"
    elif status == 408 or status >= 500:
        code = "voice_error"
    else:
        code = (next((ELEVENLABS_CODES[word] for word in words if word in ELEVENLABS_CODES), None)
                or ELEVENLABS_STATUSES.get(status)
                or ("voice_bad_request" if 400 <= status < 500 else "voice_bad_reply"))
    return VoiceError(code, detail or f"HTTP {status}", voice, keys)


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

def probe(config: dict, name: str, opener: Opener | None = None) -> dict:
    """One real, very small request of the server voice `name` on the SAME path as speak(): the text "OK.". A voice
    list does not prove that the key may speak with the voice. Returns {"key": (ok, detail), "voice": (ok, detail)}.
    ok is True, False, or None for "not checked". None is never a pass."""
    spec = VOICES.get(name) if isinstance(name, str) else None
    if spec is None or spec["kind"] not in _CALLS:
        why = ("The page speaks with the browser voice. The server has nothing to check." if spec
               else "Jarviz has no server voice of that name.")
        return {"key": (None, why), "voice": (None, why)}
    key, voice, keys = _key_of(config, spec), _voice_of(config, name), configured_keys(config)
    if not key:
        return {"key": (False, _no_key(spec)), "voice": (None, "")}
    trouble = _key_trouble(config, spec, key)
    if trouble:                                 # no request is sent
        return {"key": (False, f"{trouble[1]} ({trouble[0]})"), "voice": (None, "")}
    model, engine = scrub_keys(voice["model"], keys), scrub_keys(voice.get("engine", ""), keys)   # what may be shown
    trouble = _voice_id_trouble(voice["model"], keys, spec)
    if trouble:
        return {"key": (None, "Not checked: Jarviz sends no request with this voice line."),
                "voice": (False, _voice_fix(spec, model, f"The voice id {model!r} cannot be used. {trouble}"))}
    trouble = _engine_trouble(voice.get("engine", ""), keys, spec)
    if trouble:
        return {"key": (None, "Not checked: Jarviz sends no request with this model line."),
                "voice": (False, _engine_fix(spec, engine, f"The model id {engine!r} cannot be used. {trouble}"))}
    try:
        data, _ = speak(config, PROBE_TEXT, name, opener)
    except VoiceError as error:                 # its detail, voice id and model id lost every key already
        return _probe_trouble(spec, error)
    spoken = model + (f" with {engine}" if engine else "")
    return {"key": (True, "the key is good"),
            "voice": (True, f"{spoken}: {len(data)} bytes of audio for {PROBE_TEXT!r}")}


def _probe_trouble(spec: dict, error: VoiceError) -> dict:
    label, code, detail = spec["label"], error.code, error.detail
    good = (True, "the key is good")
    if code in ("voice_key_invalid", "voice_no_access"):
        return {"key": (False, f"{label} refused the key ({code}). {detail}".strip()), "voice": (None, "")}
    if code == "voice_unreachable":
        return {"key": (False, f"Cannot reach {label}. Check the internet connection. ({detail})"), "voice": (None, "")}
    if code == "voice_no_credit":
        return {"key": good, "voice": (False, f"The {label} account has no credit. {detail}".strip())}
    if code == "voice_not_allowed":
        what = f"the voice {error.model!r}" + (f" with the model {error.engine!r}" if error.engine else "")
        lines = f'"{spec["model_key"]}"' + (f' or "{spec["engine_key"]}"' if "engine_key" in spec else "")
        told = f"The {label} plan does not allow {what} ({code}). {detail}".strip()
        return {"key": good, "voice": (False, f"{told} Choose another one: fix {lines} in config.json.")}
    if code == "voice_not_found":
        return {"key": good, "voice": (False, _voice_fix(spec, error.model,
                                                         f"{label} does not know the voice id {error.model!r}. "
                                                         f"{detail}".strip()))}
    if code == "voice_model_not_found" and "engine_key" in spec:
        return {"key": good, "voice": (False, _engine_fix(spec, error.engine,
                                                          f"{label} does not know the model id {error.engine!r}. "
                                                          f"{detail}".strip()))}
    return {"key": (None, f"Not proven: {label} did not say whether the key is good ({code})."),
            "voice": (False, f"{label} could not speak the test text ({code}). {detail}".strip())}


def _voice_fix(spec: dict, model: str, text: str) -> str:
    names = spec.get("known_names", {})
    others = [f"{voice} ({names[voice]})" if voice in names else voice for voice in spec["known"] if voice != model][:3]
    find = f" {spec['find_ids']}" if spec.get("find_ids") else ""
    return f"{text} Some ids that exist: {', '.join(others)}.{find} Fix \"{spec['model_key']}\" in config.json."


def _engine_fix(spec: dict, engine: str, text: str) -> str:
    others = [known for known in spec["known_engines"] if known != engine][:3]
    return f"{text} Some model ids that exist: {', '.join(others)}. Fix \"{spec['engine_key']}\" in config.json."


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
    # A fake ElevenLabs key. It is made of small letters and "-" on purpose: it fits VOICE_ID_RE and ENGINE_ID_RE, so
    # only the "holds the key" rule can keep it out of the address and the body.
    el_key = "fake-elevenlabs-key-for-the-offline-only"
    el_cfg = {**default_config(), "voice": "elevenlabs", "elevenlabs_api_key": el_key}
    mp3 = b"ID3" + bytes(300)
    frame = bytes([0xFF, 0xFB]) + bytes(300)
    url = "https://api.deepgram.com/v1/speak?model=aura-2-draco-en&encoding=mp3"
    el_url = "https://api.elevenlabs.io/v1/text-to-speech/6WwXjDDEMyNmFG95zycZ?output_format=mp3_44100_128"

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

    def el_run(make, config: dict = el_cfg, text=" Good evening. ", opener=None):
        return run(make, config, text, "elevenlabs", opener)

    def el_detail(payload: dict) -> dict:
        return {"detail": payload}

    def json_body(call: dict):
        try:
            return json.loads(call.get("body") or b"")
        except ValueError:
            return None

    # -- choosing
    check("default_config: the voice lines", default_config()
          == {"voice": "browser", "deepgram_api_key": KEY_PLACEHOLDER, "deepgram_voice": "aura-2-draco-en",
              "elevenlabs_api_key": KEY_PLACEHOLDER, "elevenlabs_voice": "6WwXjDDEMyNmFG95zycZ",
              "elevenlabs_model": "eleven_flash_v2_5"}, str(default_config()))
    check("default_config: the lines in their order", list(default_config()) == [
        "voice", "deepgram_api_key", "deepgram_voice", "elevenlabs_api_key", "elevenlabs_voice", "elevenlabs_model"],
          str(list(default_config())))
    check("SERVER_VOICES: Deepgram and ElevenLabs, not the browser", SERVER_VOICES == ("deepgram", "elevenlabs"),
          str(SERVER_VOICES))
    check("every server voice has its call", all(VOICES[name]["kind"] in _CALLS for name in SERVER_VOICES), "")
    try:                                # read, not imported: importing brains.py would start its own machinery
        found = re.search(r'^KEY_PLACEHOLDER = "([^"]*)"', Path(__file__).with_name("brains.py").read_text("utf-8"),
                          re.MULTILINE)
        same, why = (found is not None and found.group(1) == KEY_PLACEHOLDER), "brains.py has another placeholder"
    except (OSError, ValueError) as error:          # a check that cannot look says so
        same, why = False, f"could not look: brains.py could not be read ({type(error).__name__})"
    check("the key placeholder is the same word as in brains.py", same, why)
    servers = [VOICES[name] for name in SERVER_VOICES]
    check("the default voice id of each server voice is a known voice",
          all(spec["default_model"] in spec["known"] for spec in servers), "")
    check("every known voice id has the shape that may go into the address",
          all(VOICE_ID_RE.fullmatch(voice) for spec in servers for voice in spec["known"]), "")
    eleven = VOICES["elevenlabs"]
    check("the default ElevenLabs model is a known model id of the right shape",
          eleven["default_engine"] in eleven["known_engines"]
          and all(ENGINE_ID_RE.fullmatch(engine) for engine in eleven["known_engines"]), "")
    check("every known ElevenLabs voice has a name", set(eleven["known_names"]) == set(eleven["known"]), "")
    spoken = {"deepgram": "deepgram", "Deep Gram": "deepgram", "the Deepgram voice": "deepgram", "aura": "deepgram",
              "Aura voice": "deepgram", "browser": "browser", "the browser voice": "browser", "built in": "browser",
              "built-in voice": "browser", "normal": "browser", "default": "browser", "the system voice": "browser",
              "Chrome": "browser", "the browser's voice": "browser",
              "ElevenLabs": "elevenlabs", "Eleven Labs": "elevenlabs", "eleven labs": "elevenlabs",
              "eleven labs voice": "elevenlabs", "the ElevenLabs voice": "elevenlabs", "11 Labs": "elevenlabs",
              "11labs": "elevenlabs", "the 11 labs voice": "elevenlabs", "Eleven": "elevenlabs",
              "labs": None, "the labs voice": None, "111 labs": None, "eleven brains": None, "kazoo": None,
              "": None, "voice": None, "the": None, "None": None}
    heard = {words: resolve_name(words) for words in spoken}
    check("resolve_name: the spoken names", heard == spoken, str({w: h for w, h in heard.items() if spoken[w] != h}))
    check("resolve_name: something that is not text is no voice",
          resolve_name(None) is None and resolve_name(42) is None, "")
    actives = [active_name(c) for c in ({"voice": "deepgram"}, {"voice": " Deepgram "}, {"voice": "ElevenLabs"},
                                        {"voice": "nonsense"}, {}, {"voice": None}, {"voice": 5})]
    check("active_name: a known voice, else the browser",
          actives == ["deepgram", "deepgram", "elevenlabs"] + ["browser"] * 4, str(actives))
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
    fresh = describe(default_config(), "elevenlabs")
    check("describe: ElevenLabs with no key yet", fresh == {"name": "elevenlabs", "label": "ElevenLabs",
                                                           "model": "6WwXjDDEMyNmFG95zycZ", "has_key": False}, str(fresh))
    check("describe: the ElevenLabs key is there, and never shown", describe(el_cfg, "elevenlabs")["has_key"] is True
          and el_key not in json.dumps(describe_all(el_cfg)) and describe(el_cfg, "deepgram")["has_key"] is False, "")
    for line in ("elevenlabs_voice", "elevenlabs_model"):
        pasted_el = {**el_cfg, line: el_key}
        check(f"describe: the ElevenLabs key pasted into {line} is not shown",
              el_key not in json.dumps(describe_all(pasted_el)), "")

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
    check("speak: a Deepgram request has no xi-api-key header", "xi-api-key" not in headers, str(sorted(headers)))
    body = json_body(call)
    check("speak: the JSON body holds the stripped text", body == {"text": "Good evening."}, str(body))
    check("speak: the Deepgram key is in its header only", key not in str(call.get("url"))
          and key.encode() not in (call.get("body") or b""), "")
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

    # -- ElevenLabs: the request that is built
    code, opener, data = el_run(answer())
    call = opener.calls[0] if opener.calls else {}
    headers = call.get("headers", {})
    check("elevenlabs: a good mp3 answer comes back as bytes", code == f"ok:{AUDIO_MIME}:{len(mp3)}" and data == mp3,
          code)
    check("elevenlabs: one request", len(opener.calls) == 1, str(len(opener.calls)))
    check("elevenlabs: the address names the voice in its path and asks for mp3_44100_128", call.get("url") == el_url,
          str(call.get("url")))
    check("elevenlabs: POST", call.get("method") == "POST", str(call.get("method")))
    check("elevenlabs: the key goes in the xi-api-key header", headers.get("xi-api-key") == el_key, "")
    check("elevenlabs: no Authorization header", "authorization" not in headers, str(sorted(headers)))
    check("elevenlabs: the other headers", headers.get("content-type") == "application/json"
          and headers.get("accept") == "audio/mpeg" and headers.get("user-agent") == USER_AGENT, str(sorted(headers)))
    check("elevenlabs: exactly these headers", set(headers) == {"xi-api-key", "content-type", "accept", "user-agent"},
          str(sorted(headers)))
    body = json_body(call)
    check("elevenlabs: the body is the stripped text and the model id, nothing else",
          body == {"text": "Good evening.", "model_id": "eleven_flash_v2_5"}, str(body))
    check("elevenlabs: the key is in its header only", el_key not in str(call.get("url"))
          and el_key.encode() not in (call.get("body") or b""), "")
    check("elevenlabs: the timeout", call.get("timeout") == SPEAK_TIMEOUT_S, str(call.get("timeout")))
    code, opener, _ = el_run(answer(), {**el_cfg, "elevenlabs_voice": " fnYMz3F5gMEDGMWcH1ex "})
    check("elevenlabs: the voice id from config.json", opener.calls and opener.calls[0]["url"]
          == el_url.replace("6WwXjDDEMyNmFG95zycZ", "fnYMz3F5gMEDGMWcH1ex"), code)
    code, opener, _ = el_run(answer(), {**el_cfg, "elevenlabs_model": " eleven_multilingual_v2 "})
    check("elevenlabs: the model id from config.json", opener.calls
          and json_body(opener.calls[0]) == {"text": "Good evening.", "model_id": "eleven_multilingual_v2"}, code)
    code, opener, _ = el_run(answer(), {**el_cfg, "elevenlabs_voice": "  ", "elevenlabs_model": ""})
    check("elevenlabs: empty lines are the default voice and model", opener.calls and opener.calls[0]["url"] == el_url
          and json_body(opener.calls[0]) == {"text": "Good evening.", "model_id": "eleven_flash_v2_5"}, code)
    code, opener, _ = el_run(answer(), {**el_cfg, "elevenlabs_model": "eleven_v3"})
    check("elevenlabs: another known model is sent", code.startswith("ok:")
          and json_body(opener.calls[0])["model_id"] == "eleven_v3", code)
    code, opener, _ = el_run(answer(), {**el_cfg, "elevenlabs_model": "a" * 60})
    check("elevenlabs: a model id of 60 characters is sent", code.startswith("ok:"), code)
    both = {**cfg, "elevenlabs_api_key": el_key}
    _, dg_opener, _ = run(answer(), both)
    _, el_opener, _ = el_run(answer(), both)
    dg_call = json.dumps({**dg_opener.calls[0], "body": str(dg_opener.calls[0]["body"])}) if dg_opener.calls else ""
    el_call = json.dumps({**el_opener.calls[0], "body": str(el_opener.calls[0]["body"])}) if el_opener.calls else ""
    check("speak: with both keys, each request carries its own key only", dg_call and el_call
          and el_key not in dg_call and key not in el_call and key in dg_call and el_key in el_call, "")

    # -- ElevenLabs: refused before any request
    for bad in ("6WwX jDDE", "-6WwX", "6WwX&output_format=pcm", "6WwX?x=1", "../voices", "6WwX%2Fx", "a" * 61,
                "ä6WwX", "6WwX\nx", "6WwX/stream", el_key):
        code, opener, error = el_run(answer(), {**el_cfg, "elevenlabs_voice": bad})
        shown = f"{getattr(error, 'detail', '')} {getattr(error, 'model', '')}"
        what = "the key itself" if bad == el_key else ascii(bad[:12])
        check(f"elevenlabs: the voice id {what} sends no request", code == "voice_not_found" and not opener.calls
              and el_key not in shown, code)
    for bad in ("Eleven_v3", "eleven v3", "-eleven", "eleven/v3", "eleven?x=1", "eleven%20v3", '"eleven"', "a" * 61,
                "éleven_v3", "eleven\nv3", el_key):
        code, opener, error = el_run(answer(), {**el_cfg, "elevenlabs_model": bad})
        shown = f"{getattr(error, 'detail', '')} {getattr(error, 'model', '')} {getattr(error, 'engine', '')}"
        what = "the key itself" if bad == el_key else ascii(bad[:12])
        check(f"elevenlabs: the model id {what} sends no request", code == "voice_model_not_found"
              and not opener.calls and el_key not in shown, code)
    for missing in (KEY_PLACEHOLDER, "", "   ", None):
        code, opener, _ = el_run(answer(), {**el_cfg, "elevenlabs_api_key": missing})
        check(f"elevenlabs: no key ({missing!r}) is voice_key_missing",
              code == "voice_key_missing" and not opener.calls, code)
    code, opener, _ = el_run(answer(), cfg)
    check("elevenlabs: the Deepgram key is no ElevenLabs key", code == "voice_key_missing" and not opener.calls, code)
    code, opener, _ = run(answer(), el_cfg)
    check("speak: the ElevenLabs key is no Deepgram key", code == "voice_key_missing" and not opener.calls, code)
    for text in ("", "   \n", None, 42):
        code, opener, _ = el_run(answer(), text=text)
        check(f"elevenlabs: the text {text!r} is voice_empty", code == "voice_empty" and not opener.calls, code)
    code, opener, _ = el_run(answer(), text="a" * (MAX_TEXT_CHARS + 1))
    check("elevenlabs: a text over the limit is voice_too_long", code == "voice_too_long" and not opener.calls, code)

    # -- ElevenLabs: its errors, in the newer shape (code, type), the older one (status) and the 422 list
    def new(kind: str, word: str, message: str = "It went wrong.") -> dict:
        return el_detail({"type": kind, "code": word, "message": message, "status": "legacy", "request_id": "r-1",
                          "param": None})

    def old(word: str, message: str = "It went wrong.") -> dict:
        return el_detail({"status": word, "message": message})

    el_statuses = [
        (401, new("authentication_error", "invalid_api_key"), "voice_key_invalid"),
        (401, new("authentication_error", "missing_api_key"), "voice_key_invalid"),
        (401, new("authentication_error", "invalid_authorization_header"), "voice_key_invalid"),
        (401, new("authentication_error", "unauthorized"), "voice_key_invalid"),
        (401, new("authentication_error", "something_new"), "voice_key_invalid"),
        (402, new("payment_required", "insufficient_credits"), "voice_no_credit"),
        (402, new("payment_required", "something_new"), "voice_no_credit"),
        (403, new("authorization_error", "insufficient_permissions"), "voice_no_access"),
        (403, new("authorization_error", "forbidden"), "voice_no_access"),
        (403, new("authorization_error", "voice_access_denied"), "voice_not_allowed"),
        (403, new("authorization_error", "model_access_denied"), "voice_not_allowed"),
        (403, new("authorization_error", "subscription_required"), "voice_not_allowed"),
        (403, new("authorization_error", "feature_not_available"), "voice_not_allowed"),
        (403, new("authorization_error", "something_new"), "voice_no_access"),
        (404, new("not_found", "voice_not_found"), "voice_not_found"),
        (400, new("validation_error", "invalid_voice_id"), "voice_not_found"),
        (404, new("not_found", "model_not_found"), "voice_model_not_found"),
        (400, new("validation_error", "unsupported_model"), "voice_model_not_found"),
        (400, new("validation_error", "text_too_long"), "voice_too_long"),
        (400, new("invalid_request", "request_too_large"), "voice_too_long"),
        (400, new("validation_error", "empty_text"), "voice_bad_request"),
        (400, new("validation_error", "invalid_output_format"), "voice_bad_request"),
        (400, new("invalid_request", "malformed_json"), "voice_bad_request"),
        (400, new("VALIDATION_ERROR", "INVALID_VOICE_ID"), "voice_not_found"),
        (429, new("rate_limit_error", "rate_limit_exceeded"), "voice_busy"),
        (429, new("rate_limit_error", "concurrent_limit_exceeded"), "voice_busy"),
        (429, new("rate_limit_error", "system_busy"), "voice_busy"),
        (429, new("authentication_error", "invalid_api_key"), "voice_busy"),
        (500, new("internal_error", "internal_error"), "voice_error"),
        (503, new("service_unavailable", "maintenance"), "voice_error"),
        (503, new("authentication_error", "invalid_api_key"), "voice_error"),
        (408, {}, "voice_error"),
        (401, old("quota_exceeded", "You have insufficient quota to complete the request."), "voice_no_credit"),
        (401, old("invalid_api_key", "Invalid API key."), "voice_key_invalid"),
        (400, old("max_character_limit_exceeded"), "voice_too_long"),
        (404, old("voice_not_found"), "voice_not_found"),
        (401, el_detail({"code": "voice_not_found", "status": "invalid_api_key"}), "voice_not_found"),
        (401, el_detail({"code": None, "status": "quota_exceeded"}), "voice_no_credit"),
        (401, {}, "voice_key_invalid"), (402, {}, "voice_no_credit"), (403, {}, "voice_no_access"),
        (404, {}, "voice_not_found"), (413, {}, "voice_too_long"), (409, {}, "voice_bad_request"),
        (422, el_detail([{"loc": ["body", "text"], "msg": "Field required", "type": "missing"}]), "voice_bad_request"),
        (404, el_detail("Not Found"), "voice_not_found"),
    ]
    for status, payload, want in el_statuses:
        detail = payload.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("code"), str):
            said = f"code {detail['code']!r}"
        elif isinstance(detail, dict):
            said = (f"code {detail['code']!r} and " if "code" in detail else "") + f"status {detail.get('status')!r}"
        else:
            said = "a detail " + type(detail).__name__
        code, _, _ = el_run(refuse(status, payload))
        check(f"elevenlabs: HTTP {status} {said} is {want}", code == want, code)
    _, _, error = el_run(refuse(401, new("authentication_error", "invalid_api_key", "Invalid API\nkey.  Check it.")))
    check("elevenlabs: the detail is the newer message on one line",
          getattr(error, "detail", "") == "Invalid API key. Check it.", getattr(error, "detail", ""))
    _, _, error = el_run(refuse(401, old("quota_exceeded", "You have insufficient quota to complete the request.")))
    check("elevenlabs: the older shape gives its message",
          getattr(error, "detail", "") == "You have insufficient quota to complete the request.", "")
    _, _, error = el_run(refuse(422, el_detail([{"loc": ["body"], "msg": 5}, {"loc": ["body"], "msg": "Bad text."}])))
    check("elevenlabs: a 422 list gives the msg of its first item, only when it is text",
          getattr(error, "detail", "") == "HTTP 422", getattr(error, "detail", ""))
    _, _, error = el_run(refuse(422, el_detail(["x", 5, {"loc": ["body"], "msg": "Field required"}])))
    check("elevenlabs: a 422 list skips what is not an item", getattr(error, "detail", "") == "Field required",
          getattr(error, "detail", ""))
    _, _, error = el_run(refuse(400, el_detail({"code": 5, "status": ["invalid_api_key"], "type": {"x": 1},
                                                "message": {"nested": True}})))
    check("elevenlabs: fields that are not text are not used", getattr(error, "code", "") == "voice_bad_request"
          and error.detail == "HTTP 400", f"{getattr(error, 'code', '')} {getattr(error, 'detail', '')}")
    _, _, error = el_run(refuse(401, {"detail": None, "message": "Not the shape."}))
    check("elevenlabs: a body without its detail says the status", getattr(error, "code", "") == "voice_key_invalid"
          and error.detail == "HTTP 401", getattr(error, "detail", ""))
    _, _, error = el_run(refuse(502, raw=b"<html>Bad gateway</html>"))
    check("elevenlabs: a body that is not JSON says the status", getattr(error, "code", "") == "voice_error"
          and error.detail == "HTTP 502", getattr(error, "detail", ""))
    _, _, error = el_run(refuse(403, raw=b'["not", "an", "object"]'))
    check("elevenlabs: a JSON list as the whole body says the status", getattr(error, "code", "") == "voice_no_access"
          and error.detail == "HTTP 403", getattr(error, "detail", ""))
    _, _, error = el_run(refuse(404, new("not_found", "model_not_found")), {**el_cfg, "elevenlabs_model": "eleven_v4"})
    check("elevenlabs: an error names the voice, its voice id and its model id",
          (getattr(error, "name", ""), getattr(error, "label", ""), getattr(error, "model", ""),
           getattr(error, "engine", "")) == ("elevenlabs", "ElevenLabs", "6WwXjDDEMyNmFG95zycZ", "eleven_v4"), "")
    _, _, error = run(refuse(401, {"err_msg": "No."}))
    check("speak: a Deepgram error names its voice and no model id",
          (getattr(error, "name", ""), getattr(error, "engine", "x")) == ("deepgram", ""), "")
    for broken in (urllib.error.URLError(OSError("no route")), TimeoutError(), ConnectionResetError()):
        code, _, _ = el_run(fails(broken))
        check(f"elevenlabs: {type(broken).__name__} is voice_unreachable", code == "voice_unreachable", code)
    code, _, _ = el_run(answer(broken=True))
    check("elevenlabs: an answer that breaks off is voice_unreachable", code == "voice_unreachable", code)

    # -- ElevenLabs: the key never travels on
    _, _, error = el_run(refuse(401, new("authentication_error", "invalid_api_key", f"Key {el_key} is bad. {el_key}")))
    detail = getattr(error, "detail", "")
    check("elevenlabs: a key said back in an error text is removed", el_key not in detail and KEY_REMOVED in detail,
          detail)
    _, _, error = el_run(refuse(401, old("invalid_api_key", "x" * 290 + el_key + " tail")))
    detail = getattr(error, "detail", "")
    check("elevenlabs: no piece of the key is left at the 300-character cut",
          len(detail) <= DETAIL_CHARS and el_key[:4] not in detail, detail[-20:])
    _, _, error = el_run(refuse(422, el_detail([{"loc": ["body"], "msg": f"bad {el_key}", "type": "x"}])))
    check("elevenlabs: a key said back in a 422 msg is removed", el_key not in getattr(error, "detail", el_key), "")
    _, _, error = el_run(refuse(401, new("authentication_error", "invalid_api_key", f"no {el_key}")))
    check("elevenlabs: the exception text is the code only", error is not None and str(error) == "voice_key_invalid"
          and error.args == ("voice_key_invalid",), str(error))

    # -- ElevenLabs: a redirect is never followed
    for status in (301, 302, 303, 307, 308):
        code, _, _ = el_run(refuse(status, new("x", "invalid_api_key")))
        check(f"elevenlabs: HTTP {status} raised by the opener is voice_bad_reply", code == "voice_bad_reply", code)
        fake = _FakeHTTPS(status, b"", {"Location": "https://elsewhere.example/steal"})
        chain = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect, fake)
        code, _, _ = el_run(None, opener=chain)
        check(f"elevenlabs: urllib's chain does not follow HTTP {status}", code == "voice_bad_reply"
              and fake.urls == [el_url], f"{code} {fake.urls}")
    fake = _FakeHTTPS(200, mp3, {"Content-Type": "audio/mpeg"})
    code, _, _ = el_run(None, opener=urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect, fake))
    check("elevenlabs: urllib's own answer object is read", code == f"ok:{AUDIO_MIME}:{len(mp3)}"
          and fake.urls == [el_url], code)

    # -- ElevenLabs: answers that are not good audio
    for what in ("a JSON answer", "an html type line", "no type line", "html bytes under an audio type line",
                 "too small", "HTTP 204"):
        code, _, _ = el_run(bad_replies[what])
        check(f"elevenlabs: {what} is voice_bad_reply", code == "voice_bad_reply", code)
    code, _, _ = el_run(answer(body=frame))
    check("elevenlabs: a frame sync start is good audio", code.startswith("ok:"), code)

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
    named = [voice for voice in VOICES["deepgram"]["known"] if voice in result["voice"][1]]
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
    for name in ("nonsense", None):
        result, opener = probed(answer(), name=name)
        check(f"probe: {name!r} is no server voice", not opener.calls and result["key"][0] is None
              and result["voice"][0] is None, str(result))

    # -- probe: ElevenLabs
    def el_probed(make, config: dict = el_cfg):
        return probed(make, config, "elevenlabs")

    result, opener = el_probed(answer(), default_config())
    check("probe elevenlabs: no key says where to type it", result["key"][0] is False
          and '"elevenlabs_api_key"' in result["key"][1] and result["voice"][0] is None and not opener.calls, str(result))
    for status, payload in ((401, new("authentication_error", "invalid_api_key", f"Refused {el_key}.")),
                            (403, new("authorization_error", "insufficient_permissions"))):
        result, _ = el_probed(refuse(status, payload))
        check(f"probe elevenlabs: HTTP {status} {payload['detail']['code']} is a refused key",
              result["key"][0] is False and result["voice"][0] is None, str(result))
    for status, payload in ((401, old("quota_exceeded", "You have insufficient quota to complete the request.")),
                            (402, new("payment_required", "insufficient_credits"))):
        result, _ = el_probed(refuse(status, payload))
        check(f"probe elevenlabs: HTTP {status} no credit is a good key and a voice that cannot speak",
              result["key"][0] is True and result["voice"][0] is False and "no credit" in result["voice"][1],
              str(result))
    result, _ = el_probed(refuse(403, new("authorization_error", "voice_access_denied", "This voice is not yours.")))
    check("probe elevenlabs: a voice the plan does not allow is a good key, the detail, and how to fix it",
          result["key"][0] is True and result["voice"][0] is False and "This voice is not yours." in result["voice"][1]
          and '"elevenlabs_voice"' in result["voice"][1] and '"elevenlabs_model"' in result["voice"][1]
          and "voice_not_allowed" in result["voice"][1], str(result))
    result, _ = el_probed(refuse(404, new("not_found", "voice_not_found", "A voice with that id was not found.")),
                          {**el_cfg, "elevenlabs_voice": "NobodyVoice123"})
    named = [voice for voice in VOICES["elevenlabs"]["known"] if voice in result["voice"][1]]
    check("probe elevenlabs: an unknown voice says how to fix it", result["key"][0] is True
          and result["voice"][0] is False and 'Fix "elevenlabs_voice" in config.json' in result["voice"][1]
          and len(named) == 3 and "NobodyVoice123" in result["voice"][1] and "My Voices" in result["voice"][1]
          and "(Eldrin)" in result["voice"][1], str(result))
    result, _ = el_probed(refuse(404, new("not_found", "voice_not_found")))
    check("probe elevenlabs: the voice that failed is not offered again",
          "6WwXjDDEMyNmFG95zycZ (" not in result["voice"][1].split("Some ids that exist:")[-1], str(result))
    result, _ = el_probed(refuse(404, new("not_found", "model_not_found")), {**el_cfg, "elevenlabs_model": "eleven_v9"})
    engines = [engine for engine in VOICES["elevenlabs"]["known_engines"] if engine in result["voice"][1]]
    check("probe elevenlabs: an unknown model says how to fix it", result["key"][0] is True
          and result["voice"][0] is False and 'Fix "elevenlabs_model" in config.json' in result["voice"][1]
          and "eleven_v9" in result["voice"][1] and len(engines) == 3, str(result))
    result, opener = el_probed(answer())
    check("probe elevenlabs: success names the voice id, the model id and the audio bytes",
          result["key"][0] is True and result["voice"][0] is True and "6WwXjDDEMyNmFG95zycZ" in result["voice"][1]
          and "eleven_flash_v2_5" in result["voice"][1] and str(len(mp3)) in result["voice"][1], str(result))
    check("probe elevenlabs: one request on the same path as speak", len(opener.calls) == 1
          and opener.calls[0]["url"] == el_url
          and json_body(opener.calls[0]) == {"text": PROBE_TEXT, "model_id": "eleven_flash_v2_5"},
          str(opener.calls[:1]))
    result, _ = el_probed(refuse(503, new("service_unavailable", "maintenance")))
    check("probe elevenlabs: a server error proves nothing about the key",
          result["key"][0] is None and result["voice"][0] is False, str(result))
    result, opener = el_probed(answer(), {**el_cfg, "elevenlabs_voice": "6WwX jDDE"})
    check("probe elevenlabs: a bad voice id sends no request", not opener.calls and result["key"][0] is None
          and result["voice"][0] is False and 'Fix "elevenlabs_voice" in config.json' in result["voice"][1],
          str(result))
    result, opener = el_probed(answer(), {**el_cfg, "elevenlabs_model": "Eleven V3"})
    check("probe elevenlabs: a bad model id sends no request", not opener.calls and result["key"][0] is None
          and result["voice"][0] is False and 'Fix "elevenlabs_model" in config.json' in result["voice"][1],
          str(result))
    for line in ("elevenlabs_voice", "elevenlabs_model"):
        result, opener = el_probed(answer(), {**el_cfg, line: el_key})
        check(f"probe elevenlabs: a key in {line} sends no request", not opener.calls and result["voice"][0] is False
              and el_key not in json.dumps(result), str(result))
    check("probe: no answer ever shows a key", shown and not any(key in text or el_key in text for text in shown), "")

    # -- a key of ANOTHER line of config.json (a brain's, the other voice's) never travels
    el_shaped = "sk_" + "0123456789abcdef" * 3            # made at run time: the shape of an ElevenLabs key (51)
    ds_key = "sk-" + "0123456789abcdef" * 2               # the shape of a DeepSeek key (35)
    check("keys: these key shapes fit the id rules, so only the key rule can stop them",
          VOICE_ID_RE.fullmatch(key) and VOICE_ID_RE.fullmatch(el_shaped) and ENGINE_ID_RE.fullmatch(ds_key), "")
    strangers = [
        ("the Deepgram key in elevenlabs_voice", "elevenlabs",
         {**cfg, "elevenlabs_api_key": el_key, "elevenlabs_voice": key}, key, "voice_not_found"),
        ("an ElevenLabs key in deepgram_voice", "deepgram",
         {**cfg, "elevenlabs_api_key": el_shaped, "deepgram_voice": el_shaped}, el_shaped, "voice_not_found"),
        ("a DeepSeek key in elevenlabs_model", "elevenlabs",
         {**el_cfg, "deepseek_api_key": ds_key, "elevenlabs_model": ds_key}, ds_key, "voice_model_not_found"),
        ("a DeepSeek key inside deepgram_voice", "deepgram",
         {**cfg, "deepseek_api_key": ds_key, "deepgram_voice": "aura-" + ds_key}, ds_key, "voice_not_found"),
        ("a DeepSeek key inside elevenlabs_voice", "elevenlabs",
         {**el_cfg, "deepseek_api_key": ds_key, "elevenlabs_voice": "6WwX" + ds_key}, ds_key, "voice_not_found"),
        ("a short OpenRouter key inside elevenlabs_voice", "elevenlabs",
         {**el_cfg, "openrouter_api_key": "k3y99", "elevenlabs_voice": "6WwXk3y99"}, "k3y99", "voice_not_found"),
        ("a short OpenRouter key inside deepgram_voice", "deepgram",
         {**cfg, "openrouter_api_key": "k3y99", "deepgram_voice": "aura-k3y99"}, "k3y99", "voice_not_found"),
        ("a short OpenRouter key inside elevenlabs_model", "elevenlabs",
         {**el_cfg, "openrouter_api_key": "k3y99", "elevenlabs_model": "eleven_k3y99"}, "k3y99", "voice_model_not_found"),
    ]
    for what, name, config, stranger, want in strangers:
        every = configured_keys(config)
        line = VOICES[name]["engine_key"] if want == "voice_model_not_found" else VOICES[name]["model_key"]
        code, opener, error = run(answer(), config, name=name)
        said = " ".join(str(getattr(error, field, "")) for field in ("detail", "model", "engine"))
        check(f"keys: {what} sends no request", code == want and not opener.calls, code)
        check(f"keys: {what} names the line, never the key", stranger in every
              and not any(known in said for known in every) and f'"{line}"' in said and "holds a key" in said
              and not re.search(r"[a-z]_api_key", said), said)
        page = json.dumps(describe_all(config))
        check(f"keys: {what} is not shown to the page", not any(known in page for known in every), "")
        result, opener = probed(answer(), config, name)
        told = json.dumps(result)
        check(f"keys: {what} is refused by the probe with no request", not opener.calls and result["voice"][0] is False
              and not any(known in told for known in every) and f'"{line}"' in result["voice"][1], told)

    harmless = {**cfg, "deepseek_api_key": KEY_PLACEHOLDER, "openai_api_key": "", "venice_api_key": "   ",
                "openrouter_api_key": 5, "claude_api_key": None, "notes_dir": "aura-2-draco-en",
                "api_key_notes": "aura-2-draco-en"}
    check("configured_keys: the placeholder, an empty line, a line that is no text and another name are no keys",
          configured_keys(harmless) == [key], f"{len(configured_keys(harmless))} keys")
    code, opener, _ = run(answer(), harmless)
    check("keys: such lines change nothing: the request goes out as before", code.startswith("ok:")
          and len(opener.calls) == 1 and opener.calls[0]["url"] == url, code)
    check("keys: such lines change nothing on the page", describe_all(harmless) == describe_all(cfg), "")
    many = {"a_api_key": "short1", "b_api_key": " a-much-longer-key-0001 ", "c_api_key": "short1",
            "deepgram_api_key": "mid-key-01", "elevenlabs_api_key": KEY_PLACEHOLDER}
    check("configured_keys: every key once, stripped, the longest first",
          configured_keys(many) == ["a-much-longer-key-0001", "mid-key-01", "short1"], str(configured_keys(many)))
    check("configured_keys: brain keys and voice keys", set(configured_keys({**el_cfg, "deepseek_api_key": ds_key}))
          == {el_key, ds_key}, "")
    for order in (["abc", "xxabcxx"], ["xxabcxx", "abc"]):
        check(f"scrub_keys: a key that holds a shorter key is removed whole ({' then '.join(order)})",
              scrub_keys("1 xxabcxx 2 abc", order) == f"1 {KEY_REMOVED} 2 {KEY_REMOVED}", scrub_keys("1 xxabcxx 2 abc", order))
    check("scrub_keys: no keys, empty keys and the placeholder change nothing",
          scrub_keys(f"a {KEY_PLACEHOLDER}", []) == scrub_keys(f"a {KEY_PLACEHOLDER}", ["", "  ", KEY_PLACEHOLDER])
          == f"a {KEY_PLACEHOLDER}", "")
    check("VoiceError: one key as a text is one key, not its letters", VoiceError("x", "a b ab", None, "ab").detail
          == f"a b {KEY_REMOVED}", VoiceError("x", "a b ab", None, "ab").detail)
    other = {**el_cfg, "deepseek_api_key": ds_key}
    _, _, error = el_run(refuse(401, new("authentication_error", "invalid_api_key", f"Not {ds_key}, not {el_key}.")),
                         other)
    detail = getattr(error, "detail", "")
    check("keys: a key of another line said back in an ElevenLabs error is removed", ds_key not in detail
          and el_key not in detail and detail.count(KEY_REMOVED) == 2, detail)
    _, _, error = el_run(refuse(401, old("invalid_api_key", "x" * 290 + ds_key + " tail")), other)
    detail = getattr(error, "detail", "")
    check("keys: no piece of a key of another line is left at the 300-character cut",
          len(detail) <= DETAIL_CHARS and ds_key[:5] not in detail, detail[-20:])
    _, _, error = run(refuse(401, {"err_msg": f"bad {ds_key} and {el_key}"}), {**cfg, **other})
    detail = getattr(error, "detail", "")
    check("keys: a key of another line said back in a Deepgram error is removed",
          ds_key not in detail and el_key not in detail and key not in detail, detail)

    # -- a key that is no header-safe text is never sent (a header would break, and http.client says the value back)
    try:
        connection = http.client.HTTPConnection("example.invalid")          # no connection is made: no network
        connection.putrequest("POST", "/", skip_host=True, skip_accept_encoding=True)
        connection.putheader("xi-api-key", "abc\ndef")
        said_back = ""
    except ValueError as error:
        said_back = str(error)
    check("keys: http.client's own error text holds the header value (why KEY_TEXT_RE exists)",
          "abc" in said_back and "def" in said_back, said_back)
    for what, bad in (("a line break", "abcd\nefgh1234"), ("a CR LF", "abcd\r\nefgh1234"),
                      ("a space", "abcd efgh1234"), ("a tab", "abcd\tefgh1234"), ("a letter outside ASCII", "abcdäfgh1234"),
                      ("a control character", "abcd\x7fefgh1234")):
        for name, base in (("deepgram", cfg), ("elevenlabs", el_cfg)):
            config = {**base, VOICES[name]["key"]: bad}
            code, opener, error = run(answer(), config, name=name)
            said = " ".join(str(getattr(error, field, "")) for field in ("detail", "model", "engine")) + str(error)
            check(f"keys: a {name} key with {what} sends no request and never shows it",
                  code == "voice_key_bad_text" and not opener.calls and bad not in said
                  and f'"{VOICES[name]["key"]}"' in said, f"{code} {said}")
            result, opener = probed(answer(), config, name)
            check(f"keys: the probe of a {name} key with {what} sends nothing and never shows it",
                  not opener.calls and result["key"][0] is False and bad not in json.dumps(result)
                  and "voice_key_bad_text" in result["key"][1], str(result))
    for kind in (ValueError, TypeError):
        _, _, error = el_run(fails(kind(f"Invalid header value b'{el_key}\\n'")))
        check(f"keys: a {kind.__name__} on the way out is a VoiceError that names it, never its text",
              isinstance(error, VoiceError) and error.code == "voice_not_sent" and el_key not in error.detail
              and str(error) == "voice_not_sent" and kind.__name__ in error.detail, str(getattr(error, "detail", error)))

    # -- a key copied from ANOTHER line is never sent to this provider
    copies = [
        ("the ElevenLabs key is the DeepSeek key", "elevenlabs", {**el_cfg, "deepseek_api_key": el_key},
         "deepseek_api_key"),
        ("the ElevenLabs key is the Anthropic key", "elevenlabs", {**el_cfg, "anthropic_api_key": f" {el_key} "},
         "anthropic_api_key"),
        ("the Deepgram key is the ElevenLabs key", "deepgram", {**cfg, "elevenlabs_api_key": key}, "elevenlabs_api_key"),
        ("the ElevenLabs key is the Deepgram key", "elevenlabs", {**cfg, "elevenlabs_api_key": key}, "deepgram_api_key"),
        ("the Deepgram key holds the OpenAI key", "deepgram", {**cfg, "openai_api_key": key[:20]}, "openai_api_key"),
        ("the Deepgram key is held by the OpenRouter key", "deepgram", {**cfg, "openrouter_api_key": f"x{key}y"},
         "openrouter_api_key"),
    ]
    for what, name, config, other in copies:
        own = VOICES[name]["key"]
        code, opener, error = run(answer(), config, name=name)
        said = " ".join(str(getattr(error, field, "")) for field in ("detail", "model", "engine"))
        check(f"keys: {what}: no request", code == "voice_key_copied" and not opener.calls, code)
        check(f"keys: {what}: both lines are named, no key is shown", f'"{own}"' in said and f'"{other}"' in said
              and not any(known in said for known in configured_keys(config)), said)
        result, opener = probed(answer(), config, name)
        check(f"keys: {what}: the probe sends nothing", not opener.calls and result["key"][0] is False
              and f'"{other}"' in result["key"][1] and not any(known in json.dumps(result) for known in configured_keys(config)),
              str(result))
    distinct = {**cfg, "elevenlabs_api_key": el_key, "deepseek_api_key": ds_key, "openai_api_key": "fake-openai-0001",
                "anthropic_api_key": "fake-anthropic-0002"}
    for name in SERVER_VOICES:
        code, opener, _ = run(answer(), distinct, name=name)
        check(f"keys: distinct keys still send ({name})", code.startswith("ok:") and len(opener.calls) == 1, code)
    check("key_lines: each key line with its stripped key, no placeholder",
          key_lines({**el_cfg, "anthropic_api_key": " x1 ", "openai_api_key": KEY_PLACEHOLDER})
          == {"anthropic_api_key": "x1", "elevenlabs_api_key": el_key}, "")
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
