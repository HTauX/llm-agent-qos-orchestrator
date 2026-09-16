from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Optional

from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger(__name__)


def load_mcp_connections(config_path: str) -> dict[str, dict[str, Any]]:
    """
    Read MCP server configuration from `mcp.json` and convert it into
    connections usable by langchain-mcp-adapters.
    """

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    config_dir = os.path.dirname(os.path.abspath(config_path))

    servers = cfg.get("mcpServers") or cfg.get("servers") or {}
    if not isinstance(servers, dict) or not servers:
        raise ValueError(f"MCP configuration does not contain mcpServers: {config_path}")

    connections: dict[str, dict[str, Any]] = {}
    for name, server_cfg in servers.items():
        if not isinstance(server_cfg, dict):
            continue

        # The transport here refers to the “client connection method”; currently only stdio is implemented.
        transport = server_cfg.get("transport") or "stdio"
        if transport != "stdio":
            raise ValueError(f"Only stdio MCP servers are supported (server={name}, transport={transport})")

        env = os.environ.copy()
        if isinstance(server_cfg.get("env"), dict):
            env.update({k: str(v) for k, v in server_cfg["env"].items()})

        command = str(server_cfg["command"])

        if command in {"python", "python3"}:
            command = sys.executable

        connections[name] = {
            "transport": "stdio",
            "command": command,
            "args": server_cfg.get("args", []),
            "env": env,
        }
        if server_cfg.get("cwd"):
            raw_cwd = os.path.expanduser(str(server_cfg["cwd"]))
            connections[name]["cwd"] = (
                raw_cwd if os.path.isabs(raw_cwd) else os.path.abspath(os.path.join(config_dir, raw_cwd))
            )

    if not connections:
        raise ValueError(f"No valid server connection information found in MCP config: {config_path}")
    return connections


async def load_tools_from_mcp_json(
    *,
    config_path: str,
    server_name: Optional[str] = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """
    Load MCP tools via `langchain-mcp-adapters`
    (returns a mapping: tool_name -> tool).


    Notes:
    - Tools converted by adapters are usually async
      (must be invoked with `await tool.ainvoke(...)`).
    - The exact input field names depend on the tool’s JSON Schema
      (e.g., neo4j-cypher uses `query` / `params`).
    """

    connections = load_mcp_connections(config_path)

    # Supported values:
    # - None / "" / "all": load all servers in the config
    # - "a,b,c": load the specified list
    # - "a": load a single server

    raw = (server_name or "").strip()
    if not raw or raw.lower() == "all":
        chosen = list(connections.keys())
    else:
        wanted = [x.strip() for x in raw.split(",") if x.strip()]
        unknown = [name for name in wanted if name not in connections]
        if unknown:
            raise ValueError(f"Unknown MCP server name(s): {unknown}; available: {sorted(connections.keys())}")
        chosen = wanted

    chosen_connections = {name: connections[name] for name in chosen}
    client = MultiServerMCPClient(chosen_connections)
    tools = await client.get_tools(server_name=None)

    tool_map: dict[str, Any] = {}
    for t in tools:
        if t.name in tool_map:
            raise ValueError(
                f"MCP tool name conflict: {t.name} (it is recommended to avoid duplicate names in the MCP config)"
            )
        tool_map[t.name] = t

    if verbose:
        logger.info("[MCP] servers=%s tools=%s", chosen, list(tool_map.keys()))
        for name, tool in tool_map.items():
            schema = getattr(tool, "args_schema", None)
            if isinstance(schema, dict):
                required = schema.get("required") or []
                props = schema.get("properties") or {}
                logger.info("[MCP] - %s: required=%s props=%s", name, required, list(props.keys()))
            else:
                logger.info("[MCP] - %s: args_schema_type=%s", name, type(schema))

    return tool_map
