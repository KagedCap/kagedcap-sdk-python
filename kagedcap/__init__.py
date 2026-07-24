"""KagedCap Python SDK — solve reCAPTCHA, Ticketmaster tmpt, and Kasada with an API key.

    from kagedcap import KagedCapClient
    kc = KagedCapClient(api_key)
    result = kc.solve(sitekey="6Lc...", url="https://...", action="login", enterprise=True)
    print(result["token"])

    # Kasada — the login result carries its headers into the reload for you.
    login = kc.kasada_login(site="ticketmaster", proxy=proxy)
    fresh = kc.kasada_reload(login)
"""

from .client import KagedCapClient, KagedCapError, derive_task, TASKS

__all__ = ["KagedCapClient", "KagedCapError", "derive_task", "TASKS"]
__version__ = "0.2.0"
