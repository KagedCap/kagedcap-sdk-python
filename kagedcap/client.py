"""KagedCap client. Uses only the Python standard library (no dependencies)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from typing import Any, Optional

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
    to report; ``request_id`` is set only when the gateway sent one."""

    def __init__(self, status: int, code: str, message: str, request_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id


def derive_task(enterprise: bool, has_proxy: bool, version: str = "v3") -> str:
    suffix = "Task" if has_proxy else "TaskProxyLess"
    if version == "v2":
        return "ReCaptchaV2" + suffix  # no enterprise variant for v2 yet
    base = "ReCaptchaV3Enterprise" if enterprise else "ReCaptchaV3"
    return base + suffix


class KagedCapClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 120.0,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.poll_interval = poll_interval

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
        """Solve a captcha. Pass ``enterprise`` and optionally ``proxy`` to auto-select
        the v3 task, or ``version="v2"`` for reCAPTCHA v2 invisible, or set ``task``
        explicitly. ``action`` is required for v3 and ignored for v2. ``user_agent`` defaults
        to :data:`DEFAULT_USER_AGENT`.

        Blocks until the solve lands, but no longer holds a connection open for the whole
        solve: the task is queued with :meth:`submit_solve`, then polled every
        ``poll_interval`` seconds — the client's, 5s by default — until it finishes.
        ``deadline`` is the whole budget in seconds for the submit and every poll that
        follows it. Outliving it raises :class:`KagedCapError` with code ``solve_timeout``;
        a solve the fleet gives up on raises that failure's own code; a result collected too
        late to still hold a token raises ``result_expired``. ``callback_url`` and
        ``idempotency_key`` are handed to :meth:`submit_solve`.

        Returns the finished ``/v2/solve/{id}`` body — ``token`` is always there,
        ``solve_ms`` / ``elapsed_ms`` may not be."""
        if deadline <= 0:
            raise ValueError("deadline must be greater than 0 seconds")
        interval = self.poll_interval if poll_interval is None else poll_interval
        if interval <= 0:
            raise ValueError("poll_interval must be greater than 0 seconds")

        # Started before the submit, because the deadline is the caller's whole budget.
        expires_at = time.monotonic() + deadline
        submitted = self.submit_solve(
            sitekey,
            url,
            action,
            task=task,
            version=version,
            enterprise=enterprise,
            proxy=proxy,
            user_agent=user_agent,
            device=device,
            enhanced=enhanced,
            secret_key=secret_key,
            callback_url=callback_url,
            idempotency_key=idempotency_key,
            timeout=deadline,
        )
        return self._poll_solve(submitted["id"], expires_at, interval)

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
        """Queue a solve on ``/v2/solve`` and return as soon as the gateway has accepted it:
        ``{"success": True, "id": ..., "status": "running"}``. Same arguments as
        :meth:`solve`, plus:

        ``callback_url`` — an https URL the gateway POSTs the finished solve to. It has to be
        publicly resolvable, and is rejected up front with ``callback_url_invalid`` if not.

        ``idempotency_key`` — an opaque string sent as the ``Idempotency-Key`` header. Retry
        a submit under the same key and you get the original solve back instead of a second
        one on your bill. The key is durable and holds across shards.

        Pair this with :meth:`get_solve` to run the wait yourself — or to not wait at all,
        when a ``callback_url`` is doing the telling. :meth:`solve` is submit plus poll."""
        resolved_task = task or derive_task(enterprise, proxy is not None, version)
        body = {
            "task": resolved_task,
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
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        status, payload = self._send("POST", "/v2/solve", body, timeout=timeout, headers=headers)
        # 202 is the only acceptance the gateway sends. Another 2xx carrying an error envelope
        # is something in front of the API answering for it — a failure urlopen won't raise on,
        # so catch it here rather than polling for an id that was never queued.
        if status != 202 or payload.get("success") is False:
            raise KagedCapError(
                status,
                payload.get("error") or "error",
                payload.get("message") or f"solve: unexpected HTTP {status} from /v2/solve",
                payload.get("request_id"),
            )
        if not payload.get("id"):
            raise KagedCapError(
                status,
                "error",
                "solve: /v2/solve accepted the task but returned no id",
                payload.get("request_id"),
            )
        return payload

    def get_solve(self, solve_id: str, *, timeout: Optional[float] = None) -> dict[str, Any]:
        """Fetch a queued solve's current state from ``/v2/solve/{id}``, returned as it came.

        Read ``status``: ``"running"`` (keep waiting), ``"done"`` (``token`` is there), or
        ``"failed"`` (stop, and read ``error``). Not ``success``, which is true only once a
        solve is done and so says nothing about one still running. ``solve_ms`` /
        ``elapsed_ms`` are best-effort and may be absent.

        A finished solve stops carrying its token about five minutes later — a reCAPTCHA
        token is dead well before that anyway — so a late poll answers ``done`` with ``token``
        null. :meth:`solve` turns that into a ``result_expired`` error; here you see it raw.

        Raises :class:`KagedCapError` with code ``not_found`` (404) for an id the gateway
        doesn't have. An id belonging to another account answers exactly the same way."""
        return self._request("GET", "/v2/solve/" + urllib.parse.quote(solve_id, safe=""), timeout=timeout)

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
    ) -> dict[str, Any]:
        """Deprecated. The legacy synchronous solve: one POST to ``/solve`` that holds the
        connection open for however long the solve takes and returns the finished JSON.
        Arguments and behaviour are unchanged — prefer :meth:`solve`, which queues the task
        and polls for it instead of betting the whole solve on one long-lived connection."""
        warnings.warn(
            "solve_deprecated() is deprecated; use solve(), which submits to /v2/solve and polls",
            DeprecationWarning,
            stacklevel=2,
        )
        resolved_task = task or derive_task(enterprise, proxy is not None, version)
        body = {
            "task": resolved_task,
            "url": url,
            "sitekey": sitekey,
            "action": action,
            "proxy": proxy,
            "userAgent": user_agent or DEFAULT_USER_AGENT,
            "device": device,
            "enhanced": enhanced,
            "secretKey": secret_key,
        }
        return self._request("POST", "/solve", body)

    def kasada_login(
        self,
        *,
        proxy: str,
        site: Optional[str] = None,
        url: Optional[str] = None,
    ) -> dict[str, Any]:
        """Start a Kasada session (KasadaLogin). ``proxy`` is required — the token is
        IP-bound, so reuse it on the target request. Returns a dict with the full header
        set (``headers`` + ``x_kpsdk_ct/cd/v/h``, ``kpsdk_st``, ``user_agent``). Keep the
        result and pass it to :meth:`kasada_reload` to refresh the session later."""
        body = {"task": "KasadaLogin", "site": site, "url": url, "proxy": proxy}
        return self._request("POST", "/solve", body)

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
    ) -> dict[str, Any]:
        """Refresh a Kasada session (no proxy needed). Pass the :meth:`kasada_login` result
        as ``session`` and its ``kpsdk_st`` + ``hash`` + ``x_kpsdk_*`` are resent for you, or
        supply them explicitly. ``hash`` and ``x_kpsdk_ct`` are required — the cd's proof-of-work
        seed embeds both. Returns the refreshed headers."""
        if session:
            kpsdk_st = kpsdk_st if kpsdk_st is not None else session.get("kpsdk_st")
            hash = hash if hash is not None else session.get("hash")
            x_kpsdk_ct = x_kpsdk_ct if x_kpsdk_ct is not None else session.get("x_kpsdk_ct")
            x_kpsdk_v = x_kpsdk_v if x_kpsdk_v is not None else session.get("x_kpsdk_v")
            x_kpsdk_h = x_kpsdk_h if x_kpsdk_h is not None else session.get("x_kpsdk_h")
            site = site if site is not None else session.get("site")
        if kpsdk_st is None:
            raise ValueError("kasada_reload: kpsdk_st is required (pass the kasada_login result or kpsdk_st)")
        body = {
            "task": "KasadaReload",
            "site": site,
            "kpsdk_st": kpsdk_st,
            "hash": hash,
            "x_kpsdk_ct": x_kpsdk_ct,
            "x_kpsdk_v": x_kpsdk_v,
            "x_kpsdk_h": x_kpsdk_h,
        }
        return self._request("POST", "/solve", body)

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
    ) -> dict[str, Any]:
        """Run a Ticketmaster EPSF evaluation (EvaluateTask). ``url`` must be a Ticketmaster
        host and ``proxy`` is required — the decision is scored against the exit IP. Pass
        ``phone_number`` (country prefix included) for verify_phone, or ``queue_id`` /
        ``event_id`` for join_queue. ``user_agent`` picks the device profile the solver runs —
        screen, GPU and client hints all move with it — and defaults to
        :data:`DEFAULT_USER_AGENT` so evaluates match your solves. Returns a dict with
        ``token``, the EPSF allow token to replay on the next APS step, and ``decision``
        (``allow`` | ``challenge`` | ``block``)."""
        body = {
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
        return self._request("POST", "/solve", body)

    def check_balance(self) -> dict[str, Any]:
        """Current balance for the API key's account."""
        return self._request("GET", "/v1/balance")

    def _poll_solve(self, solve_id: str, expires_at: float, interval: float) -> dict[str, Any]:
        """Poll a queued solve until it is done, has failed, or the deadline runs out."""
        while True:
            left = max(0.0, expires_at - time.monotonic())
            time.sleep(min(interval, left))
            # The deadline bounds the waiting, not the looking: a solve that landed during
            # that last sleep is still a result the caller paid for, so every sleep gets its
            # poll and the timeout is only declared afterwards.
            result = self.get_solve(solve_id, timeout=max(_MIN_POLL_TIMEOUT, expires_at - time.monotonic()))
            status = result.get("status")
            if status == "done":
                if not result.get("token"):
                    # The gateway drops the token a few minutes after the solve completes (the
                    # token itself dies sooner). "done" with nothing in it means the caller
                    # came back too late, not that the solve succeeded at returning nothing.
                    raise KagedCapError(
                        0,
                        "result_expired",
                        f"solve {solve_id} finished but its token has already expired — solve again",
                    )
                return result
            if status == "failed":
                code = result.get("error") or "solve_failed"
                raise KagedCapError(0, code, f"solve {solve_id} failed: {code}")
            # Anything else — "running", or a status this SDK predates — is still in flight.
            if time.monotonic() >= expires_at:
                raise KagedCapError(0, "solve_timeout", f"solve {solve_id} did not finish before the deadline")

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
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode("utf-8"))
            except Exception:
                payload = {}
            raise KagedCapError(
                e.code,
                payload.get("error") or "error",
                payload.get("message") or f"HTTP {e.code}",
                payload.get("request_id"),
            ) from None
        except urllib.error.URLError as e:
            raise KagedCapError(0, "network_error", str(getattr(e, "reason", e))) from None
