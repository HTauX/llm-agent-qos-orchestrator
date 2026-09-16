from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def _try_json_loads(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except Exception:
        return None


def parse_neo4j_records(result: Any) -> List[Dict[str, Any]]:
    """
    Best-effort parsing for the `neo4j-cypher` MCP tool output.

    The adapter may return:
    - list[dict] (already parsed records)
    - dict (with nested records/data fields)
    - str (JSON or plain text)
    """

    if result is None:
        return []

    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]

    if isinstance(result, dict):
        for key in ("records", "data", "result", "rows"):
            v = result.get(key)
            if isinstance(v, list) and all(isinstance(x, dict) for x in v):
                return v
        # Some MCP tools return {"content": [...]} or {"content": {"records": [...]}}
        content = result.get("content")
        if isinstance(content, list) and all(isinstance(x, dict) for x in content):
            return content
        if isinstance(content, dict):
            return parse_neo4j_records(content)
        # Fallback: treat the dict itself as a single record
        return [result]

    if isinstance(result, (bytes, bytearray)):
        try:
            return parse_neo4j_records(result.decode("utf-8"))
        except Exception:
            return []

    if isinstance(result, str):
        s = result.strip()
        if not s:
            return []
        parsed = _try_json_loads(s)
        if parsed is not None:
            return parse_neo4j_records(parsed)
        # Not JSON; cannot reliably parse
        return []

    # Unknown type
    return []


async def neo4j_read_records(*, read_tool: Any, query: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = await read_tool.ainvoke({"query": query, "params": params})
    return parse_neo4j_records(out)


async def neo4j_write(*, write_tool: Any, query: str, params: Dict[str, Any]) -> Any:
    return await write_tool.ainvoke({"query": query, "params": params})
