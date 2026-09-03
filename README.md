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
)
print(result["token"])

print("balance:", kc.check_balance()["display"])
```

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

Failures raise `KagedCapError` with `.status`, `.code`, and the message. Common
codes: `unauthorized`, `insufficient_funds`, `solve_failed`, `solve_timeout`,
`proxy_required`, `proxy_not_allowed`, `validation_error`, `concurrency_limit_exceeded`, `key_frozen`.

```python
from kagedcap import KagedCapError
try:
    kc.solve(...)
except KagedCapError as e:
    if e.code == "insufficient_funds":
        ...  # top up
```

Only successful solves are billed.
