from contextlib import ExitStack, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
import secrets
from threading import Thread
import unittest
from unittest.mock import Mock, patch
from urllib.request import ProxyHandler, build_opener

from tools import wechat_b2_probe as probe


class ProbeTests(unittest.TestCase):
    def setUp(self):
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        self.output = io.StringIO()
        contexts.enter_context(redirect_stdout(self.output))
        self.sleep = contexts.enter_context(patch.object(probe.time, "sleep"))

    def test_origin_validation(self):
        for url in ("", "http://localhost", "https://u:p@localhost",
                    "https://localhost/a", "https://localhost?x",
                    "https://localhost#x", "https://localhost:65536",
                    "https://localhost\\evil", " https://localhost"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                probe.Client(url)

    def test_health_only_without_token(self):
        with patch.dict(os.environ, {"CALENDAR_SERVER_URL": "https://example.invalid",
                                     "CALENDAR_WORKER_TOKEN": ""}):
            with patch.object(probe.Client, "request", return_value={"status": "ok"}) as request:
                self.assertEqual(probe.main([]), 0)
                request.assert_called_once_with("/")
                self.assertEqual(probe.main(["--isolated-test-queue"]), 1)
                request.assert_called_once()

    def test_claim_uncertainty_waits_for_health_before_new_poll(self):
        client = Mock()
        claim = {"job": {"msg_id": "test-1", "text": "B2_TEST"}}
        client.request.side_effect = [probe.Unavailable(), probe.Unavailable(),
                                      {"status": "ok"}, {"job": None}, claim]
        self.assertEqual(probe.wait_for_job(client, "synthetic"), "test-1")
        paths = [call.args[0] for call in client.request.call_args_list]
        self.assertEqual(paths, [probe.CLAIM_PATH, "/", "/", probe.CLAIM_PATH, probe.CLAIM_PATH])
        self.assertEqual([call.args for call in self.sleep.call_args_list], [(15,)] * 3)
        self.assertIn("processing", self.output.getvalue())

    def test_unmarked_message_and_invalid_claim_are_left_for_inspection(self):
        for result in ({}, {"job": {}}, {"job": {"text": "B2_TESTING private words"}},
                       {"job": {"text": "private words B2_TEST"}},
                       {"job": {"text": "B2_TEST", "msg_id": ".."}}):
            client = Mock()
            client.request.return_value = result
            with self.subTest(result=result), self.assertRaises(probe.ProbeError):
                probe.wait_for_job(client, "synthetic")
            client.request.assert_called_once_with(probe.CLAIM_PATH, "synthetic")
        self.assertNotIn("private words", self.output.getvalue())

    def test_auth_failures_stop(self):
        client = Mock()
        client.request.return_value = {"job": None}
        with self.assertRaises(probe.ProbeError):
            probe.reject_invalid_token(client)
        client.request.side_effect = probe.StatusError(403)
        with self.assertRaises(probe.StatusError):
            probe.wait_for_job(client, "synthetic")
        self.sleep.assert_not_called()

    def test_opt_in_audit_captures_rejected_job_without_message_or_credentials(self):
        client = Mock(url="https://example.invalid")
        client.request.return_value = {"job": {
            "job_id": "test-job", "msg_id": "test-1", "claimed_at": "2026-11-24T08:22:00Z",
            "openid": "fake-sender", "text": "private words",
        }}
        audit = io.StringIO()
        with self.assertRaises(probe.ProbeError):
            probe.wait_for_job(client, "synthetic", audit)
        record = json.loads(audit.getvalue())
        self.assertTrue(record.pop("observed_at_utc").endswith("+00:00"))
        self.assertEqual(record, {
            "server_url": "https://example.invalid", "job_id": "test-job",
            "msg_id": "test-1", "claimed_at": "2026-11-24T08:22:00Z",
        })

    def test_polling_interrupt_exits_cleanly(self):
        with patch.dict(os.environ, {"CALENDAR_SERVER_URL": "https://example.invalid",
                                     "CALENDAR_WORKER_TOKEN": "synthetic"}):
            with patch.object(probe.Client, "request", side_effect=[
                    {"status": "ok"}, probe.StatusError(401), {"job": None}]):
                self.sleep.side_effect = KeyboardInterrupt
                self.assertEqual(probe.main(["--isolated-test-queue"]), 130)

    def test_complete_timeout_and_service_failure_retry_identical_request(self):
        client = Mock()
        saved = {"status": "done", "exec_ok": True, "operation_id": None,
                 "already_completed": True, "notify_status": "sent"}
        client.request.side_effect = [probe.Unavailable(), probe.StatusError(503), saved]
        self.assertEqual(probe.complete(client, "synthetic", "test-1"), saved)
        self.assertEqual([c.args for c in client.request.call_args_list],
                         [("/api/jobs/test-1/complete", "synthetic", probe.RECEIPT)] * 3)
        self.assertEqual([c.args for c in self.sleep.call_args_list], [(15,), (15,)])
        for status in (404, 409):
            client.request.side_effect = probe.StatusError(status)
            with self.assertRaises(probe.StatusError):
                probe.complete(client, "synthetic", "test-1")

    def test_failed_notification_reported_after_saved_receipt(self):
        client = Mock()
        result = {"status": "done", "exec_ok": True, "operation_id": None,
                  "already_completed": False, "notify_status": "failed"}
        client.request.side_effect = [{"status": "ok"}, probe.StatusError(403),
                                      {"job": {"msg_id": "a", "text": "B2_TEST"}},
                                      result, dict(result, already_completed=True)]
        with self.assertRaisesRegex(probe.ProbeError, "回执已保存.*failed"):
            probe.run(client, "synthetic")
        self.assertEqual(client.request.call_count, 5)

    def test_loopback_protocol_and_lost_completion_response(self):
        token = secrets.token_hex(24)
        claims = []
        receipts = []
        delivered = []
        routes = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, body):
                self.send_response(status)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_GET(self):
                routes.append(self.path)
                self.respond(200, {"status": "ok"} if not self.headers.get("X-Worker-Token") else {})

            def do_POST(self):
                routes.append(self.path)
                if self.headers.get("X-Worker-Token") != token:
                    self.respond(401, {})
                elif self.path == probe.CLAIM_PATH:
                    claims.append(self.path)
                    job = {"msg_id": "test-1", "text": "B2_TEST private words", "openid": "fake-sender"}
                    self.respond(200, {"job": None if len(claims) == 1 else job})
                elif self.path == "/api/jobs/test-1/complete":
                    receipts.append(self.rfile.read(int(self.headers["Content-Length"])))
                    if len(receipts) == 1:
                        delivered.append(json.loads(receipts[0]))
                        return  # Server committed, then socket closed before the HTTP reply.
                    self.respond(200, {"status": "done", "exec_ok": True, "operation_id": None,
                                       "notify_status": "sent", "already_completed": True})
                elif self.path == "/redirect":
                    self.send_response(307)
                    self.send_header("Location", "/unexpected")
                    self.end_headers()
                else:
                    self.respond(404, {})

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = probe.Client("https://example.invalid")
                # Substitute a local HTTP transport only in this fixture.
                client.url = f"http://127.0.0.1:{server.server_port}"
                client.opener = build_opener(ProxyHandler({}), probe.NoRedirect())
                probe.run(client, token)
                with self.assertRaises(probe.StatusError) as redirect:
                    client.request("/redirect", token)
                self.assertEqual(redirect.exception.status, 307)
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(routes, ["/", *[probe.CLAIM_PATH] * 3, *["/api/jobs/test-1/complete"] * 3,
                                  "/redirect"])
        self.assertEqual(len(claims), 2)
        self.assertEqual(receipts, [probe.RECEIPT] * 3)
        self.assertEqual(delivered, [{"ok": True, "reply_text": "B2_TEST 模拟回执，未修改日历。",
                                      "operation_id": None, "error": None}])
        for private in (token, "private words", "fake-sender", "test-1"):
            self.assertNotIn(private, self.output.getvalue())
