"""KagedCap Python SDK — solve reCAPTCHA, Ticketmaster tmpt/evaluate, and Kasada with an API key.

    from kagedcap import KagedCapClient
    kc = KagedCapClient(api_key)
    result = kc.solve(sitekey="6Lc...", url="https://...", action="login", enterprise=True)
    print(result["token"])

    # Kasada — the login result carries its headers into the reload for you.
    login = kc.kasada_login(site="ticketmaster", proxy=proxy)
    fresh = kc.kasada_reload(login)

    # Ticketmaster EPSF — the host picks verify_phone vs join_queue unless you override it.
    ev = kc.evaluate("https://auth.ticketmaster.com/...", proxy, phone_number="+12025550123")
    print(ev["decision"], ev["token"])
"""

from .client import KagedCapClient, KagedCapError, derive_task, DEFAULT_USER_AGENT, TASKS

__all__ = ["KagedCapClient", "KagedCapError", "derive_task", "DEFAULT_USER_AGENT", "TASKS"]
__version__ = "0.2.1"
