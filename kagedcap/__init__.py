"""KagedCap Python SDK — solve reCAPTCHA, Ticketmaster tmpt/evaluate, and Kasada with an API key.

    from kagedcap import KagedCapClient
    kc = KagedCapClient(api_key)
    # Queues the solve and polls it every 5s; blocks until it lands or the deadline passes.
    result = kc.solve(sitekey="6Lc...", url="https://...", action="login", enterprise=True,
                      deadline=60)
    print(result["token"])

    # Or drive the wait yourself — submit_solve returns an id, get_solve reads its status.
    queued = kc.submit_solve(sitekey="6Lc...", url="https://...", action="login",
                             callback_url="https://you.example/hook")
    print(kc.get_solve(queued["id"])["status"])   # running | done | failed

    # Kasada — the login result carries its headers into the reload for you.
    login = kc.kasada_login(site="ticketmaster", proxy=proxy)
    fresh = kc.kasada_reload(login)

    # Ticketmaster EPSF — the host picks verify_phone vs join_queue unless you override it.
    ev = kc.evaluate("https://auth.ticketmaster.com/...", proxy, phone_number="+12025550123")
    print(ev["decision"], ev["token"])
"""

from .client import (
    KagedCapClient,
    KagedCapError,
    RetryPolicy,
    derive_task,
    DEFAULT_USER_AGENT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_DEADLINE,
    TASKS,
)

__all__ = [
    "KagedCapClient",
    "KagedCapError",
    "RetryPolicy",
    "derive_task",
    "DEFAULT_USER_AGENT",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_DEADLINE",
    "TASKS",
]
__version__ = "1.0.1"
