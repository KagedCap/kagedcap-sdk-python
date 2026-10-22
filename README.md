<p align="center"><img src="https://kagedcap.io/kc-logo.svg" width="260" alt="KagedCap"></p>

# KagedCap Python SDK

Solve reCAPTCHA (v3, v3 Enterprise, v2), Ticketmaster tmpt and evaluate, and
Kasada with a single API key. Pure standard library — no dependencies.

## Install

```bash
pip install kagedcap
```

Requires Python 3.8+.

## Quick start

```python
import os
from kagedcap import KagedCapClient

kc = KagedCapClient(os.environ["KAGEDCAP_API_KEY"])

result = kc.solve(
    sitekey="6LcvL3UrAAAAAO_9u8Seiuf-I6F_tP_jSS-zndXV",
    url="https://www.ticketmaster.com",
    action="Event",
    enterprise=True,   # ProxyLess Enterprise
    deadline=60,       # seconds; the whole budget for the solve. Default 120.
)
print(result["token"])

print("balance:", kc.check_balance()["display"])
```

## How `solve` waits

`solve` blocks and returns the finished solve, same as it always has. Underneath it no longer
holds a connection open for the whole solve: it queues the task on `POST /v2/solve` (which
answers `202` with an id), then polls `GET /v2/solve/{id}` every `poll_interval` seconds
until that id reports `done`.

`deadline` is the caller's whole budget in seconds — the submit plus every poll — and defaults
to 120. `poll_interval` defaults to 5s. Both are settable on the client and overridable per
call:

```python
kc = KagedCapClient(api_key, poll_interval=2.0)          # every solve on this client polls at 2s
result = kc.solve(sitekey="6Lc...", url="https://example.com",
                  deadline=30, poll_interval=1.0)        # just this one
print(result["token"], result.get("solve_ms"))           # solve_ms/elapsed_ms are best-effort
```

`solve` raises `KagedCapError` rather than returning a half-result:

| code | when |
|---|---|
| `solve_timeout` | the deadline passed with the solve still running |
| the failure's own code | the fleet gave up — whatever `error` the solve reported |
| `result_expired` | finished, but its token was already cleared (see below) |

A finished solve stops carrying its token about five minutes later — a reCAPTCHA token is dead
well before that anyway — so a poll that arrives late gets `done` with `token: null`. That is
an expired result, not a successful empty one, and `solve` says so.

### Callbacks and driving the poll yourself

`submit_solve` queues a solve and returns as soon as the gateway has it; `get_solve` reads one
back. Together they are what `solve` is built from, and they are what you want when a callback,
not a poll loop, is how you find out:

```python
queued = kc.submit_solve(
    sitekey="6Lc...", url="https://example.com", action="login",
    callback_url="https://you.example/kagedcap-hook",  # https, publicly resolvable
    idempotency_key="checkout-8412",                   # safe to retry the submit under
)
print(queued["id"])

state = kc.get_solve(queued["id"])
# Check status — "running" | "done" | "failed" — not success, which is only true once done.
if state["status"] == "done":
    print(state["token"])
```

`get_solve` returns the body untouched, expired token and all. An id the gateway doesn't have
raises `not_found` (404); an id belonging to another account answers exactly the same way.

### Retries

Since 1.0.1 the SDK waits out transient failures instead of raising them. The edge in front of
the gateway sheds load with a bare `429` before a request ever reaches the API, and the API
itself answers `429` / `5xx` when it is briefly over capacity; the same request succeeds a
second later, so a solve should not be lost to it.

- **What is retried on `/v2`:** HTTP `429`, `500`, `502`, `503`, `504`, and transport failures
  (refused, reset, EOF, timeout, DNS) on the `/v2/solve` submit and the `/v2/solve/{id}` poll.
  The verdict of a v2 solve comes through the poll body, so a `5xx` on the submit is just a
  failed hop, and a resend under the same `Idempotency-Key` gets the original job back.
- **What is retried on the synchronous `/solve`** (`kasada_login`, `kasada_reload`, the
  `*_deprecated` methods): every call goes out under an `Idempotency-Key` — yours, or a UUIDv4
  the SDK mints for that call — and only a failure that never reached the gateway is resent,
  with the same key: a transport failure, or a `429` / `502` / `503` / `504` page the edge in
  front of the API made itself (not our JSON envelope). The solve runs inside that request, so
  a bare repeat could run, and bill, it twice; under the key the gateway refuses the duplicate
  with `409` instead. A `429` or `5xx` the gateway answered — our envelope, with an `error`
  field — is raised as is: the gateway claims the key before it does anything else, so a
  repeat under it could only earn `409 idempotency_conflict`, and the original error is the
  honest outcome. If a retry does meet that `409`, the error you get is the last transient
  failure, with its attempt count, not the `409`.
- **The 100 s window on `/solve`:** no retry of a synchronous call starts more than 100 s
  after its first attempt was sent, whatever `timeout` has left. The gateway's claim on the
  key lasts 120 s, and a resend after it has lapsed is not deduplicated — it would run, and
  bill, a fresh solve. When the window ends the wait, the error is the last real response,
  with its attempt count and elapsed time, exactly as at the deadline.
- **What is never retried:** a job whose `status` is `"failed"`, any `4xx` other than `429`,
  and the codes that mean the request itself is wrong, whatever status carries them:
  `validation_error`, `unauthorized`, `key_revoked`, `account_suspended`, `insufficient_funds`,
  `key_spend_cap_reached`, `proxy_invalid`, `proxy_required`, `proxy_not_allowed`,
  `unsupported_task`, `action_required`, `callback_url_invalid`, `not_found`,
  `idempotency_conflict`, `key_frozen`, `subscription_quota_exhausted`,
  `subscription_past_due`, `host_not_allowed`. Those raise at once, as before.
- **How long:** until the call's budget runs out — `deadline` for `solve` / `evaluate`,
  `timeout` (the client's, by default) for `submit_solve` / `get_solve` and the sync methods,
  which also stop at the 100 s window above.
  Backoff starts at 250 ms, doubles to a 5 s cap, with full jitter (a random wait in
  `[50 ms, backoff]`). A `Retry-After` header is honoured instead, capped at 10 s; one that
  parses to 0, or doesn't parse, falls back to the backoff. No wait is ever shorter than
  50 ms. Between polls that *answered*, `poll_interval` still applies.
- **At the deadline:** a retry is not started with less than 500 ms of the budget left — it
  could only time out client-side — so the error reports the last answer the gateway actually
  gave. A trailing attempt that the deadline cuts short is counted in `.attempts` but never
  replaces that answer's status and code with `network_error`.
- **Idempotency:** every `/v2/solve` submit and every synchronous `/solve` carries an
  `Idempotency-Key` — yours, or a UUIDv4 the SDK generates per call — and every retry of it
  carries the same one, so a retry after a `502` that actually landed gets the original job
  back (on `/v2`) or a `409` (on `/solve`) instead of a second, billed solve.
- **Ctrl-C:** a `KeyboardInterrupt` ends the wait at once; the retry loop never swallows it.
- **When it still fails:** the `KagedCapError` you get keeps the last real response's
  `.status`, `.code` and `.request_id`, and its message names the attempt count and total
  wait, e.g.
  `POST /v2/solve: gave up after 14 attempts over 30.0s (deadline reached); last failure: HTTP 429 rate_limited: too many requests`.
  `.attempts`, `.idempotency_key` and `.retry_after` (seconds, never negative, capped at 24 h)
  are set on it for debugging.

Turn it off, or tune it, with a `RetryPolicy` on the client. A zero or negative
`initial_backoff` / `max_backoff` means the default, not a tight loop:

```python
from kagedcap import KagedCapClient, RetryPolicy

kc = KagedCapClient(api_key, retry_policy=RetryPolicy(enabled=False))       # fail on the first 429/5xx
kc = KagedCapClient(api_key, retry_policy=RetryPolicy(max_attempts=5,
                                                      initial_backoff=0.5,  # seconds
                                                      max_backoff=8.0))
```

### The old synchronous call

`solve` and `evaluate` queue on `/v2/solve` and poll. Each keeps a `*_deprecated` twin
(`solve_deprecated`, `evaluate_deprecated`) that calls the legacy synchronous `/solve`, holding
the HTTP connection open for however long the solve takes. They still work, now under a
per-call `Idempotency-Key` with the retries described in [Retries](#retries), but raise a
`DeprecationWarning`; prefer the async methods.

Kasada is held back from `/v2`, so `kasada_login` and `kasada_reload` use the synchronous
`/solve` endpoint directly — one POST that holds the connection open and returns the full result
(see [Kasada](#kasada)). `kasada_login_deprecated` / `kasada_reload_deprecated` are kept as
aliases that raise a `DeprecationWarning`.

## User agent

Solves always carry a real desktop UA — the token embeds it. Omit `user_agent` and the SDK
sends `DEFAULT_USER_AGENT`, the same Chrome desktop profile the solver fleet runs, so the
token agrees with the identity that produced it. Pass `user_agent` to match the browser your
own traffic presents instead:

```python
from kagedcap import DEFAULT_USER_AGENT

print(DEFAULT_USER_AGENT)
# Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36

kc.solve(sitekey="6Lc...", url="https://example.com", action="login", user_agent=my_ua)
```

Kasada is the exception: `kasada_login` / `kasada_reload` never send a UA — the harvester
returns the identity it actually used in `login["user_agent"]` and `login["headers"]`.

## With a proxy

```python
kc.solve(
    sitekey="6Lc...",
    url="https://example.com/login",
    action="login",
    proxy="http://user:pass@1.2.3.4:8080",  # or host:port:user:pass
)
```

Omit `proxy` for a ProxyLess solve. Set `enterprise=True` for Enterprise sitekeys,
or pass `task=` explicitly.

## Kasada

`kasada_login` starts a session (requires a proxy — the token is IP-bound) and returns the
full header set. Keep that result and pass it to `kasada_reload` to refresh the session — the
SDK resends the session's `kpsdk_st`, `hash`, and `x_kpsdk_*` values for you (`hash` + `x_kpsdk_ct` are required).

```python
login = kc.kasada_login(site="ticketmaster", proxy="http://user:pass@1.2.3.4:8080")
# Replay login["headers"] (user-agent + sec-ch-ua*) and login["x_kpsdk_*"] on your request.

fresh = kc.kasada_reload(login)  # no proxy needed
print(fresh["x_kpsdk_cd"])
```

Kasada results have **no `token`** — replay `headers` and the `x_kpsdk_*` values instead.

`kasada_login` and `kasada_reload` use the synchronous `/solve` endpoint: a single POST holds the
connection open until the session lands and returns the full result directly (`success`, `site`,
`headers`, `x_kpsdk_ct/cd/v/h`, `kpsdk_st`, `hash`, `reload`, `user_agent`) — no `deadline` /
`poll_interval` poll loop. `kasada_login_deprecated` / `kasada_reload_deprecated` remain as
aliases.

## Evaluate (Ticketmaster EPSF)

`evaluate` runs an EPSF evaluation and returns the allow token to replay on the next APS
step, plus `decision` (`allow` | `challenge` | `block`). `url` (a Ticketmaster host) and
`proxy` are both required.

```python
ev = kc.evaluate(
    "https://auth.ticketmaster.com/epsf/gec/",
    "http://user:pass@1.2.3.4:8080",
    phone_number="+12025550123",   # country prefix required
)
print(ev["decision"], ev["token"])

# join_queue instead — sent as queueId / eventId on the wire.
ev = kc.evaluate(event_url, proxy, queue_id="...", event_id="...")
```

Leave `action` unset and the host decides: `auth.*` → `verify_phone`, any other Ticketmaster
host → `join_queue`. Pass `action="verify_phone"` or `action="join_queue"` only to override
that. `user_agent` defaults to `DEFAULT_USER_AGENT` as it does for `solve` — here it also
selects the device profile the solver runs, so pass your own to match your traffic.

`evaluate` queues on `/v2/solve` and polls; `evaluate_deprecated` uses the legacy synchronous `/solve`.

## Errors

Failures raise `KagedCapError` with `.status`, `.code`, `.request_id` (quote it at support) and
the message. `.code` is a stable string from the gateway — the ones you'll see:

| code | status | when |
|---|---|---|
| `unauthorized` | 401 | missing or invalid API key |
| `account_suspended` | 403 | the account is suspended |
| `host_not_allowed` | 403 | the key's allowlist doesn't include the page host |
| `not_found` | 404 | a `/v2` job id that's unknown, expired, or not yours |
| `validation_error` | 400 | the request shape is wrong |
| `proxy_invalid` | 400 | the proxy is malformed or disallowed — caught pre-flight, no solve attempted |
| `callback_url_invalid` | 400 | `callback_url` isn't a public https URL |
| `proxy_unreachable` | 422 | the solve ran and your proxy didn't answer — not billed (was `502` before 2026-09-24) |
| `insufficient_funds` | 402 | out of balance — top up |
| `key_spend_cap_reached` | 402 | the key hit its spend cap |
| `subscription_quota_exhausted` | 402 | the plan's included quota is used up |
| `subscription_past_due` | 402 | the subscription is past due |
| `idempotency_conflict` | 409 | an `idempotency_key` was reused for a different request |
| `concurrency_limit_exceeded` | 429 | too many solves in flight at once |
| `key_frozen` | 429 | the key auto-froze after a burst of failed solves (usually bad proxies) |
| `rate_limited` | 429 | too many requests — back off |
| `overloaded` | 429 | the gateway is shedding load — back off |
| `solve_failed` | 502 | the solve was attempted and failed — not billed; don't blindly retry |
| `solve_timeout` | 504 | the solve ran past its deadline — not billed |
| `no_capacity` | 503 | no node was free to take the solve |
| `solver_unavailable` | 503 | a node was chosen but couldn't be reached |
| `maintenance` | 503 | the fleet is down for maintenance |
| `proxyless_disabled` | 503 | proxyless solving is turned off right now |
| `internal_error` | 500 | an unexpected gateway error |

On `/v2` the SDK retries `429`, `500`, `502`, `503` and `504` with backoff for you — see
[Retries](#retries) — so the ones you see are the last of a run that outlived the deadline. On
the synchronous `/solve` a `429` / `5xx` the gateway answered is raised at once (a keyed
repeat would only earn `409`); never blindly retry `502` `solve_failed` or `422`
`proxy_unreachable` yourself — the proxy or the target is the problem, and a retry just spends
another attempt on it.

Some codes come from the SDK itself, not the gateway, and carry `.status == 0`: `solve_timeout`
(the client-side deadline in `solve` elapsed), `result_expired` (the solve finished but its
token was already cleared), and `network_error` (the request never reached the gateway, or the
connection dropped before it answered). `.status` is otherwise the HTTP status, and
`.request_id` is `None` unless the gateway sent one. `.attempts` is how many times the request
was sent, `.idempotency_key` the key it went out under, and `.retry_after` the last response's
`Retry-After` in seconds, when it had one.

```python
from kagedcap import KagedCapError
try:
    kc.solve(...)
except KagedCapError as e:
    if e.code == "insufficient_funds":
        ...  # top up
```

Only successful solves are billed.
