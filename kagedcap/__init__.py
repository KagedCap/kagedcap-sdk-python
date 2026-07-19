"""KagedCap Python SDK — solve reCAPTCHA v3 tokens with an API key.

    from kagedcap import KagedCapClient
    kc = KagedCapClient(api_key)
    result = kc.solve(sitekey="6Lc...", url="https://...", action="login", enterprise=True)
    print(result["token"])
"""

from .client import KagedCapClient, KagedCapError, derive_task, TASKS

__all__ = ["KagedCapClient", "KagedCapError", "derive_task", "TASKS"]
__version__ = "0.1.0"
