"""Tests for the KagedCap client, driven against a stdlib HTTP stub so the real submit/poll
loop and the per-fleet result handling are what's under test. No third-party deps."""

import json
import os
import socket
import sys
import threading
import time
import unittest
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kagedcap import KagedCapClient, KagedCapError, RetryPolicy  # noqa: E402
from kagedcap import client as client_module  # noqa: E402
from kagedcap.client import _parse_retry_after  # noqa: E402

# Backoff as short as the policy allows (every wait is floored at 50 ms), so a retry test runs
# in the time it takes to count the attempts rather than the seconds the production schedule
# would wait between them.
FAST_RETRY = RetryPolicy(initial_backoff=0.001, max_backoff=0.005)

# Every wait between attempts is at least this long, whatever the policy or Retry-After says.
MIN_BACKOFF = 0.05

# A retry is not started with less than this much of the deadline left; the last real answer
# is reported instead. The budget tests size their deadlines around it.
MIN_ATTEMPT_BUDGET = 0.5

# No retry of the synchronous /solve starts this long after its first attempt: the gateway's
# claim on the Idempotency-Key lasts 120s, and a later resend would be a fresh, billed solve.
SYNC_RETRY_WINDOW = 100.0


class _FakeClock:
    """A monotonic clock the test drives in place of ``kagedcap.client.time``: ``monotonic``
    reads it, ``sleep`` advances it without waiting, and a stub route calls ``advance`` to
    stand in for the minutes a real attempt spends at the edge before it answers."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, seconds)

    def advance(self, seconds):
        self.now += seconds


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
        # A route answers (status, payload) or (status, payload, extra_headers). A str payload
        # is sent verbatim as text/plain — the shape of an edge answering for the API. None
        # answers nothing at all: the connection closes without a response, the shape of a
        # hop dropping the request mid-flight.
        answer = self.server.route(self.command, self.path, body, hdrs)
        if answer is None:
            return
        status, payload = answer[0], answer[1]
        extra = answer[2] if len(answer) > 2 else {}
        if isinstance(payload, str):
            data, ctype = payload.encode("utf-8"), "text/plain"
        else:
            data, ctype = json.dumps(payload).encode("utf-8"), "application/json"
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    do_GET = _dispatch
    do_POST = _dispatch


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # A route that outlives the client's timeout writes to a socket the client has closed;
        # that is the point of those tests, not something to print a traceback about.
        pass


class _StubServerTest(unittest.TestCase):
    """Carries no tests itself: the stub-server helper the test classes below share."""

    def serve(self, route, retry_policy=FAST_RETRY):
        srv = _QuietServer(("127.0.0.1", 0), _Handler)
        srv.route = route
        # shutdown() waits for serve_forever's poll; the default 0.5s would be most of the
        # suite's wall time across the ~100 stub servers the matrices below start.
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)  # cleanups run last-added first: stop serving, then close
        host, port = srv.server_address
        return KagedCapClient("test-key", base_url=f"http://{host}:{port}", poll_interval=0.01, retry_policy=retry_policy)


class ClientTest(_StubServerTest):
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

    def test_deprecated_methods_use_the_synchronous_solve_under_a_minted_key(self):
        for name, call in [
            ("solve_deprecated", lambda kc: kc.solve_deprecated("6Lc", "https://example.com", "login")),
            ("kasada_login_deprecated", lambda kc: kc.kasada_login_deprecated(proxy="http://u:p@1.2.3.4:8080")),
            ("kasada_reload_deprecated", lambda kc: kc.kasada_reload_deprecated({"kpsdk_st": 1, "hash": "hh", "x_kpsdk_ct": "ct"})),
            ("evaluate_deprecated", lambda kc: kc.evaluate_deprecated("https://auth.ticketmaster.com/x", "http://u:p@1.2.3.4:8080")),
        ]:
            with self.subTest(name):
                hit = {}

                def route(method, path, body, headers):
                    hit["path"] = path
                    hit["key"] = headers.get("idempotency-key")
                    return 200, {"success": True, "token": "t", "x_kpsdk_ct": "ct", "decision": "allow"}

                kc = self.serve(route)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    call(kc)
                self.assertEqual(hit["path"], "/solve", f"{name} must use the legacy /solve")
                # Bare or not, a v1 call carries a key, so its own retries cannot double-solve.
                self.assertEqual(uuid.UUID(hit["key"]).version, 4, f"{name} must mint a UUIDv4 Idempotency-Key")

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


class RetryTest(_StubServerTest):
    """The 2026-10-20 retry policy: transient failures (429, 5xx, transport) are waited out
    under one idempotency key until the caller's deadline; verdicts are raised at once."""

    DONE = (200, {"success": True, "id": "job-r", "status": "done", "token": "tok-r"})
    ACCEPTED = (202, {"success": True, "id": "job-r", "status": "running"})

    def test_submit_retries_429_under_the_same_generated_idempotency_key(self):
        keys = []

        def route(method, path, body, headers):
            if method == "POST":
                keys.append(headers.get("idempotency-key"))
                if len(keys) == 1:
                    return 429, "rate limited"  # Railway's edge: not our JSON envelope
                if len(keys) == 2:
                    return 429, {"success": False, "error": "overloaded", "message": "shedding"}
                return self.ACCEPTED
            return self.DONE

        kc = self.serve(route)
        res = kc.solve("6Lc", "https://example.com", "login", deadline=5)
        self.assertEqual(res["token"], "tok-r")
        self.assertEqual(len(keys), 3)
        self.assertEqual(len(set(keys)), 1, "every retry must carry the key of the first attempt")
        uuid.UUID(keys[0], version=4)  # generated, and a v4 UUID

    def test_submit_keeps_the_callers_idempotency_key_across_retries(self):
        keys = []

        def route(method, path, body, headers):
            if method == "POST":
                keys.append(headers.get("idempotency-key"))
                return (503, {"success": False, "error": "no_capacity"}) if len(keys) < 3 else self.ACCEPTED
            return self.DONE

        kc = self.serve(route)
        kc.solve("6Lc", "https://example.com", "login", idempotency_key="order-9", deadline=5)
        self.assertEqual(keys, ["order-9", "order-9", "order-9"])

    def test_submit_5xx_solve_failed_is_transient_on_v2(self):
        # On /v2 the verdict arrives through the poll body; a 5xx on the submit is a hop that
        # failed before the job had one, so it is retried (under the key) rather than raised.
        posts = {"n": 0}

        def route(method, path, body, headers):
            if method == "POST":
                posts["n"] += 1
                return (502, {"success": False, "error": "solve_failed"}) if posts["n"] == 1 else self.ACCEPTED
            return self.DONE

        kc = self.serve(route)
        self.assertEqual(kc.solve("6Lc", "https://example.com", "login", deadline=5)["token"], "tok-r")
        self.assertEqual(posts["n"], 2)

    def test_poll_retries_5xx_until_done(self):
        polls = {"n": 0}

        def route(method, path, body, headers):
            if method == "POST":
                return self.ACCEPTED
            polls["n"] += 1
            if polls["n"] == 1:
                return 503, {"success": False, "error": "solver_unavailable"}
            if polls["n"] == 2:
                return 502, "bad gateway"
            return self.DONE

        kc = self.serve(route)
        res = kc.solve("6Lc", "https://example.com", "login", deadline=5)
        self.assertEqual(res["token"], "tok-r")
        self.assertEqual(polls["n"], 3)

    def test_poll_404_fails_immediately_with_not_found(self):
        polls = {"n": 0}

        def route(method, path, body, headers):
            if method == "POST":
                return self.ACCEPTED
            polls["n"] += 1
            return 404, {"success": False, "error": "not_found", "message": "no such job", "request_id": "req-404"}

        kc = self.serve(route)
        with self.assertRaises(KagedCapError) as cm:
            kc.solve("6Lc", "https://example.com", "login", deadline=5)
        self.assertEqual(cm.exception.code, "not_found")
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(cm.exception.request_id, "req-404")
        self.assertEqual(cm.exception.attempts, 1)
        self.assertEqual(polls["n"], 1)

    def test_submit_400_fails_immediately_without_retry(self):
        posts = {"n": 0}

        def route(method, path, body, headers):
            posts["n"] += 1
            return 400, {"success": False, "error": "validation_error", "message": "sitekey is required"}

        kc = self.serve(route)
        with self.assertRaises(KagedCapError) as cm:
            kc.solve("6Lc", "https://example.com", "login", deadline=5)
        self.assertEqual(cm.exception.code, "validation_error")
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(posts["n"], 1)

    def test_terminal_code_on_a_transient_status_is_not_retried(self):
        posts = {"n": 0}

        def route(method, path, body, headers):
            posts["n"] += 1
            return 503, {"success": False, "error": "insufficient_funds"}

        kc = self.serve(route)
        with self.assertRaises(KagedCapError) as cm:
            kc.solve("6Lc", "https://example.com", "login", deadline=5)
        self.assertEqual(cm.exception.code, "insufficient_funds")
        self.assertEqual(posts["n"], 1)

    def test_constant_429_gives_up_after_the_deadline_and_says_so(self):
        posts = {"n": 0}
        keys = set()

        def route(method, path, body, headers):
            posts["n"] += 1
            keys.add(headers.get("idempotency-key"))
            time.sleep(0.02)  # a real origin round trip, so a trailing attempt can be cut short
            return 429, {"success": False, "error": "rate_limited", "message": "too many requests", "request_id": "req-429"}

        kc = self.serve(route)
        deadline = 1.0
        started = time.monotonic()
        with self.assertRaises(KagedCapError) as cm:
            kc.solve("6Lc", "https://example.com", "login", deadline=deadline)
        elapsed = time.monotonic() - started
        err = cm.exception
        # The wait ends when the deadline has too little left for another attempt.
        self.assertGreaterEqual(elapsed, deadline - MIN_ATTEMPT_BUDGET - 0.05)
        self.assertLess(elapsed, deadline + 1.0)
        self.assertGreater(posts["n"], 1)
        self.assertEqual(err.attempts, posts["n"])
        self.assertEqual(err.status, 429)
        self.assertEqual(err.code, "rate_limited")
        self.assertEqual(err.request_id, "req-429")
        self.assertIn("429", str(err))
        self.assertIn(f"{posts['n']} attempts", str(err))
        self.assertIn("too many requests", str(err))
        self.assertEqual(keys, {err.idempotency_key})

    def test_a_trailing_attempt_cut_short_by_the_deadline_does_not_replace_the_last_real_answer(self):
        posts = {"n": 0}

        def route(method, path, body, headers):
            posts["n"] += 1
            if posts["n"] == 1:
                time.sleep(0.02)
                return 429, {"success": False, "error": "rate_limited", "message": "shed", "request_id": "req-1"}
            time.sleep(3.0)  # longer than what the deadline has left: the client gives up on it
            return 429, {"success": False, "error": "rate_limited", "message": "late"}

        kc = self.serve(route)
        started = time.monotonic()
        with self.assertRaises(KagedCapError) as cm:
            kc.solve("6Lc", "https://example.com", "login", deadline=0.8)
        elapsed = time.monotonic() - started
        err = cm.exception
        self.assertLess(elapsed, 2.0)
        self.assertEqual(posts["n"], 2)
        self.assertEqual(err.attempts, 2)  # the cut-short attempt is counted ...
        self.assertEqual((err.status, err.code, err.request_id), (429, "rate_limited", "req-1"))  # ... but not reported
        self.assertIn("2 attempts", str(err))
        self.assertIn("shed", str(err))

    def test_no_attempt_is_started_with_less_than_half_a_second_of_deadline_left(self):
        posts = {"n": 0}

        def route(method, path, body, headers):
            posts["n"] += 1
            return 429, {"success": False, "error": "rate_limited"}

        kc = self.serve(route)
        with self.assertRaises(KagedCapError) as cm:
            kc.solve("6Lc", "https://example.com", "login", deadline=0.4)
        # One attempt went out (a caller's request is always sent once); no retry fitted.
        self.assertEqual(posts["n"], 1)
        self.assertEqual(cm.exception.attempts, 1)
        self.assertEqual(cm.exception.status, 429)
        self.assertIn("too little for another attempt", str(cm.exception))

    def test_transport_failure_is_retried_until_the_deadline(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()  # nothing listens here any more: every attempt is refused
        kc = KagedCapClient("test-key", base_url=f"http://127.0.0.1:{port}", retry_policy=FAST_RETRY)
        with self.assertRaises(KagedCapError) as cm:
            kc.solve("6Lc", "https://example.com", "login", deadline=1.0)
        self.assertEqual(cm.exception.code, "network_error")
        self.assertEqual(cm.exception.status, 0)
        self.assertGreater(cm.exception.attempts, 1)

    def test_get_solve_keeps_the_last_real_answer_when_its_budget_runs_out(self):
        gets = {"n": 0}

        def route(method, path, body, headers):
            gets["n"] += 1
            if gets["n"] == 1:
                return 500, {"success": False, "error": "internal_error", "request_id": "req-500"}
            time.sleep(3.0)  # outlives the budget; the attempt's own timeout shrinks with it
            return 200, {"success": True, "id": "job-r", "status": "running"}

        kc = self.serve(route)
        started = time.monotonic()
        with self.assertRaises(KagedCapError) as cm:
            kc.get_solve("job-r", timeout=0.8)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual((cm.exception.status, cm.exception.code, cm.exception.request_id), (500, "internal_error", "req-500"))
        self.assertEqual(cm.exception.attempts, 2)

    def test_a_keyboard_interrupt_is_not_swallowed_by_the_retry_loop(self):
        def route(method, path, body, headers):
            return 429, {"success": False, "error": "rate_limited"}

        kc = self.serve(route)

        class _InterruptingTime:
            monotonic = staticmethod(time.monotonic)

            @staticmethod
            def sleep(_seconds):
                raise KeyboardInterrupt

        with self.subTest("during the backoff sleep"):
            with mock.patch.object(client_module, "time", _InterruptingTime):
                with self.assertRaises(KeyboardInterrupt):
                    kc.solve("6Lc", "https://example.com", "login", deadline=5)
        with self.subTest("during the request"):
            with mock.patch.object(client_module.urllib.request, "urlopen", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    kc.solve("6Lc", "https://example.com", "login", deadline=5)

    def test_non_object_json_error_bodies_are_an_empty_envelope_and_retried(self):
        keys = []
        # Valid JSON, none of it our envelope: a quoted string, null, a list, a number.
        bodies = ['"rate limited"', "null", "[]", "7"]

        def route(method, path, body, headers):
            if method == "POST":
                keys.append(headers.get("idempotency-key"))
                if len(keys) <= len(bodies):
                    return 429, bodies[len(keys) - 1]
                return self.ACCEPTED
            return self.DONE

        kc = self.serve(route)
        res = kc.solve("6Lc", "https://example.com", "login", deadline=5)
        self.assertEqual(res["token"], "tok-r")
        self.assertEqual(len(keys), len(bodies) + 1)
        self.assertEqual(len(set(keys)), 1)

    def test_a_2xx_that_is_not_a_json_object_is_a_kagedcap_error_not_a_crash(self):
        for label, answer in [("quoted string", (202, '"accepted"')), ("null", (202, "null")), ("html", (200, "<html>ok</html>"))]:
            with self.subTest(label):
                posts = {"n": 0}

                def route(method, path, body, headers, answer=answer):
                    posts["n"] += 1
                    return answer

                kc = self.serve(route)
                with self.assertRaises(KagedCapError) as cm:
                    kc.solve("6Lc", "https://example.com", "login", deadline=5)
                self.assertEqual(cm.exception.status, answer[0])
                self.assertEqual(cm.exception.code, "error")
                self.assertIn("not a JSON object", str(cm.exception))
                self.assertEqual(posts["n"], 1)  # a 2xx is never retried

    def test_retry_after_header_is_honoured(self):
        posts = {"n": 0}

        def route(method, path, body, headers):
            if method == "POST":
                posts["n"] += 1
                if posts["n"] == 1:
                    return 429, {"success": False, "error": "rate_limited"}, {"Retry-After": "2"}
                return self.ACCEPTED
            return self.DONE

        kc = self.serve(route)
        started = time.monotonic()
        res = kc.solve("6Lc", "https://example.com", "login", deadline=10)
        elapsed = time.monotonic() - started
        self.assertEqual(res["token"], "tok-r")
        self.assertEqual(posts["n"], 2)
        self.assertGreaterEqual(elapsed, 1.8, "the retry must wait the Retry-After, not the ms backoff")
        self.assertLess(elapsed, 4.0)

    def test_backoff_is_capped_and_jittered(self):
        policy = RetryPolicy()  # production schedule: 250ms doubling to a 5s cap
        for attempt in range(1, 16):
            ceiling = min(0.25 * 2 ** (attempt - 1), 5.0)
            samples = [policy.delay(attempt) for _ in range(200)]
            self.assertTrue(all(MIN_BACKOFF <= s <= ceiling for s in samples), f"attempt {attempt} left [{MIN_BACKOFF}, {ceiling}]")
            self.assertGreater(len(set(samples)), 1, f"attempt {attempt} is not jittered")
        self.assertLessEqual(max(policy.delay(1000) for _ in range(50)), 5.0)
        # Retry-After replaces the backoff, capped at 10s; an unusable value falls back to it.
        self.assertEqual(policy.delay(1, retry_after=2.0), 2.0)
        self.assertEqual(policy.delay(1, retry_after=60.0), 10.0)
        self.assertLessEqual(policy.delay(1, retry_after=0.0), 0.25)

    def test_no_wait_is_ever_shorter_than_50ms(self):
        cases = {
            "Retry-After 0": lambda: RetryPolicy().delay(1, retry_after=0.0),
            "Retry-After 1ms": lambda: RetryPolicy().delay(1, retry_after=0.001),
            "Retry-After negative": lambda: RetryPolicy().delay(1, retry_after=-3.0),
            "Retry-After NaN": lambda: RetryPolicy().delay(1, retry_after=float("nan")),
            "Retry-After -inf": lambda: RetryPolicy().delay(1, retry_after=float("-inf")),
            "max_retry_after 0 ignores Retry-After": lambda: RetryPolicy(max_retry_after=0).delay(1, retry_after=2.0),
            "ms backoff": lambda: FAST_RETRY.delay(1),
            "ms backoff, late attempt": lambda: FAST_RETRY.delay(20),
            "zero initial_backoff": lambda: RetryPolicy(initial_backoff=0).delay(1),
            "negative initial_backoff": lambda: RetryPolicy(initial_backoff=-1).delay(1),
            "zero max_backoff": lambda: RetryPolicy(max_backoff=0).delay(1),
        }
        for label, sample in cases.items():
            with self.subTest(label):
                waits = [sample() for _ in range(100)]
                self.assertTrue(all(w >= MIN_BACKOFF for w in waits), f"{label}: {min(waits)}")
                self.assertTrue(all(w == w and w < float("inf") for w in waits), f"{label}: not finite")
        # Retry-After inf is capped at the policy's max, not passed through.
        self.assertEqual(RetryPolicy().delay(1, retry_after=float("inf")), 10.0)
        # A 0/negative backoff is the default, not an error; the jitter window is [50ms, backoff].
        self.assertEqual(RetryPolicy(initial_backoff=0).initial_backoff, 0.25)
        self.assertEqual(RetryPolicy(initial_backoff=-0.5, max_backoff=-1).max_backoff, 5.0)
        self.assertTrue(all(MIN_BACKOFF <= RetryPolicy(max_retry_after=0).delay(1, 2.0) <= 0.25 for _ in range(50)))

    def test_retry_after_is_clamped_before_any_arithmetic(self):
        in_an_hour = format_datetime(datetime.now(timezone.utc) + timedelta(hours=1), usegmt=True)
        long_ago = "Thu, 01 Jan 1970 00:00:00 GMT"
        before_epoch = "Fri, 01 Jan 1960 00:00:00 GMT"
        far_future = "Fri, 31 Dec 9999 23:59:59 GMT"
        cases = {
            "2": 2.0,
            " 30 ": 30.0,
            "0": 0.0,
            "-5": 0.0,
            "nan": None,
            "inf": 86400.0,
            "1e400": 86400.0,
            "9" * 40: 86400.0,
            "999999": 86400.0,
            "abc": None,
            "2;foo": None,
            "": None,
            None: None,
            long_ago: 0.0,
            before_epoch: None,
            far_future: 86400.0,
            "Sat, 01 Jan 10000 00:00:00 GMT": None,
        }
        for value, expected in cases.items():
            with self.subTest(repr(value)):
                self.assertEqual(_parse_retry_after(value), expected)
        soon = _parse_retry_after(in_an_hour)
        self.assertTrue(3590 <= soon <= 3600, soon)

    def test_retry_after_of_zero_or_garbage_falls_back_to_the_floored_backoff(self):
        posts = {"n": 0}
        seen = []

        def route(method, path, body, headers):
            if method == "POST":
                posts["n"] += 1
                seen.append(time.monotonic())
                if posts["n"] == 1:
                    return 429, {"success": False, "error": "rate_limited"}, {"Retry-After": "0"}
                if posts["n"] == 2:
                    return 429, {"success": False, "error": "rate_limited"}, {"Retry-After": "garbage"}
                return self.ACCEPTED
            return self.DONE

        kc = self.serve(route)
        self.assertEqual(kc.solve("6Lc", "https://example.com", "login", deadline=5)["token"], "tok-r")
        self.assertEqual(posts["n"], 3)
        gaps = [b - a for a, b in zip(seen, seen[1:])]
        self.assertTrue(all(g >= MIN_BACKOFF - 0.01 for g in gaps), gaps)

    def test_the_exposed_retry_after_is_capped_at_24_hours(self):
        def route(method, path, body, headers):
            return 429, {"success": False, "error": "rate_limited"}, {"Retry-After": "999999"}

        kc = self.serve(route, retry_policy=RetryPolicy(enabled=False))
        with self.assertRaises(KagedCapError) as cm:
            kc.solve("6Lc", "https://example.com", "login", deadline=5)
        self.assertEqual(cm.exception.retry_after, 86400.0)

    def test_every_terminal_code_is_raised_at_once_whatever_status_carries_it(self):
        terminal = [
            ("validation_error", 400), ("unauthorized", 401), ("key_revoked", 401),
            ("account_suspended", 403), ("insufficient_funds", 402), ("key_spend_cap_reached", 402),
            ("proxy_invalid", 400), ("proxy_required", 400), ("proxy_not_allowed", 400),
            ("unsupported_task", 400), ("action_required", 400), ("callback_url_invalid", 400),
            ("not_found", 404), ("idempotency_conflict", 409), ("key_frozen", 429),
            ("subscription_quota_exhausted", 402), ("subscription_past_due", 402), ("host_not_allowed", 403),
        ]
        for code, own_status in terminal:
            # Its own status, and the transient statuses that could carry it: the code decides.
            for status in (own_status, 429, 503):
                with self.subTest(f"{code} on {status}"):
                    posts = {"n": 0}

                    def route(method, path, body, headers, code=code, status=status):
                        posts["n"] += 1
                        return status, {"success": False, "error": code}

                    kc = self.serve(route)
                    with self.assertRaises(KagedCapError) as cm:
                        kc.solve("6Lc", "https://example.com", "login", deadline=5)
                    self.assertEqual(cm.exception.code, code)
                    self.assertEqual(cm.exception.status, status)
                    self.assertEqual(posts["n"], 1)

    def test_max_attempts_caps_the_retries(self):
        posts = {"n": 0}

        def route(method, path, body, headers):
            posts["n"] += 1
            return 503, {"success": False, "error": "no_capacity"}

        kc = self.serve(route, retry_policy=RetryPolicy(max_attempts=3, initial_backoff=0.001, max_backoff=0.005))
        with self.assertRaises(KagedCapError) as cm:
            kc.solve("6Lc", "https://example.com", "login", deadline=5)
        self.assertEqual(posts["n"], 3)
        self.assertEqual(cm.exception.attempts, 3)
        self.assertEqual(cm.exception.code, "no_capacity")

    def test_retries_can_be_disabled(self):
        posts = {"n": 0}

        def route(method, path, body, headers):
            posts["n"] += 1
            return 429, "rate limited"

        kc = self.serve(route, retry_policy=RetryPolicy(enabled=False))
        with self.assertRaises(KagedCapError) as cm:
            kc.solve("6Lc", "https://example.com", "login", deadline=5)
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(posts["n"], 1)

    # --- the synchronous /solve (v1): every call goes out under an Idempotency-Key (the
    # caller's, or a UUIDv4 minted per call); the gateway claims it first, so only a failure
    # that never reached the gateway is resent, and never past 100s from the first attempt ---

    def test_sync_solve_without_a_key_mints_one_uuid_and_retries_an_edge_page_under_it(self):
        calls = []

        def route(method, path, body, headers):
            calls.append(headers.get("idempotency-key"))
            if len(calls) == 1:
                return 503, "<html><body>503 Service Unavailable</body></html>"  # the edge's own page
            return 200, {"success": True, "x_kpsdk_ct": "ct"}

        kc = self.serve(route)
        res = kc.kasada_login(proxy="http://u:p@1.2.3.4:8080")
        self.assertEqual(res["x_kpsdk_ct"], "ct")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(set(calls)), 1, "the retry must carry the key minted for the first attempt")
        self.assertEqual(uuid.UUID(calls[0]).version, 4)

    def test_sync_solve_without_a_key_does_not_retry_a_transient_the_gateway_answered(self):
        calls = []

        def route(method, path, body, headers):
            calls.append(headers.get("idempotency-key"))
            return 429, {"success": False, "error": "overloaded", "message": "shedding", "request_id": "req-o"}

        kc = self.serve(route)
        with self.assertRaises(KagedCapError) as cm:
            kc.kasada_login(proxy="http://u:p@1.2.3.4:8080")
        err = cm.exception
        self.assertEqual(len(calls), 1)
        self.assertEqual((err.status, err.code, err.request_id), (429, "overloaded", "req-o"))
        self.assertEqual(err.attempts, 1)
        self.assertEqual(err.idempotency_key, calls[0])  # the minted key, for support
        self.assertEqual(uuid.UUID(err.idempotency_key).version, 4)

    def test_sync_solve_does_not_retry_past_100s_from_the_first_attempt(self):
        clock = _FakeClock()
        calls = []

        def route(method, path, body, headers):
            calls.append(headers.get("idempotency-key"))
            clock.advance(60.0)  # a minute at the edge before it answers, every time
            return 503, "<html><body>503 Service Unavailable</body></html>", {"Retry-After": "30"}

        kc = self.serve(route)
        kc.timeout = 1000.0  # the client's own budget is not what ends this wait
        with mock.patch.object(client_module, "time", clock):
            with self.assertRaises(KagedCapError) as cm:
                kc.kasada_login(proxy="http://u:p@1.2.3.4:8080")
        err = cm.exception
        # 60s in: retried. 120s in: past the window — the gateway's claim on the key is about
        # to lapse, and a resend would be a fresh, billed solve — so the 503 is the verdict.
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(set(calls)), 1)
        self.assertEqual(err.attempts, 2)
        self.assertEqual(err.status, 503)
        self.assertEqual(err.idempotency_key, calls[0])
        self.assertIn("2 attempts", str(err))
        self.assertIn("over 130.0s", str(err))  # elapsed since the first attempt
        self.assertIn("130s since the first attempt", str(err))
        self.assertIn("100s window", str(err))
        self.assertIn("HTTP 503", str(err))
        # 60s attempt, the Retry-After capped to 10s, 60s attempt — and none of the second
        # Retry-After: once the window has passed there is no attempt left to wait for.
        self.assertEqual(clock.now - 1000.0, 130.0)

    def test_sync_solve_under_a_key_retries_the_edges_own_pages_with_the_same_key(self):
        calls = []

        def route(method, path, body, headers):
            calls.append(headers.get("idempotency-key"))
            if len(calls) == 1:
                return 429, "rate limited"  # Railway
            if len(calls) == 2:
                return 502, "<html><body>502 Bad Gateway</body></html>"  # Cloudflare
            if len(calls) == 3:
                return 503, '"service unavailable"'  # JSON, but not an object: still not ours
            return 200, {"success": True, "x_kpsdk_ct": "ct"}

        kc = self.serve(route)
        res = kc.kasada_login(proxy="http://u:p@1.2.3.4:8080", idempotency_key="kas-1")
        self.assertEqual(res["x_kpsdk_ct"], "ct")
        self.assertEqual(calls, ["kas-1"] * 4)

    def test_sync_solve_under_a_key_retries_a_dropped_connection(self):
        calls = []

        def route(method, path, body, headers):
            calls.append(headers.get("idempotency-key"))
            if len(calls) == 1:
                return None  # closed without a response
            return 200, {"success": True, "x_kpsdk_ct": "ct"}

        kc = self.serve(route)
        res = kc.kasada_login(proxy="http://u:p@1.2.3.4:8080", idempotency_key="kas-1")
        self.assertEqual(res["x_kpsdk_ct"], "ct")
        self.assertEqual(calls, ["kas-1", "kas-1"])

    def test_sync_solve_under_a_key_does_not_retry_a_transient_the_gateway_answered(self):
        for code, status in [("solver_unavailable", 503), ("no_capacity", 503), ("overloaded", 429),
                             ("rate_limited", 429), ("internal_error", 500), ("solve_failed", 502), ("solve_timeout", 504)]:
            with self.subTest(f"{status} {code}"):
                calls = {"n": 0}

                def route(method, path, body, headers, code=code, status=status):
                    calls["n"] += 1
                    return status, {"success": False, "error": code, "message": "from the gateway", "request_id": "req-g"}

                kc = self.serve(route)
                with self.assertRaises(KagedCapError) as cm:
                    kc.kasada_login(proxy="http://u:p@1.2.3.4:8080", idempotency_key="kas-2")
                self.assertEqual((cm.exception.status, cm.exception.code), (status, code))
                self.assertEqual(cm.exception.request_id, "req-g")
                self.assertEqual(cm.exception.attempts, 1)
                self.assertEqual(calls["n"], 1)

    def test_sync_solve_under_a_key_does_not_retry_a_non_json_500(self):
        # 500 is not one of the statuses the edge makes on its own; a bare one is raised as is.
        calls = {"n": 0}

        def route(method, path, body, headers):
            calls["n"] += 1
            return 500, "internal server error"

        kc = self.serve(route)
        with self.assertRaises(KagedCapError) as cm:
            kc.kasada_login(proxy="http://u:p@1.2.3.4:8080", idempotency_key="kas-3")
        self.assertEqual(cm.exception.status, 500)
        self.assertEqual(calls["n"], 1)

    def test_sync_solve_retry_that_meets_409_surfaces_the_last_transient_error(self):
        calls = []

        def route(method, path, body, headers):
            calls.append(headers.get("idempotency-key"))
            if len(calls) == 1:
                return 502, "bad gateway"  # the edge — but the request had reached the gateway
            return 409, {"success": False, "error": "idempotency_conflict", "message": "already in flight"}

        kc = self.serve(route)
        with self.assertRaises(KagedCapError) as cm:
            kc.kasada_login(proxy="http://u:p@1.2.3.4:8080", idempotency_key="kas-4")
        err = cm.exception
        self.assertEqual(calls, ["kas-4", "kas-4"])
        self.assertEqual((err.status, err.code), (502, "error"))  # the 502, not the 409
        self.assertEqual(err.attempts, 2)
        self.assertEqual(err.idempotency_key, "kas-4")
        self.assertIn("2 attempts", str(err))
        self.assertIn("409 idempotency_conflict", str(err))
        self.assertIn("HTTP 502", str(err))

    def test_sync_solve_first_answer_409_is_raised_as_itself(self):
        # Only a 409 that a RETRY earned is translated; a 409 on the first send is the caller's.
        def route(method, path, body, headers):
            return 409, {"success": False, "error": "idempotency_conflict"}

        kc = self.serve(route)
        with self.assertRaises(KagedCapError) as cm:
            kc.kasada_login(proxy="http://u:p@1.2.3.4:8080", idempotency_key="kas-5")
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "idempotency_conflict"))
        self.assertEqual(cm.exception.attempts, 1)

    def test_standalone_primitives_retry_within_their_timeout(self):
        hits = {"POST": 0, "GET": 0}

        def route(method, path, body, headers):
            hits[method] += 1
            if hits[method] == 1:
                return 500, {"success": False, "error": "internal_error"}
            return self.ACCEPTED if method == "POST" else self.DONE

        kc = self.serve(route)
        sub = kc.submit_solve("6Lc", "https://example.com", "login", timeout=5)
        self.assertEqual(sub["id"], "job-r")
        self.assertEqual(kc.get_solve(sub["id"], timeout=5)["status"], "done")
        self.assertEqual(hits, {"POST": 2, "GET": 2})


if __name__ == "__main__":
    unittest.main()
