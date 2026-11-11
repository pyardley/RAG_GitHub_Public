"""Model Context Protocol server for the running Code RAG app.

The process speaks newline-delimited JSON-RPC on stdin and stdout. Logs stay
on stderr. Cursor launches this with the project `.cursor/mcp.json`.
"""

from __future__ import annotations

import json
import re
import sys

import api

_PROTOCOL = "2024-11-05"
_VERSION = re.compile(r"\d{4}-\d{2}-\d{2}")
_INSTRUCTIONS = (
    "Code RAG searches indexed GitHub repositories. "
    "Use query for a question, search for exact text, and impact for a class, method, "
    "file, or FactoryBot call such as FactoryBot.create(:claim). "
    "Set repo to owner/name when more than one repository is indexed. "
    "The app must already be running."
)

_TOOLS = [
    {
        "name": "repositories",
        "description": "List the indexed repositories, the model in use, and whether an ingest is running.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "query",
        "description": (
            "Ask the indexed code a question and return the closest passages. "
            "Use this for how something works. Set repo to owner/name to stay in one repository. "
            "Passages are shortened. Leave answer false so you read the passages yourself."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question, in your own words."},
                "repo": {"type": "string", "description": "owner/name. Omit to search every indexed repository."},
                "k": {"type": "integer", "minimum": 1, "maximum": 30, "description": "How many passages to return. Defaults to 8."},
                "answer": {"type": "boolean", "description": "When true, the app's own model also writes a reply."},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search",
        "description": (
            "Find passages that contain the text or identifier. "
            "A quoted phrase is matched exactly. A regular expression inside quotes is used as written."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The text, identifier, or quoted phrase to find."},
                "repo": {"type": "string", "description": "owner/name. Omit to search every indexed repository."},
                "k": {"type": "integer", "minimum": 1, "maximum": 30, "description": "How many passages to return. Defaults to 8."},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
    {
        "name": "impact",
        "description": (
            "Show where a class, method, file, or FactoryBot call is defined and what reaches it. "
            "FactoryBot.create(:claim) resolves to factory :claim. "
            "via is the call path. Diagram edges point from a caller toward the definition. "
            "A ⇢ in via, and possible on a passage or edge, is a call by a name that more than "
            "one class defines, so that caller is possible, not certain."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "A class, method, file, or FactoryBot call."},
                "repo": {"type": "string", "description": "owner/name. Omit to search every indexed repository."},
                "answer": {"type": "boolean", "description": "When true, the app's own model also writes a reply."},
            },
            "required": ["target"],
            "additionalProperties": False,
        },
    },
    {
        "name": "default_branch",
        "description": "Read the default branch name of a GitHub repository before indexing it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/name or a GitHub URL."},
            },
            "required": ["repo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ingest",
        "description": (
            "Start indexing a GitHub repository. The call returns when the index has started, not when it finishes. "
            "Use repositories to see progress. Do not start a second ingest while one is running."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/name or a GitHub URL."},
                "branch": {"type": "string", "description": "Branch to index. Omit for the default branch."},
                "force": {"type": "boolean", "description": "When true, rebuild even if this commit is already indexed."},
            },
            "required": ["repo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "remove_index",
        "description": (
            "Delete one repository index and its local checkout. The other repositories stay. "
            "confirm must be true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/name of the indexed repository."},
                "confirm": {"type": "boolean", "description": "Must be true. This deletes that index and its checkout."},
            },
            "required": ["repo", "confirm"],
            "additionalProperties": False,
        },
    },
]


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        if not isinstance(message, dict):
            continue
        response = handle(message)
        if response is not None:
            _emit(response)
    return 0


def handle(message: dict) -> dict | None:
    method = message.get("method")
    if not isinstance(method, str):
        return _error(message.get("id"), -32600, "Invalid request")
    if method.startswith("notifications/"):
        return None
    result = _dispatch(method, message.get("params") if isinstance(message.get("params"), dict) else {})
    if isinstance(result, dict) and result.get("error"):
        return {"jsonrpc": "2.0", "id": message.get("id"), "error": result["error"]}
    return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}


def _dispatch(method: str, params: dict) -> dict:
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if isinstance(requested, str) and _VERSION.fullmatch(requested) else _PROTOCOL
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "code-rag", "version": "1.0.0"},
            "instructions": _INSTRUCTIONS,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": _TOOLS}
    if method == "resources/list":
        return {"resources": []}
    if method == "prompts/list":
        return {"prompts": []}
    if method == "tools/call":
        return _call(params)
    return {"error": {"code": -32601, "message": f"Method not found: {method}"}}


def _call(params: dict) -> dict:
    name = params.get("name")
    arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    try:
        payload = _tool(name, arguments)
    except api.RagError as exc:
        return _tool_result(str(exc), error=True)
    except Exception as exc:
        print(f"tool {name} failed: {exc}", file=sys.stderr)
        return _tool_result("The tool failed before the app responded.", error=True)
    return _tool_result(json.dumps(payload, indent=2))


def _tool(name: object, arguments: dict) -> dict:
    if name == "repositories":
        return api.repositories()
    if name == "query":
        return api.query(
            arguments.get("question", ""),
            arguments.get("repo"),
            _count(arguments.get("k", 8)),
            bool(arguments.get("answer", False)),
        )
    if name == "search":
        return api.search(arguments.get("question", ""), arguments.get("repo"), _count(arguments.get("k", 8)))
    if name == "impact":
        return api.impact(arguments.get("target", ""), arguments.get("repo"), arguments.get("answer", False))
    if name == "default_branch":
        return api.default_branch(arguments.get("repo", ""))
    if name == "ingest":
        return api.ingest(arguments.get("repo", ""), arguments.get("branch"), arguments.get("force", False))
    if name == "remove_index":
        return api.remove_index(arguments.get("repo", ""), arguments.get("confirm", False))
    raise api.RagError(f"Unknown tool: {name}")


def _count(value: object) -> int:
    return 8 if value is None else value  # type: ignore[return-value]


def _tool_result(text: str, *, error: bool = False) -> dict:
    result = {"content": [{"type": "text", "text": text}]}
    if error:
        result["isError"] = True
    return result


def _error(message_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
