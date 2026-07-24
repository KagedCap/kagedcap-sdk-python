<p align="center"><img src="https://kagedcap.io/kc-logo.svg" width="260" alt="KagedCap"></p>

# KagedCap Python SDK

Solve reCAPTCHA (v3, v3 Enterprise, v2), Ticketmaster tmpt, and Kasada with a single API
key. Pure standard library — no dependencies.

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
    # Send a real desktop UA — the token embeds it, so match the browser your traffic presents.
    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    enterprise=True,   # ProxyLess Enterprise
)
print(result["token"])

print("balance:", kc.check_balance()["display"])
```

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
SDK resends the session's `kpsdk_st` and `x_kpsdk_*` values for you.

```python
login = kc.kasada_login(site="ticketmaster", proxy="http://user:pass@1.2.3.4:8080")
# Replay login["headers"] (user-agent + sec-ch-ua*) and login["x_kpsdk_*"] on your request.

fresh = kc.kasada_reload(login)  # no proxy needed
print(fresh["x_kpsdk_cd"])
```

Kasada results have **no `token`** — replay `headers` and the `x_kpsdk_*` values instead.

## Errors

Failures raise `KagedCapError` with `.status`, `.code`, and the message. Common
codes: `unauthorized`, `insufficient_funds`, `solve_failed`, `solve_timeout`,
`proxy_required`, `proxy_not_allowed`, `validation_error`.

```python
from kagedcap import KagedCapError
try:
    kc.solve(...)
except KagedCapError as e:
    if e.code == "insufficient_funds":
        ...  # top up
```

Only successful solves are billed.
