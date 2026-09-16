from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse

from qos_system_lg.app_config import get_bool
from qos_system_lg.mcp_client import load_tools_from_mcp_json
from qos_system_lg.neo4j_tools import neo4j_read_records
from qos_system_lg.runlog import format_step_lines
from qos_system_lg.workflow import GraphState, build_workflow

_here = os.path.dirname(os.path.abspath(__file__))
# First load `qos-system-lg/.env` (for self-contained configuration in this directory),
# then load `.env` from the CWD (for backward compatibility).
load_dotenv(os.path.join(_here, "..", ".env"))
load_dotenv()


def _default_mcp_config_path() -> str:
    # By default, read qos-system-lg/mcp.json
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "mcp.json"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifecycle: load MCP tools and compile the LangGraph at startup.
    """

    config_path_env = os.getenv("MCP_CONFIG_PATH")
    config_path = config_path_env or _default_mcp_config_path()
    if config_path_env:
        config_path = os.path.expanduser(config_path)
        if not os.path.isabs(config_path):
            # `.env` is usually placed under qos-system-lg/; relative paths are also resolved
            # against that directory to avoid being affected by the CWD.
            project_root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
            config_path = os.path.abspath(os.path.join(project_root, config_path))
    server_name = os.getenv("MCP_SERVER_NAME") or None
    read_tool_name = os.getenv("MCP_READ_TOOL_NAME") or "read_neo4j_cypher"
    write_tool_name = os.getenv("MCP_WRITE_TOOL_NAME") or "write_neo4j_cypher"
    email_tool_name = os.getenv("MCP_EMAIL_TOOL_NAME") or "send_email"
    executor_url = (
        os.getenv("EXECUTOR_AGENT_CARD_URL")
        or os.getenv("EXECUTOR_A2A_URL")  # Backward-compatible variable name
        or "http://127.0.0.1:8010/.well-known/agent-card.json"
    )

    # If alert email is enabled, also include email-mcp in the load list by default
    if get_bool("alerting", "enable_email_on_alarm", fallback=False):
        if server_name and server_name.strip().lower() != "all":
            names = {x.strip() for x in server_name.split(",") if x.strip()}
            if "email-mcp" not in names:
                server_name = server_name + ",email-mcp"

    mcp_verbose = str(os.getenv("MCP_VERBOSE") or "").strip().lower() in {"1", "true", "yes", "y"}
    tool_map = await load_tools_from_mcp_json(config_path=config_path, server_name=server_name, verbose=mcp_verbose)

    required = {read_tool_name, write_tool_name}
    if get_bool("alerting", "enable_email_on_alarm", fallback=False):
        required.add(email_tool_name)
    missing = [name for name in sorted(required) if name not in tool_map]
    if missing:
        raise RuntimeError(f"MCP tools missing: {missing}, available: {list(tool_map.keys())}")

    tools = list(tool_map.values())

    app.state.tool_map = tool_map
    app.state.tools = tools
    app.state.executor_url = executor_url
    app.state.graph = build_workflow(
        tools=tools,
        executor_a2a_url=executor_url,
        read_tool_name=read_tool_name,
        write_tool_name=write_tool_name,
        email_tool_name=email_tool_name,
    )

    yield


app = FastAPI(title="QoS Demo Orchestrator (LangGraph + Neo4j MCP + A2A)", lifespan=lifespan)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True}


@app.post("/run_demo")
async def run_demo(
    scenario: str = Query(default="QOS_DEMO", description="Neo4j demo scenario identifier (default: QOS_DEMO)"),
    flow_id: str = Query(default="FLOW-001", description="Flow node ID (default: FLOW-001)"),
) -> Dict[str, Any]:
    """
    Trigger an end-to-end demo run
    """

    graph = app.state.graph
    init_state: GraphState = {"scenario": scenario, "flow_id": flow_id, "mcp_audit": [], "notifications": []}
    final_state: Dict[str, Any] = await graph.ainvoke(init_state)
    return final_state


@app.post("/run_demo_all_violations")
async def run_demo_all_violations(
    scenario: str = Query(default="QOS_DEMO", description="Neo4j demo scenario identifier (default: QOS_DEMO)"),
    limit: int = Query(default=10, ge=1, le=200, description="Max number of violating flows to process (default: 10)"),
) -> Dict[str, Any]:
    """
    Orchestrate remediation for all currently violating flows (sequentially).
    """

    read_tool_name = os.getenv("MCP_READ_TOOL_NAME") or "read_neo4j_cypher"
    read_tool = (app.state.tool_map or {}).get(read_tool_name)
    if not read_tool:
        raise RuntimeError(f"Missing read tool: {read_tool_name}")

    query = """
    MATCH (f:Flow {scenario: $scenario})
    WHERE coalesce(f.status, "") = "VIOLATED"
    RETURN coalesce(f.flow_id, f.id) AS flow_id
    ORDER BY flow_id
    LIMIT $limit
    """.strip()
    rows = await neo4j_read_records(
        read_tool=read_tool, query=query, params={"scenario": scenario, "limit": int(limit)}
    )
    flow_ids = [r.get("flow_id") for r in rows if isinstance(r.get("flow_id"), str) and r.get("flow_id")]

    graph = app.state.graph
    results: list[dict[str, Any]] = []
    for fid in flow_ids:
        init_state: GraphState = {"scenario": scenario, "flow_id": fid, "mcp_audit": [], "notifications": []}
        final_state: Dict[str, Any] = await graph.ainvoke(init_state)
        results.append({"flow_id": fid, "final": final_state})

    return {"scenario": scenario, "count": len(results), "flow_ids": flow_ids, "results": results}


@app.post("/run_demo_all_violations_stream")
async def run_demo_all_violations_stream(
    scenario: str = Query(default="QOS_DEMO", description="Neo4j demo scenario identifier (default: QOS_DEMO)"),
    limit: int = Query(default=10, ge=1, le=200, description="Max number of violating flows to process (default: 10)"),
    final_json: bool = Query(
        default=False,
        description="Append final state JSON at the end of each flow log stream (default: false)",
    ),
) -> StreamingResponse:
    """
    Stream remediation runs for all currently violating flows (sequentially).
    For each flow, stream each LangGraph node’s key information step by step.
    """

    read_tool_name = os.getenv("MCP_READ_TOOL_NAME") or "read_neo4j_cypher"
    read_tool = (app.state.tool_map or {}).get(read_tool_name)
    if not read_tool:
        raise RuntimeError(f"Missing read tool: {read_tool_name}")

    query = """
    MATCH (f:Flow {scenario: $scenario})
    WHERE coalesce(f.status, "") = "VIOLATED"
    RETURN coalesce(f.flow_id, f.id) AS flow_id
    ORDER BY flow_id
    LIMIT $limit
    """.strip()
    rows = await neo4j_read_records(
        read_tool=read_tool, query=query, params={"scenario": scenario, "limit": int(limit)}
    )
    flow_ids = [r.get("flow_id") for r in rows if isinstance(r.get("flow_id"), str) and r.get("flow_id")]

    graph = app.state.graph

    async def _gen():
        try:
            yield f"[all_violations] scenario={scenario} count={len(flow_ids)} limit={limit}\n"
            if flow_ids:
                yield f"[all_violations] flow_ids={json.dumps(flow_ids, ensure_ascii=False)}\n"
            else:
                yield "[all_violations] no VIOLATED flows found\n"
                return

            for idx, fid in enumerate(flow_ids, start=1):
                yield "\n"
                yield f"=== FLOW {fid} ({idx}/{len(flow_ids)}) ===\n"
                step_no = 0
                last_state: Optional[Dict[str, Any]] = None

                init_state: GraphState = {"scenario": scenario, "flow_id": fid, "mcp_audit": [], "notifications": []}
                accumulated_state: Dict[str, Any] = dict(init_state)
                async for update in graph.astream(init_state, stream_mode="updates"):
                    if not isinstance(update, dict):
                        continue
                    for node_name, node_state in update.items():
                        if not isinstance(node_name, str) or not isinstance(node_state, dict):
                            continue
                        step_no += 1
                        accumulated_state.update(node_state)
                        last_state = dict(accumulated_state)
                        for line in format_step_lines(step_no, node_name, node_state):
                            print(line, flush=True)
                            yield line + "\n"

                if final_json and last_state is not None:
                    yield "--- FINAL STATE (JSON) ---\n"
                    yield json.dumps(last_state, ensure_ascii=False, indent=2) + "\n"

            yield "\n[all_violations] done\n"
        except asyncio.CancelledError:
            return
        except Exception as e:
            yield f"[run_demo_all_violations_stream] ERROR: {e}\n"

    return StreamingResponse(_gen(), media_type="text/plain; charset=utf-8")


@app.post("/run_demo_stream")
async def run_demo_stream(
    scenario: str = Query(default="QOS_DEMO", description="Neo4j demo scenario identifier (default: QOS_DEMO)"),
    flow_id: str = Query(default="FLOW-001", description="Flow node ID (default: FLOW-001)"),
    final_json: bool = Query(
        default=False, description="Append final state JSON at the end of the log stream (default: false)"
    ),
) -> StreamingResponse:
    """
    Stream each LangGraph node’s key information step by step

    """

    graph = app.state.graph
    init_state: GraphState = {"scenario": scenario, "flow_id": flow_id, "mcp_audit": [], "notifications": []}

    async def _gen():
        step_no = 0
        last_state: Optional[Dict[str, Any]] = None
        accumulated_state: Dict[str, Any] = dict(init_state)
        try:
            async for update in graph.astream(init_state, stream_mode="updates"):
                if not isinstance(update, dict):
                    continue
                for node_name, node_state in update.items():
                    if not isinstance(node_name, str) or not isinstance(node_state, dict):
                        continue
                    step_no += 1
                    accumulated_state.update(node_state)
                    last_state = dict(accumulated_state)
                    for line in format_step_lines(step_no, node_name, node_state):
                        # Synchronously write to orchestrator.log
                        # (stdout/stderr are redirected to this file in the script).
                        print(line, flush=True)
                        yield line + "\n"
            if final_json and last_state is not None:
                yield "\n--- FINAL STATE (JSON) ---\n"
                yield json.dumps(last_state, ensure_ascii=False, indent=2) + "\n"
        except asyncio.CancelledError:
            # When the client disconnects, FastAPI/uvicorn will cancel the generator;
            return
        except Exception as e:
            yield f"[run_demo_stream] ERROR: {e}\n"

    return StreamingResponse(_gen(), media_type="text/plain; charset=utf-8")
