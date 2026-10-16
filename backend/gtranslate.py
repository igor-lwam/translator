"""Google Translate client with endpoint fallback, retries and an in-memory cache.

The public translate.google.com/m and translate.googleapis.com (client=gtx) endpoints
answer server IPs with a captcha, so we try several endpoints in order:
  1. official Cloud Translation v2 (only if GOOGLE_TRANSLATE_API_KEY is set)
  2. clients5.google.com (client=dict-chrome-ex)
  3. translate.googleapis.com (client=gtx)
"""

import html
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

SOURCE, TARGET = "en", "ru"
_TIMEOUT = 30
_MAX_CHARS = 1500      # per GET request (URL length limit)
_MAX_ITEMS = 100       # per request
_PAUSE = 0.2           # between chunks
_RETRIES = 2
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class TranslateError(Exception):
    pass


class _Blocked(Exception):
    """Endpoint refused us (captcha / rate limit) — skip it for the rest of the call."""


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_CACHE: OrderedDict[str, str] = OrderedDict()
_CACHE_MAX = 20000


def _cache_get(text: str) -> str | None:
    if text in _CACHE:
        _CACHE.move_to_end(text)
        return _CACHE[text]
    return None


def _cache_put(text: str, ru: str) -> None:
    _CACHE[text] = ru
    _CACHE.move_to_end(text)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _request(url: str, data: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": _UA, **(headers or {})})
    last_err = None
    for attempt in range(_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                if "/sorry" in resp.geturl():
                    raise _Blocked("captcha (google.com/sorry)")
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) or "/sorry" in (e.headers.get("Location") or ""):
                raise _Blocked(f"HTTP {e.code}")
            last_err = f"HTTP {e.code}"
        except urllib.error.URLError as e:
            last_err = f"network error: {e.reason}"
        else:
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                raise _Blocked("non-JSON response (captcha page?)")
        time.sleep(0.5 * 2 ** attempt)
    raise TranslateError(last_err)


# ---------------------------------------------------------------------------
# Providers: each takes a list of texts, returns a list of translations
# ---------------------------------------------------------------------------

def _cloud_v2(texts: list[str]) -> list[str]:
    key = os.environ["GOOGLE_TRANSLATE_API_KEY"]
    url = "https://translation.googleapis.com/language/translate/v2?key=" + urllib.parse.quote(key)
    data = json.dumps({"q": texts, "source": SOURCE, "target": TARGET, "format": "text"}).encode()
    resp = _request(url, data, {"Content-Type": "application/json"})
    return [t["translatedText"] for t in resp["data"]["translations"]]


def _clients5(texts: list[str]) -> list[str]:
    params = [("client", "dict-chrome-ex"), ("sl", SOURCE), ("tl", TARGET)]
    params += [("q", t) for t in texts]
    resp = _request("https://clients5.google.com/translate_a/t?" + urllib.parse.urlencode(params))
    # sl=en → ["...", "..."]; sl=auto → [["...", "en"], ...]
    return [r[0] if isinstance(r, list) else r for r in resp]


def _gtx(texts: list[str]) -> list[str]:
    out = []
    for text in texts:
        params = {"client": "gtx", "sl": SOURCE, "tl": TARGET, "dt": "t", "q": text}
        resp = _request("https://translate.googleapis.com/translate_a/single?"
                        + urllib.parse.urlencode(params))
        out.append("".join(seg[0] for seg in resp[0] if seg and seg[0]))
    return out


def _providers():
    if os.environ.get("GOOGLE_TRANSLATE_API_KEY"):
        yield "cloud-v2", _cloud_v2
    yield "clients5", _clients5
    yield "gtx", _gtx


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _chunks(texts: list[str]):
    chunk, size = [], 0
    for t in texts:
        if chunk and (size + len(t) > _MAX_CHARS or len(chunk) >= _MAX_ITEMS):
            yield chunk
            chunk, size = [], 0
        chunk.append(t)
        size += len(t)
    if chunk:
        yield chunk


def translate_texts(texts: list[str]) -> dict[str, str]:
    """Translate EN→RU. Returns {original: translation}. Raises TranslateError."""
    results: dict[str, str] = {}
    todo = []
    for t in dict.fromkeys(texts):
        cached = _cache_get(t)
        if cached is not None:
            results[t] = cached
        elif t.strip():
            todo.append(t)

    providers = list(_providers())
    errors: dict[str, str] = {}

    for i, chunk in enumerate(_chunks(todo)):
        if i:
            time.sleep(_PAUSE)
        while True:
            if not providers:
                detail = "; ".join(f"{n}: {e}" for n, e in errors.items())
                raise TranslateError(f"все источники недоступны ({detail})")
            name, fn = providers[0]
            try:
                translated = fn(chunk)
                if len(translated) != len(chunk):
                    raise _Blocked(f"got {len(translated)} results for {len(chunk)} texts")
                break
            except (_Blocked, TranslateError, KeyError, IndexError, TypeError) as e:
                errors[name] = str(e) or type(e).__name__
                providers.pop(0)
        for orig, ru in zip(chunk, translated):
            ru = html.unescape(ru).strip()
            if ru:
                _cache_put(orig, ru)
                results[orig] = ru

    return results
