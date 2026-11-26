"""Jarviz brains: one way to ask whichever model you chose in config.json.

    "brain": "anthropic" | "openai" | "deepseek" | "venice" | "openrouter"

Claude uses the official `anthropic` package. OpenAI uses its Responses API. DeepSeek, Venice and
OpenRouter use the OpenAI-style chat completions format. Those four go over plain HTTPS from the
standard library. Every key comes from config.json on the server. No key ever reaches the browser.

To add a brain: add one entry to PROVIDERS and its two lines to config.json.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

try:                    # optional at boot: without the package only the Claude brain is missing
    import anthropic
except ImportError:
    anthropic = None

KEY_PLACEHOLDER = "PUT-YOUR-KEY-HERE"
REQUEST_TIMEOUT_S = 90.0
USER_AGENT = "Jarviz/1.0"

# Thinking and answer share the token budget on most current models. First try, then one retry
# with a bigger budget when the text comes back empty.
MAX_TOKENS = {"chat": (4000, 16000), "teardown": (9000, 16000)}
# A teardown note is long, so its call may take longer than a chat answer.
TIMEOUT_S = {"chat": REQUEST_TIMEOUT_S, "teardown": 240.0}
# How often the anthropic package may try a failed call again by itself. A teardown waits long for
# one answer, so it gets no silent second and third wait on top.
RETRIES = {"chat": 2, "teardown": 0}

# Claude Opus 5.5: thinking is always on, so no thinking setting is ever sent, and no tool choice
# is forced. Depth is set per route with effort.
EFFORT = {"chat": "low", "teardown": "medium"}      # casual chat is snappier at low effort
# If Claude declines a question, the API can run it again on a backup model in the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = frozenset(
    {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1"}
)

# kind "anthropic": the anthropic package.  kind "responses": POST {base_url}/responses.
# kind "chat": POST {base_url}/chat/completions; token_fields = the name to try first, then the other.
PROVIDERS = {
    "anthropic": {
        "label": "Claude", "kind": "anthropic",
        "key": "anthropic_api_key", "model_key": "model", "default_model": "claude-opus-5-5",
    },
    "openai": {
        "label": "OpenAI", "kind": "responses", "base_url": "https://api.openai.com/v1",
        "key": "openai_api_key", "model_key": "openai_model", "default_model": "gpt-6.2-sol",
    },
    "deepseek": {
        "label": "DeepSeek", "kind": "chat", "base_url": "https://api.deepseek.com",
        "key": "deepseek_api_key", "model_key": "deepseek_model", "default_model": "deepseek-flash",
        "token_fields": ("max_tokens", "max_completion_tokens"),
    },
    "venice": {
        "label": "Venice", "kind": "chat", "base_url": "https://api.venice.ai/api/v1",
        "key": "venice_api_key", "model_key": "venice_model", "default_model": "venice-uncensored-1-2",
        "token_fields": ("max_completion_tokens", "max_tokens"),
        # Jarviz brings his own character, and thinking text must not be read aloud.
        "extra": {"venice_parameters": {"include_venice_system_prompt": False, "strip_thinking_response": True}},
    },
    "openrouter": {
        "label": "OpenRouter", "kind": "chat", "base_url": "https://openrouter.ai/api/v1",
        "key": "openrouter_api_key", "model_key": "openrouter_model", "default_model": "openrouter/auto",
        "token_fields": ("max_completion_tokens", "max_tokens"),
    },
}
DEFAULT_BRAIN = "anthropic"
# What a person may call a brain, with spaces and capitals removed.
ALIASES = {"claude": "anthropic", "anthropic": "anthropic", "opus": "anthropic",
           "openai": "openai", "gpt": "openai", "chatgpt": "openai",
           "deepseek": "deepseek", "venice": "venice", "openrouter": "openrouter"}

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class BrainError(Exception):
    """The brain could not answer. `code` says why. `detail` is the provider's own short message."""

    def __init__(self, code: str, detail: str = "", brain: dict | None = None):
        super().__init__(code)
        self.code = code
        self.detail = " ".join(str(detail).split())[:300]
        self.label = (brain or {}).get("label", "the")
        self.model = (brain or {}).get("model", "")


# ------------------------------------------------------------------- choosing

def default_config() -> dict:
    """The brain lines of config.json: which brain is active, and a key and a model for each."""
    config = {"brain": DEFAULT_BRAIN}
    for spec in PROVIDERS.values():
        config[spec["key"]] = KEY_PLACEHOLDER
        config[spec["model_key"]] = spec["default_model"]
    return config


def resolve_name(spoken: str) -> str | None:
    """ "Deep Seek", "the OpenAI brain", "claude" -> the name used in config.json, or None."""
    squashed = re.sub(r"[^a-z]", "", str(spoken).lower())
    squashed = re.sub(r"^the|(?:brain|model|api)$", "", squashed)
    return ALIASES.get(squashed)


def active_name(config: dict) -> str:
    name = str(config.get("brain", "")).strip().lower()
    return name if name in PROVIDERS else DEFAULT_BRAIN


def describe(config: dict, name: str | None = None) -> dict:
    """What the browser may know about a brain. Never the key itself."""
    name = name or active_name(config)
    spec = PROVIDERS[name]
    key = str(config.get(spec["key"], "")).strip()
    return {
        "name": name,
        "label": spec["label"],
        "model": str(config.get(spec["model_key"]) or spec["default_model"]).strip(),
        "has_key": key not in ("", KEY_PLACEHOLDER),
    }


def describe_all(config: dict) -> list[dict]:
    return [describe(config, name) for name in PROVIDERS]


def state(config: dict) -> str:
    brain = describe(config)
    if not brain["has_key"]:
        return "no_key"
    if PROVIDERS[brain["name"]]["kind"] == "anthropic" and anthropic is None:
        return "sdk_missing"
    return "ready"


# --------------------------------------------------------------------- asking

def merge_same_role(messages: list[dict]) -> list[dict]:
    """Join neighbours with the same role. Some providers demand strict user/assistant turns."""
    merged: list[dict] = []
    for message in messages:
        if merged and merged[-1]["role"] == message["role"]:
            merged[-1] = {"role": message["role"], "content": merged[-1]["content"] + "\n\n" + message["content"]}
        else:
            merged.append({"role": message["role"], "content": message["content"]})
    return merged


def ask(config: dict, route: str, system: str, messages: list[dict]) -> str:
    """Ask the active brain and return its text. Raises BrainError."""
    brain = describe(config)
    spec = PROVIDERS[brain["name"]]
    if not brain["has_key"]:
        raise BrainError("api_key_missing", brain=brain)
    if spec["kind"] == "anthropic" and anthropic is None:
        raise BrainError("sdk_missing", brain=brain)
    key = str(config[spec["key"]]).strip()
    messages = merge_same_role(messages)
    call = {"anthropic": _claude_once, "responses": _responses_once, "chat": _chat_once}[spec["kind"]]
    budgets = MAX_TOKENS[route]
    for attempt, budget in enumerate(budgets):
        try:
            text = call(key, brain, spec, route, system, messages, budget)
        except BrainError as error:
            if attempt and error.code == "bad_request":     # this model cannot take the bigger budget
                break
            raise
        if text:
            return text
    raise BrainError("empty_reply", brain=brain)


# -- Claude (the anthropic package)

_client_lock = threading.Lock()
_client_cache: tuple[str, object] | None = None
_feature_level: dict[str, int] = {}      # per model: how many optional request features to leave out


def get_client(api_key: str):
    global _client_cache
    with _client_lock:
        if _client_cache is None or _client_cache[0] != api_key:
            _client_cache = (api_key, anthropic.Anthropic(api_key=api_key, timeout=REQUEST_TIMEOUT_S))
        return _client_cache[1]


def create_message(client, model: str, route: str, system: str, messages: list[dict], max_tokens: int):
    """One Messages API call. Optional features are dropped one by one if the model rejects them."""
    ladder = [("fallbacks", "effort"), ("effort",), ()]
    if model not in FALLBACK_MODELS:
        ladder = ladder[1:]
    rejected = None
    if hasattr(client, "with_options"):
        client = client.with_options(max_retries=RETRIES.get(route, 2))
    for level in range(min(_feature_level.get(model, 0), len(ladder) - 1), len(ladder)):
        features = ladder[level]
        request = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages,
                   "timeout": TIMEOUT_S.get(route, REQUEST_TIMEOUT_S)}
        if "effort" in features:
            request["output_config"] = {"effort": EFFORT[route]}
        try:
            if "fallbacks" in features:
                message = client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **request)
            else:
                message = client.messages.create(**request)
        except anthropic.BadRequestError as error:
            rejected = error
            continue
        _feature_level[model] = level
        return message
    raise rejected


def _claude_detail(error) -> str:
    body = getattr(error, "body", None)
    if isinstance(body, dict) and isinstance(body.get("error"), dict) and body["error"].get("message"):
        return str(body["error"]["message"])
    return str(getattr(error, "message", "") or type(error).__name__)


def _claude_error(error, brain: dict) -> BrainError:
    """Turn an anthropic error into a BrainError. Most specific class first."""
    detail = _claude_detail(error)
    if isinstance(error, anthropic.AuthenticationError):
        return BrainError("api_key_invalid", detail, brain)
    if isinstance(error, anthropic.PermissionDeniedError):
        return BrainError("api_permission", detail, brain)
    if isinstance(error, anthropic.NotFoundError):
        return BrainError("model_not_found", detail, brain)
    if isinstance(error, anthropic.RateLimitError):
        return BrainError("rate_limited", detail, brain)
    if isinstance(error, anthropic.BadRequestError):
        return BrainError("no_credit" if "credit balance" in detail.lower() else "bad_request", detail, brain)
    if isinstance(error, anthropic.APIStatusError):
        return BrainError("api_error" if error.status_code >= 500 else "bad_request", detail, brain)
    return BrainError("api_unreachable", type(error).__name__, brain)


def _claude_once(key, brain, spec, route, system, messages, budget) -> str:
    try:
        message = create_message(get_client(key), brain["model"], route, system, messages, budget)
    except anthropic.APIError as error:
        raise _claude_error(error, brain) from None
    if message.stop_reason == "refusal":
        raise BrainError("refused", brain=brain)
    return "".join(block.text for block in message.content if block.type == "text").strip()


# -- the other brains (plain HTTPS)

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None                      # a key must never follow a redirect to another address


_OPENER = urllib.request.build_opener(_NoRedirect)
_token_field: dict[str, str] = {}        # per brain: the token-limit name its API accepted


def _json_of(raw: bytes) -> dict:
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _error_text(payload: dict) -> tuple[str, str]:
    """(message, code words) from an error body. Providers shape these differently."""
    error = payload.get("error")
    if isinstance(error, str):
        return error, ""
    if isinstance(error, dict):
        return str(error.get("message") or ""), f"{error.get('code') or ''} {error.get('type') or ''}"
    return str(payload.get("message") or ""), ""


def _http_error(status: int, payload: dict, brain: dict) -> BrainError:
    detail, words = _error_text(payload)
    text = f"{detail} {words}".lower()
    if status == 402 or (status in (403, 429) and re.search(r"credit|quota|balance|spend limit|billing", text)):
        code = "no_credit"
    elif status == 401:
        code = "api_key_invalid"
    elif status == 404 or ("model" in text and re.search(r"not exist|not found|unknown model|invalid model", text)):
        code = "model_not_found"
    elif status == 403:
        code = "api_permission"
    elif status == 429:
        code = "rate_limited"
    elif status == 408 or status >= 500:
        code = "api_error"
    else:
        code = "bad_request"
    return BrainError(code, detail or f"HTTP {status}", brain)


def _call(method: str, url: str, key: str, body: dict | None, brain: dict, timeout: float = REQUEST_TIMEOUT_S) -> dict:
    request = urllib.request.Request(
        url, method=method, data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            payload = _json_of(response.read())
    except urllib.error.HTTPError as error:
        raise _http_error(error.code, _json_of(error.read()), brain) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise BrainError("api_unreachable", type(error).__name__, brain) from None
    if payload.get("error") and "choices" not in payload and "output" not in payload:
        # some routers answer 200 and put the error in the body
        inner = payload["error"].get("code") if isinstance(payload["error"], dict) else None
        raise _http_error(inner if isinstance(inner, int) else 502, payload, brain)
    return payload


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") for part in content
                       if isinstance(part, dict) and part.get("type") in ("text", "output_text"))
    return ""


def _chat_once(key, brain, spec, route, system, messages, budget) -> str:
    """OpenAI-style chat completions: DeepSeek, Venice, OpenRouter."""
    url = spec["base_url"] + "/chat/completions"
    body = {"model": brain["model"], "messages": [{"role": "system", "content": system}, *messages],
            **spec.get("extra", {})}
    first, second = spec["token_fields"]
    field = _token_field.get(brain["name"], first)
    wait = TIMEOUT_S.get(route, REQUEST_TIMEOUT_S)
    try:
        data = _call("POST", url, key, {**body, field: budget}, brain, wait)
    except BrainError as error:
        if error.code != "bad_request" or "max_" not in error.detail.lower():
            raise
        field = second if field == first else first          # this API wants the other name
        data = _call("POST", url, key, {**body, field: budget}, brain, wait)
    _token_field[brain["name"]] = field
    choice = (data.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}
    text = THINK_RE.sub("", _text_of(message.get("content"))).strip()
    if not text and (message.get("refusal") or choice.get("finish_reason") == "content_filter"):
        raise BrainError("refused", brain=brain)
    return text


def _responses_once(key, brain, spec, route, system, messages, budget) -> str:
    """The OpenAI Responses API. store=False: OpenAI does not keep the conversation."""
    body = {"model": brain["model"], "instructions": system, "input": messages,
            "max_output_tokens": budget, "store": False}
    data = _call("POST", spec["base_url"] + "/responses", key, body, brain, TIMEOUT_S.get(route, REQUEST_TIMEOUT_S))
    texts, refused = [], False
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "refusal":
                refused = True
        texts.append(_text_of(item.get("content")))
    text = "".join(texts).strip()
    if not text and refused:
        raise BrainError("refused", brain=brain)
    return text


# -------------------------------------------------------------------- probing

def list_models(config: dict, name: str, limit: int = 8) -> list[str]:
    """Some model ids this brain offers. Best effort: an empty list when it cannot be read."""
    spec, brain = PROVIDERS[name], describe(config, name)
    if spec["kind"] == "anthropic" or not brain["has_key"]:
        return []
    try:
        data = _call("GET", spec["base_url"] + "/models", str(config[spec["key"]]).strip(), None, brain, timeout=20)
    except BrainError:
        return []
    return [str(item["id"]) for item in data.get("data") or [] if isinstance(item, dict) and item.get("id")][:limit]


def probe(config: dict, name: str | None = None) -> dict:
    """One real, cheap call that proves the key works and the model can be reached.

    Returns {"key": (ok, detail), "model": (ok, detail)}. ok is True, False, or None for "not checked".
    """
    brain = describe(config, name)
    spec = PROVIDERS[brain["name"]]
    if not brain["has_key"]:
        return {"key": (False, f"config.json has no {brain['label']} key yet. Type it into \"{spec['key']}\"."),
                "model": (None, "")}
    key = str(config[spec["key"]]).strip()
    try:
        if spec["kind"] == "anthropic":
            if anthropic is None:
                return {"key": (False, "The anthropic package is missing. Run: pip install anthropic"), "model": (None, "")}
            client = anthropic.Anthropic(api_key=key, timeout=30.0)
            try:
                client.models.list(limit=1)
            except anthropic.APIError as error:
                raise _claude_error(error, brain) from None
            try:
                found = client.models.retrieve(brain["model"])
            except anthropic.APIError as error:
                model_error = _claude_error(error, brain)
                return {"key": (True, ""), "model": (False, _model_trouble(config, brain, model_error))}
            return {"key": (True, ""), "model": (True, f"{brain['model']} ({getattr(found, 'display_name', '') or 'found'})")}
        if spec["kind"] == "responses":
            model_id = urllib.parse.quote(brain["model"], safe="")
            _call("GET", f"{spec['base_url']}/models/{model_id}", key, None, brain, timeout=30)
        else:
            body = {"model": brain["model"], "messages": [{"role": "user", "content": "Reply with the word OK."}],
                    _token_field.get(brain["name"], spec["token_fields"][0]): 32, **spec.get("extra", {})}
            _call("POST", spec["base_url"] + "/chat/completions", key, body, brain, timeout=60)
    except BrainError as error:
        if error.code in ("api_key_invalid", "api_permission"):
            return {"key": (False, f"{brain['label']} refused the key. {error.detail}"), "model": (None, "")}
        if error.code == "api_unreachable":
            return {"key": (False, f"Cannot reach {brain['label']}. Check the internet connection."), "model": (None, "")}
        if error.code == "no_credit":
            return {"key": (True, "the key is good"), "model": (False, f"The {brain['label']} account has no credit. {error.detail}")}
        return {"key": (True, ""), "model": (False, _model_trouble(config, brain, error))}
    return {"key": (True, ""), "model": (True, brain["model"])}


def _model_trouble(config: dict, brain: dict, error: BrainError) -> str:
    spec = PROVIDERS[brain["name"]]
    text = f"The model id {brain['model']!r} did not work with the {brain['label']} key ({error.code}). {error.detail}"
    offered = list_models(config, brain["name"])
    if offered:
        text += f" Some ids it offers: {', '.join(offered)}."
    return text + f" Fix \"{spec['model_key']}\" in config.json."
