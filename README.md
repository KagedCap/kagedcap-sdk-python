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

### The old synchronous call

`solve_deprecated` takes the same arguments as `solve` (minus `deadline`, `poll_interval`,
`callback_url` and `idempotency_key`) and calls the legacy synchronous `/solve` endpoint, which
holds the HTTP connection open for however long the solve takes. It still works and its
behaviour is unchanged — it just raises a `DeprecationWarning` now. Prefer `solve`.

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

## Errors

Failures raise `KagedCapError` with `.status`, `.code`, `.request_id` (quote it at support) and
the message. Common codes: `unauthorized`, `insufficient_funds`, `solve_failed`, `solve_timeout`,
`result_expired`, `not_found`, `proxy_required`, `proxy_not_allowed`, `validation_error`,
`callback_url_invalid`, `concurrency_limit_exceeded`, `key_frozen`, `proxyless_disabled`,
`maintenance`, `internal_error`.

`.status` is the HTTP status when there was one, and `0` when the SDK itself is the one giving
up — `solve_timeout`, `result_expired`, `network_error`. `.request_id` is `None` unless the
gateway sent one.

```python
from kagedcap import KagedCapError
try:
    kc.solve(...)
except KagedCapError as e:
    if e.code == "insufficient_funds":
        ...  # top up
```

Only successful solves are billed.
