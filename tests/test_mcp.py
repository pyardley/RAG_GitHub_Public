"""The MCP server speaks JSON-RPC and calls the running app."""

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))

import server  # noqa: E402


class _App(BaseHTTPRequestHandler):
    seen: list[tuple[str, str, dict | None]] = []

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            self._json({
                "repositories": [{"name": "hmcts/hwf-publicapp", "branch": "master", "commit_sha": "abc", "files": 1, "chunks": 2}],
                "model": {"name": "gpt-4o-mini"},
                "started": "2026-11-09 17:00 UTC",
                "ingest": {"running": False, "repo": None, "log": "a very long ingest log", "error": None, "progress": None},
            })
            return
        if path == "/api/default-branch":
            repo = parse_qs(urlparse(self.path).query).get("repo", [""])[0]
            self._record("GET", path, {"repo": repo})
            self._json({"branch": "master"})
            return
        self._json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else {}
        path = urlparse(self.path).path
        self._record("POST", path, body)
        if path == "/api/query":
            self._json({
                "repositories": [{"name": "hmcts/hwf-publicapp"}],
                "queries": [body["question"]],
                "truncated": False,
                "passage_limit": body.get("k"),
                "hits": [{
                    "repo": "hmcts/hwf-publicapp",
                    "path": "app/models/fee.rb",
                    "start_line": 1,
                    "end_line": 4,
                    "symbol": "Fee#save",
                    "content": "x" * 1600,
                    "score": 0.8,
                }],
                "matches": [],
                "answer": "written" if body.get("answer") else None,
            })
            return
        if path == "/api/impact":
            self._json({
                "repositories": [{"name": "hmcts/et-full-system"}],
                "definitions": [{
                    "repo": "hmcts/et-full-system",
                    "path": "spec/factories/claims.rb",
                    "start_line": 1,
                    "end_line": 3,
                    "symbol": "factory :claim",
                    "content": "factory :claim do\nend\n",
                }],
                "references": [{
                    "repo": "hmcts/et-full-system",
                    "path": "spec/models/claim_spec.rb",
                    "start_line": 8,
                    "end_line": 10,
                    "symbol": "builds a claim",
                    "content": "build(:claim)",
                    "via": "builds a claim → factory :claim",
                }],
                "gaps": [],
                "related": [],
                "diagram": {"nodes": [], "edges": [{"from": "builds a claim", "to": "factory :claim"}]},
                "definition_count": 1,
                "reference_count": 1,
                "callers_omitted": 0,
                "callers_capped": False,
                "truncated": False,
            })
            return
        if path == "/api/remove":
            self._json({"error": "should not be called"}, 500)
            return
        self._json({"error": "Not found"}, 404)

    def _record(self, method: str, path: str, body: dict | None) -> None:
        type(self).seen.append((method, path, body))

    def _json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args) -> None:
        return


class McpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _App.seen = []
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _App)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        os.environ["CODE_RAG_URL"] = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()

    def test_initialize_names_the_tools(self) -> None:
        reply = server.handle({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        })
        self.assertEqual(reply["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(reply["result"]["serverInfo"]["name"], "code-rag")
        listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [tool["name"] for tool in listed["result"]["tools"]]
        self.assertEqual(
            names,
            ["repositories", "query", "search", "impact", "default_branch", "ingest", "remove_index"],
        )

    def test_a_notification_has_no_reply(self) -> None:
        self.assertIsNone(server.handle({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }))

    def test_query_returns_a_shortened_passage_and_does_not_ask_for_an_answer(self) -> None:
        _App.seen.clear()
        reply = server.handle({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "query", "arguments": {"question": "where is the fee saved", "repo": "hmcts/hwf-publicapp"}},
        })
        text = json.loads(reply["result"]["content"][0]["text"])
        self.assertNotIn("isError", reply["result"])
        self.assertEqual(text["passages"][0]["symbol"], "Fee#save")
        self.assertTrue(text["passages"][0]["content_shortened"])
        self.assertLess(len(text["passages"][0]["content"]), 1600)
        self.assertNotIn("answer", text)
        method, path, body = _App.seen[-1]
        self.assertEqual((method, path), ("POST", "/api/query"))
        self.assertEqual(body["answer"], False)
        self.assertEqual(body["repo"], "hmcts/hwf-publicapp")

    def test_repositories_leave_out_the_ingest_log(self) -> None:
        reply = server.handle({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "repositories", "arguments": {}},
        })
        text = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(text["repositories"][0]["name"], "hmcts/hwf-publicapp")
        self.assertNotIn("log", text["ingest"])

    def test_impact_includes_the_call_edge(self) -> None:
        reply = server.handle({
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "impact", "arguments": {"target": "FactoryBot.create(:claim)", "repo": "hmcts/et-full-system"}},
        })
        text = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(text["definitions"][0]["symbol"], "factory :claim")
        self.assertEqual(text["references"][0]["via"], "builds a claim → factory :claim")
        self.assertEqual(text["diagram"]["edges"], [{"from": "builds a claim", "to": "factory :claim"}])

    def test_remove_without_confirm_does_not_call_the_app(self) -> None:
        before = len(_App.seen)
        reply = server.handle({
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "remove_index", "arguments": {"repo": "hmcts/hwf-publicapp"}},
        })
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("confirm", reply["result"]["content"][0]["text"])
        self.assertEqual(len(_App.seen), before)

    def test_stdio_answers_initialize(self) -> None:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "mcp" / "server.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "CODE_RAG_URL": os.environ["CODE_RAG_URL"]},
        )
        try:
            process.stdin.write(json.dumps({
                "jsonrpc": "2.0",
                "id": 7,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }) + "\n")
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            process.stdin.flush()
            line = process.stdout.readline()
            reply = json.loads(line)
            self.assertEqual(reply["id"], 7)
            self.assertEqual(reply["result"]["serverInfo"]["name"], "code-rag")
            process.stdin.close()
            rest, _err = process.communicate(timeout=5)
            self.assertEqual(rest, "")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
