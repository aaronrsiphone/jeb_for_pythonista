"""Client for Brave's LLM Context search API, used by the web_search tool.

Brave's LLM Context endpoint returns page content already extracted and cut
to a token budget, rather than a results page of titles and snippets.  That
is why the search agent in ``tools/web_search.py`` needs no page-fetching
tool: one call yields passages it can read and cite.  This module owns
everything about talking to Brave — the request shape, the account's rate
and monthly limits, and turning the JSON response into compact plain text —
so the tool deals only in queries and answers.

Limits are enforced on this side because overrunning them is costly both
ways: the account allows two requests per second and 2,000 a month, and a
quota exhausted mid-month disables the tool until the next billing period.
The monthly count is persisted in the state directory (outside any project
workspace, like the session logs) and keyed by calendar month.  Brave's
billing period may roll over on a different day, so treat the count as an
approximation that errs on the side of stopping early.

Every network call goes through an injectable ``send`` callable, so tests —
and any environment that cannot reach Brave — run against a fake.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time

ENDPOINT = "https://api.search.brave.com/res/v1/llm/context"

# Brave moved LLM Context to a new extraction pipeline by default on
# 2026-07-31.  Set this to "2026-02-06" to pin the previous pipeline; None
# sends no Api-Version header and takes whatever Brave currently serves.
API_VERSION = None

REQUESTS_PER_SECOND = 2
MONTHLY_LIMIT = 2000
TIMEOUT = 30  # seconds; Brave's own recommendation for this endpoint

# Brave's documented query limits.
MAX_QUERY_CHARS = 600
MAX_QUERY_WORDS = 75

# The context budget for one search.  Brave suggests about 2048 tokens for
# simple factual lookups and 8192 (its default) for standard queries; 4096
# returns real passages while keeping an iterative agent's own context
# small.  Brave treats the token budgets as the binding but approximate
# limit, so render() also caps the text it hands on.
DEFAULT_PARAMS = {
    "count": 10,
    "maximum_number_of_tokens": 4096,
    "maximum_number_of_tokens_per_url": 1024,
    # Fewer, more relevant passages: precision over recall.
    "context_threshold_mode": "strict",
}

FRESHNESS_PRESETS = ("pd", "pw", "pm", "py")
_FRESHNESS_RANGE = re.compile(r"^\d{4}-\d{2}-\d{2}to\d{4}-\d{2}-\d{2}$")

USAGE_FILENAME = "brave_usage.json"
_USAGE_MONTHS_KEPT = 12


class SearchError(Exception):
    """A search could not be run.  The message is shown to the model."""


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------

def validate_query(query) -> str:
    """Return *query* stripped, or raise SearchError naming the limit broken.

    An over-long query is refused rather than truncated: cutting it changes
    its meaning, while an error lets the search agent rewrite it.
    """
    if not isinstance(query, str) or not query.strip():
        raise SearchError("brave_search needs a non-empty 'query'")
    query = query.strip()
    if len(query) > MAX_QUERY_CHARS:
        raise SearchError(
            f"query is {len(query)} characters; Brave's limit is "
            f"{MAX_QUERY_CHARS}. Shorten it."
        )
    words = len(query.split())
    if words > MAX_QUERY_WORDS:
        raise SearchError(
            f"query is {words} words; Brave's limit is {MAX_QUERY_WORDS}. "
            "Shorten it."
        )
    return query


def validate_freshness(freshness):
    """Return a valid freshness value or None; raise SearchError otherwise."""
    if freshness in (None, ""):
        return None
    if not isinstance(freshness, str):
        raise SearchError("'freshness' must be a string")
    value = freshness.strip()
    if value in FRESHNESS_PRESETS or _FRESHNESS_RANGE.match(value):
        return value
    raise SearchError(
        "'freshness' must be one of pd, pw, pm, py, or a date range "
        "YYYY-MM-DDtoYYYY-MM-DD"
    )


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

class BraveSearch:
    """Rate-limited, quota-tracking client for the LLM Context endpoint.

    *load_key* is a zero-argument callable returning the Brave API key (see
    ``keys.load_brave_key``).  *state_dir* is where the monthly request
    count is kept; ``None`` disables quota tracking.  *send*, *clock*,
    *sleep* and *month* exist for tests.
    """

    def __init__(self, load_key, state_dir, send=None, clock=time.monotonic,
                 sleep=time.sleep, month=None, params=None):
        self._load_key = load_key
        self._usage_path = (os.path.join(state_dir, USAGE_FILENAME)
                            if state_dir else None)
        self._send = send or _requests_send
        self._clock = clock
        self._sleep = sleep
        self._month = month or (lambda: time.strftime("%Y-%m"))
        self._last_request = None
        self.params = dict(DEFAULT_PARAMS if params is None else params)

    # -- key ----------------------------------------------------------------

    def _key(self) -> str:
        try:
            return str(self._load_key() or "").strip()
        except Exception:
            return ""

    def has_key(self) -> bool:
        return bool(self._key())

    # -- monthly quota ------------------------------------------------------

    def usage(self) -> tuple:
        """Return ``(month, requests counted this month)``."""
        month = self._month()
        return month, int(self._read_usage().get(month, 0) or 0)

    def remaining(self) -> int:
        return max(0, MONTHLY_LIMIT - self.usage()[1])

    def _read_usage(self) -> dict:
        if not self._usage_path:
            return {}
        try:
            with open(self._usage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _count_request(self):
        """Add one to this month's count.  Never raises: tracking is advisory."""
        if not self._usage_path:
            return
        month = self._month()
        data = self._read_usage()
        data[month] = int(data.get(month, 0) or 0) + 1
        for old in sorted(data)[:-_USAGE_MONTHS_KEPT]:
            data.pop(old, None)
        try:
            directory = os.path.dirname(self._usage_path) or "."
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, self._usage_path)
        except OSError:
            pass

    # -- rate limit ---------------------------------------------------------

    def _throttle(self):
        """Space requests at least 1/REQUESTS_PER_SECOND apart."""
        interval = 1.0 / REQUESTS_PER_SECOND
        if self._last_request is not None:
            wait = interval - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    # -- search -------------------------------------------------------------

    def search(self, query, freshness=None, goggles=None) -> dict:
        """Run one LLM Context search and return Brave's JSON response.

        Raises SearchError for an invalid argument, a missing key, an
        exhausted monthly quota, or any failed request.  A 429 is retried
        once after a one-second pause.
        """
        query = validate_query(query)
        freshness = validate_freshness(freshness)
        key = self._key()
        if not key:
            raise SearchError(
                "no Brave API key is stored: run ':key set brave <key>' "
                "(or set BRAVE_API_KEY)"
            )
        if self.remaining() <= 0:
            raise SearchError(
                f"Brave monthly limit reached ({MONTHLY_LIMIT} requests in "
                f"{self._month()}); web search is unavailable until next month"
            )

        # POST with a JSON body: Brave accepts the same parameters either
        # way, and an inline goggle can be far longer than a URL allows.
        body = dict(self.params)
        body["q"] = query
        if freshness:
            body["freshness"] = freshness
        if goggles:
            body["goggles"] = goggles
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Subscription-Token": key,
        }
        if API_VERSION:
            headers["Api-Version"] = API_VERSION

        status, payload = None, None
        for attempt in (1, 2):
            self._throttle()
            status, payload = self._send(ENDPOINT, headers, body, TIMEOUT)
            if status == 429 and attempt == 1:
                self._sleep(1.0)
                continue
            break

        # Rate-limited requests are not served; anything else that reached
        # Brave is counted, so the local count errs on the side of stopping.
        if status != 429:
            self._count_request()

        if status == 200 and isinstance(payload, dict):
            return payload
        snippet = _snippet(payload)
        if status == 200:
            raise SearchError(f"Brave returned an unreadable response: {snippet}")
        if status in (401, 403):
            raise SearchError(f"Brave rejected the API key (HTTP {status})")
        if status == 429:
            raise SearchError(
                "Brave rate limit hit twice in a row (HTTP 429); wait and retry"
            )
        raise SearchError(f"Brave returned HTTP {status}: {snippet}")


def _snippet(payload, limit=300) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    text = " ".join(str(text).split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _requests_send(url, headers, body, timeout):
    """POST *body* as JSON; return ``(status_code, parsed JSON or text)``."""
    import requests  # imported lazily so tests never need the network stack

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    except requests.RequestException as exc:
        raise SearchError(f"request to Brave failed: {exc}") from exc
    try:
        payload = resp.json()
    except ValueError:
        payload = resp.text
    return resp.status_code, payload


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(response, cap=12000) -> str:
    """Turn an LLM Context response into compact plain text for a model.

    Plain text rather than JSON: Brave says a snippet may itself be
    JSON-serialised (a table, a schema, a code block), and wrapping it in
    JSON again would escape every quote and inflate the token count.  Each
    source becomes a numbered header line with its date, the URL, then its
    snippets.  An empty ``grounding.generic`` is Brave's way of saying
    nothing relevant was found, so it renders as an explicit message the
    search agent can react to by reformulating.
    """
    if not isinstance(response, dict):
        return "No relevant results."
    sources = response.get("sources") or {}
    grounding = response.get("grounding") or {}
    lines = []
    for i, item in enumerate(grounding.get("generic") or [], start=1):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        meta = sources.get(url) if isinstance(sources, dict) else None
        meta = meta if isinstance(meta, dict) else {}
        # age is [full date, YYYY-MM-DD, relative, ISO timestamp]; the
        # first three positions predate the fourth, so index 1 is stable
        # under either extraction pipeline.  Empty when the date is unknown.
        age = meta.get("age") or []
        when = age[1] if isinstance(age, list) and len(age) > 1 else ""
        host = meta.get("hostname") or ""
        label = ", ".join(part for part in (host, when) if part)
        title = item.get("title") or meta.get("title") or url
        lines.append(f"[{i}] {title}" + (f" ({label})" if label else ""))
        lines.append(url)
        for snippet in item.get("snippets") or []:
            if not isinstance(snippet, str):
                snippet = json.dumps(snippet, ensure_ascii=False, default=str)
            snippet = snippet.strip()
            if snippet:
                lines.append(snippet)
        lines.append("")
    text = "\n".join(lines).strip()
    if not text:
        return "No relevant results."
    if len(text) > cap:
        text = text[:cap].rstrip() + "\n[... truncated]"
    return text
