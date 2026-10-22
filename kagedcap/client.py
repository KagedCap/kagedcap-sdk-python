"""KagedCap client. Uses only the Python standard library (no dependencies)."""

from __future__ import annotations

import email.utils
import http.client
import json
import math
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

DEFAULT_BASE_URL = "https://api.kagedcap.io"

# Sent on solves that don't specify a user_agent. The gateway never fills one in, but the
# solver fleet runs this exact Chrome desktop profile — matching it keeps the UA baked into
# the token in agreement with the identity the solver presented. Bump the Chrome version here.
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"

# Seconds between polls of GET /v2/solve/{id}. The gateway answers "running" until the solve
# lands, so polling tighter than this buys nothing but requests.
DEFAULT_POLL_INTERVAL = 5.0

# Whole budget, in seconds, for a solve's submit plus every poll that follows it.
DEFAULT_DEADLINE = 120.0

# Floor on one poll's transport timeout. The poll that runs as the budget expires is the one
# most likely to be carrying the finished solve, so it gets a usable timeout instead of the
# zero seconds the deadline has left — this is also the most a solve can overrun its deadline.
_MIN_POLL_TIMEOUT = 5.0

# Retry defaults (2026-10-20). Railway's edge in front of the gateway sheds load with a bare
# 429 — body "rate limited", not our JSON envelope — before the request ever reaches the API,
# at hundreds of thousands of submits an hour; the API itself answers 429/5xx when it is
# briefly over capacity. Those are moments, not verdicts: the same request succeeds a second
# later, so the SDK keeps sending it until the caller's deadline instead of handing the moment
# back as a failed solve. Backoff starts at 250ms, doubles, caps at 5s, and is fully jittered;
# a Retry-After header wins over the backoff but is never honoured past 10s.
DEFAULT_RETRY_INITIAL_BACKOFF = 0.25
DEFAULT_RETRY_MAX_BACKOFF = 5.0
DEFAULT_RETRY_MAX_RETRY_AFTER = 10.0

# No wait between attempts is ever shorter than this — not for a Retry-After of 0, not for a
# policy configured with a 0 backoff. Below it a retry is a tight loop against an edge that is
# already shedding, and the jitter has no room left to spread a fleet out.
_MIN_BACKOFF = 0.05

# An attempt needs at least one round trip. With less of the deadline than this left, a retry
# would only time out client-side and replace the gateway's real answer with "timed out", so
# none is started and the previous answer is the verdict. Also the floor on the transport
# timeout of a submit or sync solve, which is otherwise the time the deadline has left.
_MIN_ATTEMPT_BUDGET = 0.5

# Longest Retry-After the SDK reports on an error, in seconds (24h). The wait it actually
# takes is capped separately, by RetryPolicy.max_retry_after.
_MAX_RETRY_AFTER_SECONDS = 86400.0

# The synchronous /solve is retried only inside this many seconds of its first attempt. The
# gateway's claim on the Idempotency-Key is SET NX EX 120: it lasts 120s from the first
# attempt the gateway saw, and a resend after it has lapsed is not deduplicated — it runs,
# and bills, a fresh solve. 100s leaves a retry 20s to reach the gateway inside the claim.
_SYNC_RETRY_WINDOW = 100.0

# Statuses that mean "not now" rather than "not this": the edge or the gateway shedding load
# (429), or a hop that failed before a verdict existed (500/502/503/504).
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})

# The statuses the edge in front of the gateway (Railway, Cloudflare) answers with itself —
# a page or a bare line, not our envelope — when the request never reached the API.
_EDGE_STATUSES = frozenset({429, 502, 503, 504})

# Codes that make the request itself wrong. Sending the same request again can only earn the
# same answer, whichever status carried it, so these are never retried.
_TERMINAL_CODES = frozenset({
    "validation_error",
    "unauthorized",
    "key_revoked",
    "account_suspended",
    "insufficient_funds",
    "key_spend_cap_reached",
    "proxy_invalid",
    "proxy_required",
    "proxy_not_allowed",
    "unsupported_task",
    "action_required",
    "callback_url_invalid",
    "not_found",
    "idempotency_conflict",
    "key_frozen",
    "subscription_quota_exhausted",
    "subscription_past_due",
    "host_not_allowed",
})

# Guards the 2 ** attempt below: past this many doublings the cap has long since taken over,
# and a float can't hold the raw power anyway.
_MAX_BACKOFF_DOUBLINGS = 30

TASKS = [
    "ReCaptchaV3Task",
    "ReCaptchaV3TaskProxyLess",
    "ReCaptchaV3EnterpriseTask",
    "ReCaptchaV3EnterpriseTaskProxyLess",
    "ReCaptchaV2Task",
    "ReCaptchaV2TaskProxyLess",
    "TicketmasterTmptTask",
    "KasadaLogin",
    "KasadaReload",
    "EvaluateTask",
]


class KagedCapError(Exception):
    """Raised on any non-2xx response or transport failure, and on a solve that comes back
    failed, expired, or outlives its deadline. ``status`` is 0 when there was no HTTP status
    to report; ``request_id`` is set only when the gateway sent one.

    ``attempts`` is how many times the request was sent before this error was raised (``None``
    for errors that never went on the wire), ``idempotency_key`` the key those sends carried —
    quote it at support, since the gateway may hold a job under it even though the SDK gave
    up — and ``retry_after`` the parsed ``Retry-After`` header of the last response, in
    seconds (never negative, capped at 24 hours), when it sent one."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        request_id: Optional[str] = None,
        *,
        attempts: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id
        self.attempts = attempts
        self.idempotency_key = idempotency_key
        self.retry_after = retry_after
        # Whether the response body was the gateway's own JSON envelope (it names its error),
        # as opposed to a page the edge in front of it made. Set by _send; read by the retry
        # classification, which treats the two differently on the synchronous /solve.
        self._from_api = False


@dataclass(frozen=True)
class RetryPolicy:
    """How the client retries transient failures — HTTP 429/500/502/503/504 and transport
    errors — on the ``/v2/solve`` submit and the ``/v2/solve/{id}`` poll, and the narrower set
    (transport errors and the edge's own non-JSON 429/502/503/504) on the synchronous
    ``/solve``, which every call sends under an ``Idempotency-Key`` and retries only inside
    100s of its first attempt. The defaults are the policy described on
    :data:`DEFAULT_RETRY_INITIAL_BACKOFF`; pass ``RetryPolicy(enabled=False)`` to the client
    to get the pre-2026-10-20 behaviour of failing on the first such response.

    ``max_attempts`` is ``None`` for "until the caller's deadline", which is the default: the
    deadline is the caller's budget, and a retry cap on top of it only trades a solve that
    would have landed for an error. A zero or negative ``initial_backoff`` / ``max_backoff``
    is replaced by the default, and no wait is ever shorter than 50 ms."""

    enabled: bool = True
    max_attempts: Optional[int] = None
    initial_backoff: float = DEFAULT_RETRY_INITIAL_BACKOFF
    max_backoff: float = DEFAULT_RETRY_MAX_BACKOFF
    max_retry_after: float = DEFAULT_RETRY_MAX_RETRY_AFTER

    def __post_init__(self) -> None:
        if self.max_attempts is not None and self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1, or None for unlimited")
        # A zero or negative backoff would be a tight loop against an edge that is already
        # shedding; it means the default instead, and the 50 ms floor applies to every wait.
        if not _is_positive_finite(self.initial_backoff):
            object.__setattr__(self, "initial_backoff", DEFAULT_RETRY_INITIAL_BACKOFF)
        if not _is_positive_finite(self.max_backoff):
            object.__setattr__(self, "max_backoff", DEFAULT_RETRY_MAX_BACKOFF)
        if not self.max_retry_after >= 0:  # also rejects NaN
            raise ValueError("max_retry_after must not be negative")

    def delay(self, attempt: int, retry_after: Optional[float] = None) -> float:
        """Seconds to wait before sending attempt ``attempt + 1``; never less than 50 ms. A
        server-supplied ``retry_after`` (seconds) wins when it is usable — positive, with a
        positive ``max_retry_after`` to cap it — otherwise exponential backoff from
        ``initial_backoff`` capped at ``max_backoff``, with full jitter — a random point in
        ``[50 ms, backoff]`` — so a fleet of clients shed by the edge at the same moment
        doesn't come back as one synchronized wave."""
        if retry_after is not None and retry_after > 0 and self.max_retry_after > 0:
            return max(_MIN_BACKOFF, min(retry_after, self.max_retry_after))
        doublings = min(max(attempt - 1, 0), _MAX_BACKOFF_DOUBLINGS)
        ceiling = max(_MIN_BACKOFF, min(self.initial_backoff * (2 ** doublings), self.max_backoff))
        return random.uniform(_MIN_BACKOFF, ceiling)


def _is_positive_finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def is_recaptcha_task(task: str) -> bool:
    """Whether ``task`` is a reCAPTCHA one. Kept for callers that branch on the family; it is no
    longer a routing gate — the reCAPTCHA, tmpt, and evaluate fleets run over the async endpoint,
    because the job row carries each fleet's full result rather than only a token. Kasada is the
    exception: it runs over the synchronous ``/solve`` endpoint."""
    return isinstance(task, str) and task.startswith("ReCaptcha")


def _is_kasada(task: str) -> bool:
    return isinstance(task, str) and task.startswith("Kasada")


def _is_evaluate(task: str) -> bool:
    return task == "EvaluateTask"


def derive_task(enterprise: bool, has_proxy: bool, version: str = "v3") -> str:
    suffix = "Task" if has_proxy else "TaskProxyLess"
    if version == "v2":
        return "ReCaptchaV2" + suffix  # no enterprise variant for v2 yet
    base = "ReCaptchaV3Enterprise" if enterprise else "ReCaptchaV3"
    return base + suffix


def _is_transient(err: KagedCapError, *, sync_solve: bool) -> bool:
    """Whether ``err`` is a moment worth waiting out rather than a verdict on the request."""
    if err.status == 0 and err.code == "network_error":
        return True  # refused, reset, EOF, timeout, DNS: the request may never have arrived
    if err.status not in _TRANSIENT_STATUSES or err.code in _TERMINAL_CODES:
        return False
    if sync_solve:
        # The synchronous /solve runs the solve inside the request, and the gateway's very
        # first step is to claim the Idempotency-Key (SET NX, 120s). So a transient answer the
        # gateway itself gave — our JSON envelope, 429 or 5xx — means the key is already taken,
        # and resending under it can only earn 409 idempotency_conflict: the original error is
        # the honest outcome. Only an answer the edge made on its own (Railway's / Cloudflare's
        # 429/502/503/504 page), which never reached the gateway, is worth resending. On
        # /v2/solve the submit only queues the job, and a resend under the key hands the
        # original job back, so every transient status is retried there. (2026-10-20)
        return err.status in _EDGE_STATUSES and not err._from_api
    return True


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """``Retry-After`` as seconds from now, from either of its wire forms (delta-seconds or an
    HTTP-date), clamped to ``[0, 24h]`` before anything is computed from it: a negative number
    or a date in the past is 0; NaN, or a date outside the years 1970-9999, is unusable.
    ``None`` when absent or unparsable, so the caller falls back to its backoff."""
    if not value:
        return None
    text = value.strip()
    try:
        return _clamp_retry_after(float(text))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None or not 1970 <= when.year <= 9999:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    try:
        return _clamp_retry_after((when - datetime.now(timezone.utc)).total_seconds())
    except (OverflowError, ValueError):
        return None


def _clamp_retry_after(seconds: float) -> Optional[float]:
    if math.isnan(seconds):
        return None
    return min(max(0.0, seconds), _MAX_RETRY_AFTER_SECONDS)


def _decode_envelope(raw: bytes) -> Optional[dict[str, Any]]:
    """The body as the gateway's JSON envelope, or ``None`` when it is not a JSON object —
    a page from the edge, a bare string, ``null``, a list. Anything that is not an object is
    an empty envelope to the callers: it carries no code, message or id of ours."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:  # JSONDecodeError and UnicodeDecodeError both are
        return None
    return payload if isinstance(payload, dict) else None


class KagedCapClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 120.0,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        retry_policy: Optional[RetryPolicy] = None,
    ) -> None:
        """``timeout`` is the transport timeout of one request, and the retry budget of a call
        that has no ``deadline`` of its own (:meth:`submit_solve`, :meth:`get_solve`, the
        synchronous Kasada methods). ``retry_policy`` defaults to :class:`RetryPolicy` — retry
        transient failures until the budget runs out — and ``RetryPolicy(enabled=False)``
        turns retries off."""
        if not api_key:
            raise ValueError("api_key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.retry_policy = retry_policy if retry_policy is not None else RetryPolicy()

    def solve(
        self,
        sitekey: str,
        url: str,
        action: Optional[str] = None,
        *,
        task: Optional[str] = None,
        version: str = "v3",
        enterprise: bool = False,
        proxy: Optional[str] = None,
        user_agent: Optional[str] = None,
        device: Optional[str] = None,
        enhanced: Optional[bool] = None,
        secret_key: Optional[str] = None,
        callback_url: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        deadline: float = DEFAULT_DEADLINE,
        poll_interval: Optional[float] = None,
    ) -> dict[str, Any]:
        """Solve a reCAPTCHA, tmpt, or evaluate captcha. Pass ``enterprise`` and optionally
        ``proxy`` to auto-select the v3 task, or ``version="v2"`` for reCAPTCHA v2 invisible, or
        set ``task`` explicitly. ``action`` is required for v3 and ignored for v2. ``user_agent``
        defaults to :data:`DEFAULT_USER_AGENT`.

        For Kasada use :meth:`kasada_login` / :meth:`kasada_reload` — a Kasada result has no
        token for this method to return, so a Kasada task is rejected here.

        Blocks until the solve lands, but no longer holds a connection open for the whole
        solve: the task is queued with :meth:`submit_solve`, then polled every ``poll_interval``
        seconds — the client's, 5s by default — until it finishes. ``deadline`` is the whole
        budget in seconds for the submit and every poll that follows it, retries included: a
        submit or poll that is rate limited (429), meets a 5xx, or fails in transport is sent
        again with backoff until the deadline, per the client's :class:`RetryPolicy`. Outliving
        the deadline raises :class:`KagedCapError` with code ``solve_timeout``, or, when the
        last thing the gateway said was a transient failure, with that failure's status and
        code and a message naming the attempt count; a solve the fleet gives up on raises that
        failure's own code; a result collected too late to still hold its payload raises
        ``result_expired``. ``callback_url`` and ``idempotency_key`` are handed to
        :meth:`submit_solve`, which generates a key when none is given.

        Returns the finished ``/v2/solve/{id}`` body — the solved fields (``token``, plus
        ``decision`` for evaluate) are always present, ``solve_ms`` / ``elapsed_ms`` may not be."""
        resolved_task = task or derive_task(enterprise, proxy is not None, version)
        if _is_kasada(resolved_task):
            raise KagedCapError(
                0,
                "validation_error",
                "solve() cannot carry a Kasada result (it has no token); use kasada_login() or kasada_reload()",
            )
        if deadline <= 0:
            raise ValueError("deadline must be greater than 0 seconds")
        interval = self.poll_interval if poll_interval is None else poll_interval
        if interval <= 0:
            raise ValueError("poll_interval must be greater than 0 seconds")

        # Started before the submit, because the deadline is the caller's whole budget.
        expires_at = time.monotonic() + deadline
        body = self._solve_body(
            resolved_task, sitekey, url, action, proxy, user_agent, device, enhanced, secret_key, callback_url
        )
        submitted = self._submit_body(body, idempotency_key=idempotency_key, expires_at=expires_at)
        # evaluate's "expired" test is the decision, not the token: a challenge verdict
        # legitimately carries an empty token.
        result_key = "decision" if _is_evaluate(resolved_task) else "token"
        return self._poll_solve(submitted["id"], expires_at, interval, result_key)

    def submit_solve(
        self,
        sitekey: str,
        url: str,
        action: Optional[str] = None,
        *,
        task: Optional[str] = None,
        version: str = "v3",
        enterprise: bool = False,
        proxy: Optional[str] = None,
        user_agent: Optional[str] = None,
        device: Optional[str] = None,
        enhanced: Optional[bool] = None,
        secret_key: Optional[str] = None,
        callback_url: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """Queue a reCAPTCHA, tmpt, or evaluate solve on ``/v2/solve`` and return as soon as the
        gateway has accepted it: ``{"success": True, "id": ..., "status": "running"}``. Same
        arguments as :meth:`solve`, plus:

        ``callback_url`` — an https URL the gateway POSTs the finished solve to. It has to be
        publicly resolvable, and is rejected up front with ``callback_url_invalid`` if not.

        ``idempotency_key`` — an opaque string sent as the ``Idempotency-Key`` header. Retry
        a submit under the same key and you get the original solve back instead of a second
        one on your bill. The key is durable and holds across shards. Leave it unset and the
        SDK generates a UUIDv4 for this call, so its own retries of the submit are safe too;
        the key is on ``KagedCapError.idempotency_key`` should the submit fail.

        ``timeout`` — the transport timeout of one attempt and the budget for retrying it;
        defaults to the client's ``timeout``.

        Pair this with :meth:`get_solve` to run the wait yourself — or to not wait at all,
        when a ``callback_url`` is doing the telling. :meth:`solve` is submit plus poll. Kasada
        is submitted through :meth:`kasada_login` / :meth:`kasada_reload`, which take its
        differently shaped inputs."""
        resolved_task = task or derive_task(enterprise, proxy is not None, version)
        if _is_kasada(resolved_task):
            raise KagedCapError(
                0,
                "validation_error",
                f"submit_solve: {resolved_task} takes Kasada-shaped inputs; use kasada_login() or kasada_reload()",
            )
        body = self._solve_body(
            resolved_task, sitekey, url, action, proxy, user_agent, device, enhanced, secret_key, callback_url
        )
        return self._submit_body(body, timeout=timeout, idempotency_key=idempotency_key)

    def get_solve(self, solve_id: str, *, timeout: Optional[float] = None) -> dict[str, Any]:
        """Fetch a queued solve's current state from ``/v2/solve/{id}``, returned as it came.

        Read ``status``: ``"running"`` (keep waiting), ``"done"`` (the result fields are there),
        or ``"failed"`` (stop, and read ``error``). Not ``success``, which is true only once a
        solve is done and so says nothing about one still running. Which result fields are
        present depends on the task — ``token`` (+ ``score`` / ``verification``) for reCAPTCHA,
        ``decision`` for evaluate, ``headers`` + ``x_kpsdk_*`` for Kasada. ``solve_ms`` /
        ``elapsed_ms`` are best-effort and may be absent.

        A finished solve stops carrying its result about five minutes later — a reCAPTCHA token
        is dead well before that anyway — so a late poll answers ``done`` with the result fields
        cleared. The high-level methods turn that into a ``result_expired`` error; here you see it
        raw.

        Raises :class:`KagedCapError` with code ``not_found`` (404) for an id the gateway
        doesn't have. An id belonging to another account answers exactly the same way. A poll
        that is rate limited or meets a 5xx is retried within ``timeout`` (the client's by
        default) per the client's :class:`RetryPolicy`."""
        budget = self.timeout if timeout is None else timeout
        expires_at = time.monotonic() + budget
        return self._get_solve(solve_id, expires_at, lambda: max(_MIN_ATTEMPT_BUDGET, expires_at - time.monotonic()))

    def solve_deprecated(
        self,
        sitekey: str,
        url: str,
        action: Optional[str] = None,
        *,
        task: Optional[str] = None,
        version: str = "v3",
        enterprise: bool = False,
        proxy: Optional[str] = None,
        user_agent: Optional[str] = None,
        device: Optional[str] = None,
        enhanced: Optional[bool] = None,
        secret_key: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Deprecated. The legacy synchronous solve: one POST to ``/solve`` that holds the
        connection open for however long the solve takes and returns the finished JSON.
        Arguments and behaviour are unchanged — prefer :meth:`solve`, which queues the task
        and polls for it instead of betting the whole solve on one long-lived connection.
        ``idempotency_key`` and the retries are as on :meth:`kasada_login`."""
        warnings.warn(
            "solve_deprecated() is deprecated; use solve(), which submits to /v2/solve and polls",
            DeprecationWarning,
            stacklevel=2,
        )
        resolved_task = task or derive_task(enterprise, proxy is not None, version)
        body = self._solve_body(resolved_task, sitekey, url, action, proxy, user_agent, device, enhanced, secret_key, None)
        return self._sync_solve(body, idempotency_key)

    def kasada_login(
        self,
        *,
        proxy: str,
        site: Optional[str] = None,
        url: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Start a Kasada session (KasadaLogin) over the synchronous ``/solve`` endpoint.
        ``proxy`` is required — the token is IP-bound, so reuse it on the target request. One
        POST holds the connection open until the session lands and returns the full result
        directly: ``success``, ``site``, ``headers`` + ``x_kpsdk_ct/cd/v/h``, ``kpsdk_st``,
        ``hash``, ``reload``, and ``user_agent``. Keep the result and pass it to
        :meth:`kasada_reload` to refresh the session later.

        Every call goes out under an ``Idempotency-Key`` — ``idempotency_key``, or a UUIDv4 the
        SDK mints for this call — and is resent under that same key only through a failure
        that never reached the gateway: a transport error, or a 429/502/503/504 page the edge
        in front of the API made itself. The synchronous endpoint runs the solve inside the
        request, so a bare repeat could run, and bill, it twice; under the key the gateway
        refuses a duplicate with ``409 idempotency_conflict`` instead. A 429 or 5xx the
        gateway answered (its JSON envelope) is raised as is — the gateway claims the key
        before anything else, so a repeat under it could only earn that 409. If a retry does
        meet the 409, the error raised is the last transient failure, not the 409. No retry
        starts more than 100s after the first attempt was sent: the gateway holds the key for
        120s, and a later resend would run a fresh solve instead of being deduplicated — the
        error is then the last real response, with its attempt count and elapsed time. The
        key is on ``KagedCapError.idempotency_key`` should the call fail."""
        body = {"task": "KasadaLogin", "site": site, "url": url, "proxy": proxy}
        return self._sync_solve(body, idempotency_key)

    def kasada_login_deprecated(
        self, *, proxy: str, site: Optional[str] = None, url: Optional[str] = None
    ) -> dict[str, Any]:
        """Deprecated. Alias for :meth:`kasada_login`, which uses the synchronous ``/solve``
        endpoint itself now. Prefer :meth:`kasada_login`."""
        warnings.warn(
            "kasada_login_deprecated() is deprecated; use kasada_login()",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._sync_solve({"task": "KasadaLogin", "site": site, "url": url, "proxy": proxy})

    def kasada_reload(
        self,
        session: Optional[dict[str, Any]] = None,
        *,
        kpsdk_st: Optional[int] = None,
        hash: Optional[str] = None,
        x_kpsdk_ct: Optional[str] = None,
        x_kpsdk_v: Optional[str] = None,
        x_kpsdk_h: Optional[str] = None,
        site: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Refresh a Kasada session (no proxy needed) over the synchronous ``/solve`` endpoint.
        Pass the :meth:`kasada_login` result as ``session`` and its ``kpsdk_st`` + ``hash`` +
        ``x_kpsdk_*`` are resent for you, or supply them explicitly. ``hash`` and ``x_kpsdk_ct``
        are required — the cd's proof-of-work seed embeds both. One POST returns the refreshed
        result directly. ``idempotency_key`` and the retries are as on :meth:`kasada_login`."""
        body = self._kasada_reload_body(session, kpsdk_st, hash, x_kpsdk_ct, x_kpsdk_v, x_kpsdk_h, site)
        return self._sync_solve(body, idempotency_key)

    def kasada_reload_deprecated(
        self,
        session: Optional[dict[str, Any]] = None,
        *,
        kpsdk_st: Optional[int] = None,
        hash: Optional[str] = None,
        x_kpsdk_ct: Optional[str] = None,
        x_kpsdk_v: Optional[str] = None,
        x_kpsdk_h: Optional[str] = None,
        site: Optional[str] = None,
    ) -> dict[str, Any]:
        """Deprecated. Alias for :meth:`kasada_reload`, which uses the synchronous ``/solve``
        endpoint itself now. Prefer :meth:`kasada_reload`."""
        warnings.warn(
            "kasada_reload_deprecated() is deprecated; use kasada_reload()",
            DeprecationWarning,
            stacklevel=2,
        )
        body = self._kasada_reload_body(session, kpsdk_st, hash, x_kpsdk_ct, x_kpsdk_v, x_kpsdk_h, site)
        return self._sync_solve(body)

    def evaluate(
        self,
        url: str,
        proxy: str,
        *,
        action: Optional[str] = None,
        phone_number: Optional[str] = None,
        queue_id: Optional[str] = None,
        event_id: Optional[str] = None,
        user_agent: Optional[str] = None,
        deadline: float = DEFAULT_DEADLINE,
        poll_interval: Optional[float] = None,
    ) -> dict[str, Any]:
        """Run a Ticketmaster EPSF evaluation (EvaluateTask). ``url`` must be a Ticketmaster
        host and ``proxy`` is required — the decision is scored against the exit IP. Pass
        ``phone_number`` (country prefix included) for verify_phone, or ``queue_id`` /
        ``event_id`` for join_queue. ``user_agent`` picks the device profile the solver runs —
        screen, GPU and client hints all move with it — and defaults to
        :data:`DEFAULT_USER_AGENT` so evaluates match your solves. Submits to ``/v2/solve`` and
        polls; returns a dict with ``token``, the EPSF allow token to replay on the next APS
        step, and ``decision`` (``allow`` | ``challenge`` | ``block``)."""
        return self._solve_async(self._evaluate_body(url, proxy, action, phone_number, queue_id, event_id, user_agent), deadline, poll_interval, "decision")

    def evaluate_deprecated(
        self,
        url: str,
        proxy: str,
        *,
        action: Optional[str] = None,
        phone_number: Optional[str] = None,
        queue_id: Optional[str] = None,
        event_id: Optional[str] = None,
        user_agent: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Deprecated. Run evaluate over the legacy synchronous ``/solve``. Prefer
        :meth:`evaluate`. ``idempotency_key`` and the retries are as on :meth:`kasada_login`."""
        warnings.warn(
            "evaluate_deprecated() is deprecated; use evaluate(), which submits to /v2/solve and polls",
            DeprecationWarning,
            stacklevel=2,
        )
        body = self._evaluate_body(url, proxy, action, phone_number, queue_id, event_id, user_agent)
        return self._sync_solve(body, idempotency_key)

    def check_balance(self) -> dict[str, Any]:
        """Current balance for the API key's account."""
        return self._request("GET", "/v1/balance")

    @staticmethod
    def _solve_body(task, sitekey, url, action, proxy, user_agent, device, enhanced, secret_key, callback_url) -> dict[str, Any]:
        return {
            "task": task,
            "url": url,
            "sitekey": sitekey,
            "action": action,
            "proxy": proxy,
            "userAgent": user_agent or DEFAULT_USER_AGENT,
            "device": device,
            "enhanced": enhanced,
            "secretKey": secret_key,
            "callback_url": callback_url,
        }

    @staticmethod
    def _kasada_reload_body(session, kpsdk_st, hash, x_kpsdk_ct, x_kpsdk_v, x_kpsdk_h, site) -> dict[str, Any]:
        if session:
            kpsdk_st = kpsdk_st if kpsdk_st is not None else session.get("kpsdk_st")
            hash = hash if hash is not None else session.get("hash")
            x_kpsdk_ct = x_kpsdk_ct if x_kpsdk_ct is not None else session.get("x_kpsdk_ct")
            x_kpsdk_v = x_kpsdk_v if x_kpsdk_v is not None else session.get("x_kpsdk_v")
            x_kpsdk_h = x_kpsdk_h if x_kpsdk_h is not None else session.get("x_kpsdk_h")
            site = site if site is not None else session.get("site")
        if kpsdk_st is None:
            raise ValueError("kasada_reload: kpsdk_st is required (pass the kasada_login result or kpsdk_st)")
        return {
            "task": "KasadaReload",
            "site": site,
            "kpsdk_st": kpsdk_st,
            "hash": hash,
            "x_kpsdk_ct": x_kpsdk_ct,
            "x_kpsdk_v": x_kpsdk_v,
            "x_kpsdk_h": x_kpsdk_h,
        }

    @staticmethod
    def _evaluate_body(url, proxy, action, phone_number, queue_id, event_id, user_agent) -> dict[str, Any]:
        return {
            "task": "EvaluateTask",
            "url": url,
            "proxy": proxy,
            # Deliberately not defaulted: the gateway derives the action from the host
            # (auth.* → verify_phone, any other Ticketmaster host → join_queue), and sending
            # a guess would silently override a default that is right more often than we are.
            "action": action,
            "phone_number": phone_number,
            "queueId": queue_id,  # camelCase on the wire — a snake_case key is dropped in silence
            "eventId": event_id,
            "userAgent": user_agent or DEFAULT_USER_AGENT,
        }

    def _solve_async(self, body: dict[str, Any], deadline: float, poll_interval: Optional[float], result_key: str) -> dict[str, Any]:
        """Submit a prepared body to ``/v2/solve`` and poll until it is done. ``result_key`` is
        the field whose absence on a ``done`` job means the result was swept (``token`` for
        reCAPTCHA/tmpt, ``decision`` for evaluate)."""
        if deadline <= 0:
            raise ValueError("deadline must be greater than 0 seconds")
        interval = self.poll_interval if poll_interval is None else poll_interval
        if interval <= 0:
            raise ValueError("poll_interval must be greater than 0 seconds")
        expires_at = time.monotonic() + deadline
        submitted = self._submit_body(body, expires_at=expires_at)
        return self._poll_solve(submitted["id"], expires_at, interval, result_key)

    def _submit_body(
        self,
        body: dict[str, Any],
        *,
        timeout: Optional[float] = None,
        idempotency_key: Optional[str] = None,
        expires_at: Optional[float] = None,
    ) -> dict[str, Any]:
        """POST a prepared body to ``/v2/solve`` and return the acceptance (the id to poll).
        Retried through transient failures until ``expires_at`` — or, when the caller has no
        deadline, for ``timeout`` (the client's by default)."""
        # Every submit goes out under an Idempotency-Key (2026-10-20). A retry is only safe if
        # the attempt it repeats cannot have counted, and a 502 from the edge may well arrive
        # after the gateway queued the job: resent bare, that job would be queued — and billed
        # — a second time. Under the same key the gateway hands the original job back instead.
        key = idempotency_key or str(uuid.uuid4())
        if expires_at is None:
            expires_at = time.monotonic() + (self.timeout if timeout is None else timeout)
        status, payload = self._send_retrying(
            "POST",
            "/v2/solve",
            body,
            headers={"Idempotency-Key": key},
            expires_at=expires_at,
            attempt_timeout=lambda: max(_MIN_ATTEMPT_BUDGET, expires_at - time.monotonic()),
            what="POST /v2/solve",
            idempotency_key=key,
        )
        # 202 is the only acceptance the gateway sends. Another 2xx carrying an error envelope
        # is something in front of the API answering for it — a failure urlopen won't raise on,
        # so catch it here rather than polling for an id that was never queued.
        if status != 202 or payload.get("success") is False:
            raise KagedCapError(
                status,
                payload.get("error") or "error",
                payload.get("message") or f"solve: unexpected HTTP {status} from /v2/solve",
                payload.get("request_id"),
                idempotency_key=key,
            )
        if not payload.get("id"):
            raise KagedCapError(
                status,
                "error",
                "solve: /v2/solve accepted the task but returned no id",
                payload.get("request_id"),
                idempotency_key=key,
            )
        return payload

    def _get_solve(self, solve_id: str, expires_at: float, attempt_timeout: Callable[[], Optional[float]]) -> dict[str, Any]:
        """One read of ``/v2/solve/{id}``, retried through transient failures until ``expires_at``."""
        path = "/v2/solve/" + urllib.parse.quote(solve_id, safe="")
        return self._send_retrying("GET", path, expires_at=expires_at, attempt_timeout=attempt_timeout, what="GET " + path)[1]

    def _poll_solve(self, solve_id: str, expires_at: float, interval: float, result_key: str = "token") -> dict[str, Any]:
        """Poll a queued solve until it is done, has failed, or the deadline runs out."""
        while True:
            left = max(0.0, expires_at - time.monotonic())
            time.sleep(min(interval, left))
            # The deadline bounds the waiting, not the looking: a solve that landed during
            # that last sleep is still a result the caller paid for, so every sleep gets its
            # poll and the timeout is only declared afterwards. ``interval`` is the gap between
            # polls that answered; a poll that was shed or met a 5xx is retried on the policy's
            # backoff inside _get_solve, within the same deadline.
            result = self._get_solve(solve_id, expires_at, lambda: max(_MIN_POLL_TIMEOUT, expires_at - time.monotonic()))
            status = result.get("status")
            if status == "done":
                if not result.get(result_key):
                    # The gateway drops the result a few minutes after the solve completes (the
                    # token itself dies sooner). "done" with the result cleared means the caller
                    # came back too late, not that the solve succeeded at returning nothing.
                    raise KagedCapError(
                        0,
                        "result_expired",
                        f"solve {solve_id} finished but its result has already expired — solve again",
                    )
                return result
            if status == "failed":
                # A verdict, not a moment: the fleet ran the solve and gave up on it.
                code = result.get("error") or "solve_failed"
                raise KagedCapError(0, code, f"solve {solve_id} failed: {code}")
            # Anything else — "running", or a status this SDK predates — is still in flight.
            if time.monotonic() >= expires_at:
                raise KagedCapError(0, "solve_timeout", f"solve {solve_id} did not finish before the deadline")

    def _sync_solve(self, body: dict[str, Any], idempotency_key: Optional[str] = None) -> dict[str, Any]:
        """POST to the synchronous ``/solve``. Every call goes out under an ``Idempotency-Key``
        — the caller's, or a UUIDv4 minted once here, as :meth:`_submit_body` does — and is
        retried under that same key, but only through failures that never reached the gateway
        (2026-10-20): the sync endpoint runs the solve inside the request, so a dropped
        connection can hide a solve that already ran and was billed, and resending it bare
        would solve — and bill — again. Under the key the gateway refuses the duplicate
        instead. The retry budget is the client's ``timeout``, and no retry starts more than
        :data:`_SYNC_RETRY_WINDOW` seconds after the first attempt, when the gateway's claim
        on the key is about to lapse."""
        key = idempotency_key or str(uuid.uuid4())
        expires_at = time.monotonic() + self.timeout
        return self._send_retrying(
            "POST",
            "/solve",
            body,
            headers={"Idempotency-Key": key},
            expires_at=expires_at,
            attempt_timeout=lambda: max(_MIN_ATTEMPT_BUDGET, expires_at - time.monotonic()),
            what="POST /solve",
            sync_solve=True,
            idempotency_key=key,
        )[1]

    def _send_retrying(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        *,
        headers: Optional[dict[str, str]] = None,
        expires_at: float,
        attempt_timeout: Callable[[], Optional[float]],
        what: str,
        sync_solve: bool = False,
        idempotency_key: Optional[str] = None,
    ) -> tuple[int, dict[str, Any]]:
        """:meth:`_send`, repeated through transient failures until it answers, the policy's
        attempt cap is hit, the deadline ``expires_at`` has too little left for another
        attempt, or — on the synchronous ``/solve`` (``sync_solve``) — :data:`_SYNC_RETRY_WINDOW`
        has passed since the first attempt. ``attempt_timeout`` is asked for each attempt's
        transport timeout, so it can shrink with the deadline. The error raised when the budget
        runs out keeps the status, code and request id of the last real answer — a trailing
        attempt that the deadline cut short does not replace it — and its message says how
        many attempts were made and how long they took. A ``KeyboardInterrupt`` is not caught
        here or below: Ctrl-C ends the wait and propagates."""
        policy = self.retry_policy
        started = time.monotonic()
        attempts = 0
        last: Optional[KagedCapError] = None
        while True:
            attempts += 1
            try:
                return self._send(method, path, body, timeout=attempt_timeout(), headers=headers)
            except KagedCapError as e:
                e.attempts = attempts
                e.idempotency_key = idempotency_key
                if sync_solve and last is not None and e.status == 409 and e.code == "idempotency_conflict":
                    # The retry found the gateway already holding this key, so the attempt
                    # that failed had reached it after all; its transient error is the
                    # honest outcome, not the 409 that the resend was bound to get.
                    raise self._retries_exhausted(
                        what, last, attempts, started,
                        "the retry answered 409 idempotency_conflict, so the first attempt had reached the gateway",
                    ) from None
                if not policy.enabled or not _is_transient(e, sync_solve=sync_solve):
                    raise
                # A transport failure that ends with the deadline (nearly) gone is the deadline
                # cutting the attempt short, not the gateway speaking: the gateway's last real
                # answer, if there was one, stays the verdict.
                cut_short = e.status == 0 and expires_at - time.monotonic() < _MIN_ATTEMPT_BUDGET
                if last is None or not last.status or not cut_short:
                    last = e
            if policy.max_attempts is not None and attempts >= policy.max_attempts:
                raise self._retries_exhausted(what, last, attempts, started, f"attempt cap {policy.max_attempts} reached")
            # Sleep the full backoff (or Retry-After) — never shorter, so a shed edge is not
            # hammered — unless the deadline lands first, in which case the wait ends there.
            # On the sync /solve the retry window ends it the same way: no attempt follows it.
            stop_at = min(expires_at, started + _SYNC_RETRY_WINDOW) if sync_solve else expires_at
            delay = policy.delay(attempts, last.retry_after)
            time.sleep(max(0.0, min(delay, stop_at - time.monotonic())))
            if sync_solve:
                # The gateway holds the key for 120s from the first attempt it saw. A resend
                # after the claim has lapsed is not deduplicated — it runs, and bills, a fresh
                # solve — so past the window the last real answer is the verdict.
                elapsed = time.monotonic() - started
                if elapsed >= _SYNC_RETRY_WINDOW:
                    raise self._retries_exhausted(
                        what, last, attempts, started,
                        f"{elapsed:.0f}s since the first attempt, past the {_SYNC_RETRY_WINDOW:.0f}s window in which the gateway still deduplicates the key",
                    )
            # An attempt needs a round trip; with less than that left it would only time out
            # and bury the last real answer under "timed out", so none is started.
            left = expires_at - time.monotonic()
            if left < _MIN_ATTEMPT_BUDGET:
                why = "deadline reached" if left <= 0 else f"{left * 1000:.0f}ms of the deadline left, too little for another attempt"
                raise self._retries_exhausted(what, last, attempts, started, why)

    @staticmethod
    def _retries_exhausted(what: str, last: KagedCapError, attempts: int, started: float, why: str) -> KagedCapError:
        waited = time.monotonic() - started
        tail = f"HTTP {last.status} {last.code}: {last}" if last.status else f"{last.code}: {last}"
        return KagedCapError(
            last.status,
            last.code,
            f"{what}: gave up after {attempts} attempt{'s' if attempts != 1 else ''} over {waited:.1f}s ({why}); last failure: {tail}",
            last.request_id,
            attempts=attempts,
            idempotency_key=last.idempotency_key,
            retry_after=last.retry_after,
        )

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        return self._send(method, path, body, timeout=timeout, headers=headers)[1]

    def _send(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> tuple[int, dict[str, Any]]:
        """The one place that talks HTTP. Hands back the status alongside the decoded body,
        for the callers that care which 2xx they got."""
        data = None
        if body is not None:
            clean = {k: v for k, v in body.items() if v is not None}
            data = json.dumps(clean).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"x-api-key": self.api_key, "content-type": "application/json", **(headers or {})},
        )
        # A caller's deadline only ever shortens the client's transport timeout.
        timeout_s = self.timeout if timeout is None else min(self.timeout, timeout)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
            except (OSError, http.client.HTTPException):
                raw = b""
            # Not a JSON object — Railway's edge answers its 429 with a bare "rate limited",
            # and a quoted string or null is no more our envelope than that — is an empty one.
            payload = _decode_envelope(raw) or {}
            err = KagedCapError(
                e.code,
                payload.get("error") or "error",
                payload.get("message") or f"HTTP {e.code}",
                payload.get("request_id"),
                attempts=1,
                retry_after=_parse_retry_after(e.headers.get("Retry-After")),
            )
            err._from_api = payload.get("error") is not None
            raise err from None
        except urllib.error.URLError as e:
            raise KagedCapError(0, "network_error", str(getattr(e, "reason", e)), attempts=1) from None
        except (OSError, http.client.HTTPException) as e:
            # urlopen only wraps failures up to the request being sent; a read timeout, a reset
            # or an EOF while waiting for the response surfaces raw. Same transport failure.
            raise KagedCapError(0, "network_error", str(e) or type(e).__name__, attempts=1) from None
        payload = _decode_envelope(raw)
        if payload is None:
            # A 2xx carrying a page or a bare value is something in front of the API answering
            # for it. There is no id, status or token in it to act on; say so as a KagedCapError
            # rather than let the decode fail somewhere up the stack.
            raise KagedCapError(status, "error", f"HTTP {status}: response was not a JSON object", attempts=1)
        return status, payload
