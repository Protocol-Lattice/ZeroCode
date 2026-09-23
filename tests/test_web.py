"""Native web_fetch with local HTTP fixtures; no paid API or internet calls."""

import gzip
import http.server
import json
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import unittest

from tests import test_agent as agent
from tests.test_agent import MockAPI, Terminal, environment, function_tools, reply
from tests.test_parallel import results


class WebServer:
    def __init__(self, routes):
        self.routes = routes
        self.requests = []
        self.started = threading.Event()
        self.release = threading.Event()
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                fixture.requests.append((self.path, dict(self.headers)))
                status, headers, body = fixture.routes.get(
                    self.path, (404, {"Content-Type": "text/plain"}, b"Missing"))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.path == "/slow":
                    fixture.started.set()
                    fixture.release.wait(8)
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                    pass  # A bounded fetch, timeout or cancellation closes early.

            def log_message(self, *_args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def resource(body, content_type="text/plain; charset=utf-8"):
    return (200, {"Content-Type": content_type}, body.encode() if isinstance(body, str) else body)


class WebTests(unittest.TestCase):
    run_agent = agent.AgentTests.run_agent

    def fetch(self, args, provider="openrouter", extra=()):
        with MockAPI([reply(provider, calls=[("web_fetch", args)]), reply(provider, "Fetched.")]) as api:
            run = self.run_agent(api, provider=provider, extra=("--no-skills", *extra))
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual(len(api.requests), 2, run.stdout)
            found = results(api.requests[1][1], provider)
            self.assertEqual(len(found), 1)
            self.assertNotIn("PROPOSED COMMAND", run.stdout)
            return found[0][1], run

    def test_all_providers_advertise_and_fetch_without_approval_or_credentials(self):
        path = "/api?q=%22hello%22&literal=$(id);value"
        text = '{"message":"Hello, żółw 🌍", "ok":true}\n'
        with WebServer({path: resource(text, "application/json")}) as web:
            for provider in ("openrouter", "openai", "claude", "gemini"):
                with self.subTest(provider=provider), MockAPI([
                    reply(provider, calls=[("web_fetch", {"url": web.url + path})]),
                    reply(provider, "Fetched.")]) as api:
                    run = self.run_agent(api, provider, extra=("--no-skills",))
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                    catalog = function_tools(api.requests[0][1], provider)
                    definition = next(t for t in catalog if t["name"] == "web_fetch")
                    schema = definition["input_schema" if provider == "claude" else "parameters"]
                    self.assertEqual(schema["required"], ["url"])
                    self.assertFalse(schema["additionalProperties"])
                    payload = json.loads(results(api.requests[1][1], provider)[0][1])
                    self.assertEqual(payload, {"url": web.url + path, "effective_url": web.url + path,
                                              "status": 200, "content_type": "application/json",
                                              "content": text, "truncated": False})
                    self.assertIn("HTTP 200", run.stdout)
                    self.assertNotIn("PROPOSED COMMAND", run.stdout)
            self.assertEqual(len(web.requests), 4)
            for requested, headers in web.requests:
                self.assertEqual(requested, path)
                self.assertNotIn("test-key-never-render-me", json.dumps(headers))
                self.assertFalse({"authorization", "x-api-key", "cookie"} & {k.lower() for k in headers})

    def test_redirects_gzip_html_and_empty_responses(self):
        html = "<html><body>Public docs: żółw</body></html>"
        routes = {"/redirect": (302, {"Location": "/page"}, b"Redirect body must be discarded"),
                  "/page": (200, {"Content-Type": "text/html", "Content-Encoding": "gzip"},
                            gzip.compress(html.encode())),
                  "/empty": (204, {}, b"")}
        with WebServer(routes) as web:
            raw, _ = self.fetch({"url": web.url + "/redirect"})
            payload = json.loads(raw)
            self.assertEqual(payload["effective_url"], web.url + "/page")
            self.assertEqual(payload["url"], web.url + "/redirect")
            self.assertEqual(payload["content"], html)
            self.assertFalse(payload["truncated"])
            raw, _ = self.fetch({"url": web.url + "/empty"})
            self.assertEqual(json.loads(raw)["content"], "")
            self.assertEqual(json.loads(raw)["status"], 204)

    def test_body_limits_and_utf8_boundaries(self):
        routes = {"/large": resource("a" * 1000000), "/utf8": resource("abc🌍żółw tail"),
                  "/exact": resource("four"), "/escaped": resource("\t\n\"\\" * 5000)}
        with WebServer(routes) as web:
            for path, args, expected, truncated in [
                ("/large", {}, "a" * 8000, True),
                ("/large", {"max_bytes": 12000}, "a" * 12000, True),
                ("/utf8", {"max_bytes": 6}, "abc", True),
                ("/exact", {"max_bytes": 4}, "four", False),
            ]:
                with self.subTest(path=path, args=args):
                    raw, _ = self.fetch({"url": web.url + path, **args})
                    payload = json.loads(raw)
                    self.assertEqual(payload["content"], expected)
                    self.assertEqual(payload["truncated"], truncated)
            raw, _ = self.fetch({"url": web.url + "/escaped", "max_bytes": 12000})
            payload = json.loads(raw)
            self.assertLessEqual(len(raw.encode()), 16000)
            self.assertTrue(payload["truncated"])
            self.assertTrue(payload["content"])
            self.assertTrue(("\t\n\"\\" * 5000).startswith(payload["content"]))

    def test_http_transport_and_content_errors_are_tool_results(self):
        routes = {"/loop": (302, {"Location": "/loop"}, b""),
                  "/local-file": (302, {"Location": "file:///etc/passwd"}, b""),
                  "/credentials": (302, {"Location": "http://user:pass@127.0.0.1/"}, b""),
                  "/binary": resource(b"%PDF", "application/pdf"),
                  "/nul": resource(b"hello\x00hidden"), "/invalid": resource(b"hello\xff"),
                  "/broken-gzip": (200, {"Content-Type": "text/plain", "Content-Encoding": "gzip"}, b"bad")}
        with WebServer(routes) as web:
            for path in ("/missing", *routes):
                with self.subTest(path=path):
                    raw, _ = self.fetch({"url": web.url + path})
                    self.assertTrue(raw.startswith("Error:"), raw)
                    self.assertNotIn("root:", raw)
                    if path == "/missing":
                        self.assertIn("HTTP 404", raw)
            self.assertLessEqual(sum(p == "/loop" for p, _ in web.requests), 6)

    def test_invalid_arguments_and_non_http_urls_make_no_requests(self):
        with WebServer({"/": resource("unexpected")}) as web:
            invalid = [{}, {"url": ""}, {"url": 42}, {"url": "file:///etc/passwd"},
                       {"url": "ftp://example.com/file"}, {"url": "https://"},
                       {"url": web.url + "/\r\nInjected: true"}, {"url": web.url + "/\x00"},
                       {"url": web.url.replace("http://", "http://user:password@")},
                       {"url": web.url + "/" + "a" * 2048}]
            invalid.extend({"url": web.url, "max_bytes": value}
                           for value in (0, 3, 12001, -1, 4.5, "8000", True))
            invalid.extend({"url": web.url, "timeout_seconds": value}
                           for value in (0, 61, -1, 1.5, "30", False))
            with MockAPI([reply("openai", calls=[("web_fetch", args) for args in invalid]),
                          reply("openai", "Invalid requests handled.")]) as api:
                run = self.run_agent(api, "openai", extra=("--no-skills",))
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                found = results(api.requests[1][1], "openai")
                self.assertEqual(len(found), len(invalid))
                self.assertTrue(all(text.startswith("Error:") for _, text in found), found)
                self.assertEqual(web.requests, [])

    def test_timeout_and_connection_failure(self):
        with WebServer({"/slow": resource("Too late")}) as web:
            raw, _ = self.fetch({"url": web.url + "/slow", "timeout_seconds": 1})
            self.assertTrue(raw.startswith("Error:"), raw)
            self.assertRegex(raw.lower(), r"timeout|timed out")
            url = web.url
        raw, _ = self.fetch({"url": url, "timeout_seconds": 1})
        self.assertTrue(raw.startswith("Error:"), raw)
        self.assertIn("connect", raw.lower())

    def test_https_certificate_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            cert, key = Path(folder, "cert.pem"), Path(folder, "key.pem")
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=localhost"],
                           check=True, capture_output=True, timeout=10)
            web = WebServer({"/": resource("Untrusted certificate must not be accepted")})
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            web.server.socket = context.wrap_socket(web.server.socket, server_side=True)
            with web:
                raw, _ = self.fetch({"url": web.url.replace("http:", "https:")})
                self.assertTrue(raw.startswith("Error:"), raw)
                self.assertIn("certificate", raw.lower())
                self.assertEqual(web.requests, [])

    def test_cancellation_then_next_task(self):
        with WebServer({"/slow": resource("Cancelled content")}) as web, MockAPI([
            reply("openai", calls=[("web_fetch", {"url": web.url + "/slow"})]),
            reply("openai", "Fresh task completed.")]) as api:
            terminal = Terminal(["--provider", "openai", "--no-learning"], environment(api.url), rows=40, columns=140)
            try:
                terminal.wait_for("Your terminal.")
                terminal.send("Fetch the page.\r")
                terminal.wait_for("fetching web page")
                self.assertTrue(web.started.wait(3))
                terminal.send(b"\x1b")
                terminal.wait_for("Cancelled.", timeout=3)
                web.release.set()
                terminal.send("A new task.\r")
                terminal.wait_for("Fresh task completed.")
                self.assertNotIn("Cancelled content", json.dumps(api.requests[-1][1]))
            finally:
                self.assertEqual(terminal.close(), terminal.original)

    def test_chat_only_cannot_fetch(self):
        with WebServer({"/": resource("Must not fetch")}) as web, MockAPI([
            reply("openai", calls=[("web_fetch", {"url": web.url})])]) as api:
            run = self.run_agent(api, "openai", extra=("--chat-only",))
            self.assertNotEqual(run.returncode, 0)
            self.assertNotIn("tools", api.requests[0][1])
            self.assertEqual(web.requests, [])
