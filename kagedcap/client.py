"""KagedCap client. Uses only the Python standard library (no dependencies)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional

DEFAULT_BASE_URL = "https://api.kagedcap.io"

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
]


class KagedCapError(Exception):
    """Raised on any non-2xx response or transport failure."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def derive_task(enterprise: bool, has_proxy: bool, version: str = "v3") -> str:
    suffix = "Task" if has_proxy else "TaskProxyLess"
    if version == "v2":
        return "ReCaptchaV2" + suffix  # no enterprise variant for v2 yet
    base = "ReCaptchaV3Enterprise" if enterprise else "ReCaptchaV3"
    return base + suffix


class KagedCapClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, timeout: float = 120.0) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

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
    ) -> dict[str, Any]:
        """Solve a captcha. Pass ``enterprise`` and optionally ``proxy`` to auto-select
        the v3 task, or ``version="v2"`` for reCAPTCHA v2 invisible, or set ``task``
        explicitly. ``action`` is required for v3 and ignored for v2. Returns the JSON."""
        resolved_task = task or derive_task(enterprise, proxy is not None, version)
        body = {
            "task": resolved_task,
            "url": url,
            "sitekey": sitekey,
            "action": action,
            "proxy": proxy,
            "userAgent": user_agent,
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
        x_kpsdk_ct: Optional[str] = None,
        x_kpsdk_v: Optional[str] = None,
        x_kpsdk_h: Optional[str] = None,
        site: Optional[str] = None,
    ) -> dict[str, Any]:
        """Refresh a Kasada session (no proxy needed). Pass the :meth:`kasada_login` result
        as ``session`` and its ``kpsdk_st`` + ``x_kpsdk_*`` are resent for you, or supply
        them explicitly. Returns the refreshed headers."""
        if session:
            kpsdk_st = kpsdk_st if kpsdk_st is not None else session.get("kpsdk_st")
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
            "x_kpsdk_ct": x_kpsdk_ct,
            "x_kpsdk_v": x_kpsdk_v,
            "x_kpsdk_h": x_kpsdk_h,
        }
        return self._request("POST", "/solve", body)

    def check_balance(self) -> dict[str, Any]:
        """Current balance for the API key's account."""
        return self._request("GET", "/v1/balance")

    def _request(self, method: str, path: str, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        data = None
        if body is not None:
            clean = {k: v for k, v in body.items() if v is not None}
            data = json.dumps(clean).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"x-api-key": self.api_key, "content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode("utf-8"))
            except Exception:
                payload = {}
            raise KagedCapError(e.code, payload.get("error", "error"), payload.get("message", f"HTTP {e.code}")) from None
        except urllib.error.URLError as e:
            raise KagedCapError(0, "network_error", str(getattr(e, "reason", e))) from None
