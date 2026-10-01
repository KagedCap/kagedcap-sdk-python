"""Tests for the KagedCap client, driven against a stdlib HTTP stub so the real submit/poll
loop and the per-fleet result handling are what's under test. No third-party deps."""

import json
import os
import sys
import threading
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kagedcap import KagedCapClient, KagedCapError  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep test output quiet
        pass

    def _dispatch(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = {}
        if raw:
            try:
                body = json.loads(raw)
            except Exception:
                body = {}
        # Header names are case-insensitive on the wire and urllib capitalizes them
        # (x-api-key -> X-api-key), so normalize to lowercase for stable lookups.
        hdrs = {k.lower(): v for k, v in self.headers.items()}
        status, payload = self.server.route(self.command, self.path, body, hdrs)
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _dispatch
    do_POST = _dispatch


class ClientTest(unittest.TestCase):
    def serve(self, route):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        srv.route = route
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        host, port = srv.server_address
        return KagedCapClient("test-key", base_url=f"http://{host}:{port}", poll_interval=0.01)

    # --- reCAPTCHA async path ---

    def test_solve_returns_token_when_status_reaches_done(self):
        def route(method, path, body, headers):
            if method == "POST" and path == "/v2/solve":
                seen["key"] = headers.get("x-api-key")
                return 202, {"success": True, "id": "job-1", "status": "running"}
            return 200, {"success": True, "id": "job-1", "status": "done", "token": "03AF", "task": "ReCaptchaV3TaskProxyLess"}

        seen = {}
        kc = self.serve(route)
        res = kc.solve("6Lc", "https://example.com", "login", deadline=2)
        self.assertEqual(res["token"], "03AF")
        self.assertEqual(seen["key"], "test-key")  # asserted in the test body, not the handler thread

    def test_solve_reports_result_expired_when_done_has_no_token(self):
        def route(method, path, body, headers):
            if method == "POST":
                return 202, {"success": True, "id": "job-1", "status": "running"}
            return 200, {"success": True, "id": "job-1", "status": "done", "token": None}

        kc = self.serve(route)
        with self.assertRaises(KagedCapError) as cm:
            kc.solve("6Lc", "https://example.com", "login", deadline=2)
        self.assertEqual(cm.exception.code, "result_expired")

    def test_solve_surfaces_the_gateway_failure_code(self):
        def route(method, path, body, headers):
            if method == "POST":
                return 202, {"success": True, "id": "job-1", "status": "running"}
            return 200, {"success": True, "id": "job-1", "status": "failed", "error": "proxy_required"}

        kc = self.serve(route)
        with self.assertRaises(KagedCapError) as cm:
            kc.solve("6Lc", "https://example.com", "login", deadline=2)
        self.assertEqual(cm.exception.code, "proxy_required")

    def test_solve_forwards_callback_url_and_idempotency_key(self):
        seen = {}

        def route(method, path, body, headers):
            if method == "POST":
                seen["body"] = body
                seen["key"] = headers.get("idempotency-key")
                return 202, {"success": True, "id": "job-1", "status": "running"}
            return 200, {"success": True, "id": "job-1", "status": "done", "token": "t"}

        kc = self.serve(route)
        kc.solve("6Lc", "https://example.com", "login", callback_url="https://hooks.example.com/x", idempotency_key="order-9", deadline=2)
        self.assertEqual(seen["body"].get("callback_url"), "https://hooks.example.com/x")
        self.assertEqual(seen["key"], "order-9")

    def test_solve_rejects_a_kasada_task_without_calling_the_api(self):
        calls = {"n": 0}

        def route(method, path, body, headers):
            calls["n"] += 1
            return 200, {}

        kc = self.serve(route)
        with self.assertRaises(KagedCapError) as cm:
            kc.solve("x", "https://example.com", task="KasadaLogin", deadline=2)
        self.assertEqual(cm.exception.code, "validation_error")
        self.assertEqual(calls["n"], 0)

    # --- Kasada synchronous path ---

    def test_kasada_login_posts_to_sync_solve_and_returns_the_result_directly(self):
        seen = {}

        def route(method, path, body, headers):
            seen["method"] = method
            seen["path"] = path
            seen["task"] = body.get("task")
            seen["proxy"] = body.get("proxy")
            return 200, {
                "success": True, "site": "ticketmaster", "task": "KasadaLogin",
                "headers": {"user-agent": "UA"}, "x_kpsdk_ct": "ct", "x_kpsdk_cd": "cd",
                "x_kpsdk_v": "v", "x_kpsdk_h": "h", "kpsdk_st": 123, "hash": "hh",
                "reload": True, "user_agent": "UA",
            }

        kc = self.serve(route)
        res = kc.kasada_login(proxy="http://u:p@1.2.3.4:8080", site="ticketmaster")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["path"], "/solve")  # synchronous endpoint, not /v2/solve
        self.assertEqual(seen["task"], "KasadaLogin")
        self.assertEqual(seen["proxy"], "http://u:p@1.2.3.4:8080")
        # The sync /solve body is the full result, returned directly — no polling.
        self.assertEqual(res["x_kpsdk_ct"], "ct")
        self.assertEqual(res["headers"]["user-agent"], "UA")
        self.assertEqual(res["user_agent"], "UA")

    def test_kasada_reload_posts_to_sync_solve_and_resends_the_prior_session(self):
        seen = {}

        def route(method, path, body, headers):
            seen["method"] = method
            seen["path"] = path
            seen["body"] = body
            return 200, {"success": True, "x_kpsdk_ct": "ct1", "x_kpsdk_cd": "cd1"}

        kc = self.serve(route)
        res = kc.kasada_reload({"kpsdk_st": 123, "hash": "hh", "x_kpsdk_ct": "ct0", "site": "ticketmaster"})
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["path"], "/solve")  # synchronous endpoint, not /v2/solve
        self.assertEqual(seen["body"].get("task"), "KasadaReload")
        self.assertEqual(seen["body"].get("kpsdk_st"), 123)
        self.assertEqual(seen["body"].get("hash"), "hh")
        self.assertEqual(res["x_kpsdk_ct"], "ct1")

    # --- evaluate async path ---

    def test_evaluate_returns_the_decision(self):
        def route(method, path, body, headers):
            if method == "POST" and path == "/v2/solve":
                return 202, {"success": True, "id": "job-e", "status": "running"}
            return 200, {"success": True, "id": "job-e", "status": "done", "token": "ev", "decision": "allow"}

        kc = self.serve(route)
        res = kc.evaluate("https://auth.ticketmaster.com/x", "http://u:p@1.2.3.4:8080", deadline=2)
        self.assertEqual(res["decision"], "allow")
        self.assertEqual(res["token"], "ev")

    def test_evaluate_keeps_a_challenge_with_no_token(self):
        def route(method, path, body, headers):
            if method == "POST":
                return 202, {"success": True, "id": "job-e", "status": "running"}
            return 200, {"success": True, "id": "job-e", "status": "done", "token": "", "decision": "challenge"}

        kc = self.serve(route)
        res = kc.evaluate("https://auth.ticketmaster.com/x", "http://u:p@1.2.3.4:8080", deadline=2)
        self.assertEqual(res["decision"], "challenge")

    def test_evaluate_reports_result_expired_when_done_has_no_decision(self):
        def route(method, path, body, headers):
            if method == "POST":
                return 202, {"success": True, "id": "job-e", "status": "running"}
            return 200, {"success": True, "id": "job-e", "status": "done"}

        kc = self.serve(route)
        with self.assertRaises(KagedCapError) as cm:
            kc.evaluate("https://auth.ticketmaster.com/x", "http://u:p@1.2.3.4:8080", deadline=2)
        self.assertEqual(cm.exception.code, "result_expired")

    # --- deprecated sync paths + primitives ---

    def test_deprecated_methods_use_the_synchronous_solve(self):
        for name, call in [
            ("solve_deprecated", lambda kc: kc.solve_deprecated("6Lc", "https://example.com", "login")),
            ("kasada_login_deprecated", lambda kc: kc.kasada_login_deprecated(proxy="http://u:p@1.2.3.4:8080")),
            ("evaluate_deprecated", lambda kc: kc.evaluate_deprecated("https://auth.ticketmaster.com/x", "http://u:p@1.2.3.4:8080")),
        ]:
            with self.subTest(name):
                hit = {}

                def route(method, path, body, headers):
                    hit["path"] = path
                    return 200, {"success": True, "token": "t", "x_kpsdk_ct": "ct", "decision": "allow"}

                kc = self.serve(route)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    call(kc)
                self.assertEqual(hit["path"], "/solve", f"{name} must use the legacy /solve")

    def test_submit_solve_and_get_solve_primitives(self):
        def route(method, path, body, headers):
            if method == "POST" and path == "/v2/solve":
                return 202, {"success": True, "id": "job-p", "status": "running"}
            return 200, {"success": True, "id": "job-p", "status": "done", "token": "tok-p"}

        kc = self.serve(route)
        sub = kc.submit_solve("6Lc", "https://example.com", "login")
        self.assertEqual(sub["id"], "job-p")
        job = kc.get_solve(sub["id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["token"], "tok-p")


if __name__ == "__main__":
    unittest.main()
