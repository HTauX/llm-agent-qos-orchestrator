"""Deterministic LangGraph workflow for capacity-aware QoS remediation."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence

from langchain_core.tools import BaseTool
from langgraph.graph import END, StateGraph

from qos_system_lg.a2a_client import send_text_message
from qos_system_lg.app_config import get_str
from qos_system_lg.llm_client import try_parse_json_object
from qos_system_lg.workflow import GraphState, _tool_text


def build_deterministic_workflow(
    *,
    tools: Sequence[BaseTool],
    executor_a2a_url: str,
    read_tool_name: str = "read_neo4j_cypher",
    write_tool_name: str = "write_neo4j_cypher",
    email_tool_name: str = "send_email",
) -> Any:
    """
    Deterministic workflow (no LLM):
    - Multi-path aware (USES_PATH allocations + Path nodes)
    - Plans partial transfer (rebalance) and writes back atomically
    """

    from qos_system_lg.app_config import format_template, get_bool  # local import to avoid widening top-level deps
    from qos_system_lg.multipath import flow_latency_ms_max
    from qos_system_lg.multipath_analysis import build_candidate_analysis
    from qos_system_lg.multipath_repo import load_flow_and_paths
    from qos_system_lg.rebalance_planner import build_plan_payload

    def _tool_by_name(name: str) -> Optional[BaseTool]:
        for t in tools:
            if getattr(t, "name", None) == name:
                return t
        return None

    read_tool = _tool_by_name(read_tool_name)
    write_tool = _tool_by_name(write_tool_name)
    email_tool = _tool_by_name(email_tool_name)
    if not read_tool or not write_tool:
        raise RuntimeError(f"Missing required MCP tools: {read_tool_name} / {write_tool_name}")

    async def alarm_ingest(state: GraphState) -> GraphState:
        if state.get("error"):
            return {**state, "step": "AlarmIngest"}
        ts = datetime.now(timezone.utc).isoformat()
        alarm = {
            "alarm_id": f"ALARM-{uuid.uuid4().hex[:10].upper()}",
            "flow_id": state.get("flow_id"),
            "scenario": state.get("scenario"),
            "metric": "qos_violation",
            "timestamp": ts,
            "detail": "deterministic demo alarm ingest",
        }
        return {**state, "step": "AlarmIngest", "alarm": alarm, "should_remediate": True}

    async def confirm_alarm(state: GraphState) -> GraphState:
        if state.get("error"):
            return {**state, "step": "ConfirmAlarm"}

        scenario = str(state.get("scenario") or "QOS_DEMO")
        flow_id = str(state.get("flow_id") or "")
        flow, paths = await load_flow_and_paths(read_tool=read_tool, scenario=scenario, flow_id=flow_id)
        if not flow:
            verification = {
                "status": "UNKNOWN",
                "bw_ok": False,
                "latency_ok": False,
                "rate": 0.0,
                "latency": 0.0,
                "reason": "Neo4j Flow missing; unable to evaluate SLA",
            }
            return {
                **state,
                "step": "ConfirmAlarm",
                "flow": {},
                "verification": verification,
                "should_remediate": False,
            }

        paths_by_id = {p.path_id: p for p in paths}
        # If USES_PATH/Path are not seeded, fall back to Flow.latency (legacy single-flow demo).
        derived_latency = float(flow.latency)
        if flow.allocations:
            try:
                derived_latency = float(flow_latency_ms_max(paths_by_id=paths_by_id, allocations=flow.allocations))
            except ValueError as exc:
                verification = {
                    "status": "UNKNOWN",
                    "bw_ok": False,
                    "latency_ok": False,
                    "rate": float(flow.requested_rate),
                    "latency": 0.0,
                    "reason": f"Incomplete topology data: {exc}",
                }
                return {
                    **state,
                    "step": "ConfirmAlarm",
                    "flow": {},
                    "verification": verification,
                    "should_remediate": False,
                }
        bw_ok = float(flow.requested_rate) >= float(flow.sla_min_bw)
        latency_ok = derived_latency <= float(flow.sla_max_latency)
        should_remediate = not (bw_ok and latency_ok)
        status = "VIOLATED" if should_remediate else "OK"

        flow_obj: Dict[str, Any] = {
            "flow_id": flow.flow_id,
            "scenario": flow.scenario,
            "src": flow.src,
            "dst": flow.dst,
            "requested_rate": float(flow.requested_rate),
            "rate": float(flow.requested_rate),
            "sla_min_bw": float(flow.sla_min_bw),
            "sla_max_latency": float(flow.sla_max_latency),
            "latency": derived_latency,
            "status": status,
            "allocations": [
                {
                    "path_id": a.path_id,
                    "rate_mbps": float(a.rate_mbps),
                    "path_nodes": (list(paths_by_id[a.path_id].nodes) if a.path_id in paths_by_id else []),
                }
                for a in flow.allocations
                if float(a.rate_mbps) > 0.0
            ],
        }
        verification = {
            "status": status,
            "bw_ok": bw_ok,
            "latency_ok": latency_ok,
            "rate": float(flow.requested_rate),
            "latency": derived_latency,
            "reason": (
                f"derived_latency={derived_latency:.3f}ms sla_max_latency={flow.sla_max_latency}ms"
                if should_remediate
                else "SLA satisfied"
            ),
        }

        notifications = list(state.get("notifications") or [])
        if should_remediate and get_bool("alerting", "enable_email_on_alarm", fallback=False) and email_tool:
            to_addr = get_str("alerting", "email_to", fallback="")
            cc = get_str("alerting", "email_cc", fallback="")
            subj_tpl = get_str("alerting", "email_subject", fallback="[QoS Alarm] {flow_id} {status} {src}->{dst}")
            body_tpl = get_str("alerting", "email_body", fallback="QoS alarm: {flow_id} {status}")
            values = {
                "scenario": flow.scenario,
                "flow_id": flow.flow_id,
                "status": status,
                "src": flow.src,
                "dst": flow.dst,
                "rate": float(flow.requested_rate),
                "sla_min_bw": float(flow.sla_min_bw),
                "latency": derived_latency,
                "sla_max_latency": float(flow.sla_max_latency),
                "reason": verification.get("reason"),
            }
            try:
                if to_addr:
                    subject = format_template(subj_tpl, values)
                    body = format_template(body_tpl, values)
                    result = await email_tool.ainvoke({"to_addr": to_addr, "subject": subject, "body": body, "cc": cc})
                    result_text = _tool_text(result)
                    email_ok = "error" not in result_text.lower() and "authentication failed" not in result_text.lower()
                    notifications.append(
                        {
                            "type": "email",
                            "to": to_addr,
                            "subject": subject,
                            "ok": email_ok,
                            "result": result,
                            **({} if email_ok else {"error": result_text}),
                        }
                    )
            except Exception as e:
                notifications.append({"type": "email", "to": to_addr, "subject": None, "ok": False, "error": str(e)})

        return {
            **state,
            "step": "ConfirmAlarm",
            "flow": flow_obj,
            "verification": verification,
            "should_remediate": should_remediate,
            "notifications": notifications,
        }

    async def analyze_pressure(state: GraphState) -> GraphState:
        if state.get("error") or not state.get("should_remediate", True):
            return {**state, "step": "AnalyzePressure"}

        scenario = str(state.get("scenario") or "QOS_DEMO")
        flow_id = str(state.get("flow_id") or "")
        flow, paths = await load_flow_and_paths(read_tool=read_tool, scenario=scenario, flow_id=flow_id)
        if not flow:
            return {
                **state,
                "step": "AnalyzePressure",
                "analysis": {"reason": "Neo4j Flow missing; cannot analyze candidate paths"},
                "should_remediate": False,
            }
        if not paths:
            return {
                **state,
                "step": "AnalyzePressure",
                "analysis": {"reason": "No Path nodes found; cannot analyze multi-path candidates"},
                "should_remediate": False,
            }

        analysis = build_candidate_analysis(flow=flow, paths=paths, delta_step_mbps=1.0)
        analysis["reason"] = "Computed from current Neo4j load snapshot (delta_step=1Mbps)"
        return {**state, "step": "AnalyzePressure", "analysis": analysis, "should_remediate": True}

    async def plan_change(state: GraphState) -> GraphState:
        if state.get("error") or not state.get("should_remediate", True):
            return {**state, "step": "PlanChange"}

        scenario = str(state.get("scenario") or "QOS_DEMO")
        flow_id = str(state.get("flow_id") or "")
        flow, paths = await load_flow_and_paths(read_tool=read_tool, scenario=scenario, flow_id=flow_id)
        if not flow or not paths:
            return {
                **state,
                "step": "PlanChange",
                "plan": {"reason": "Missing flow/paths; cannot plan"},
                "should_remediate": False,
            }

        plan = build_plan_payload(flow=flow, paths=paths, delta_step_mbps=1.0)
        should_remediate = bool(plan.get("plan_ok"))
        return {**state, "step": "PlanChange", "plan": plan, "should_remediate": should_remediate}

    async def execute_change(state: GraphState) -> GraphState:
        if state.get("error") or not state.get("should_remediate", True):
            return {**state, "step": "ExecuteChange"}

        plan = state.get("plan") or {}
        plan_id = plan.get("plan_id") if isinstance(plan, dict) else None
        cli_config = plan.get("cli_config") if isinstance(plan, dict) else None
        payload = {"capability": "execute_change", "params": {"plan": plan, "cli_config": cli_config}}
        text = json.dumps(payload, ensure_ascii=False)

        try:
            resp = await send_text_message(
                agent_card_url_or_base=executor_a2a_url, text=text, context_id=str(plan_id or "")
            )
            obj = resp.json_obj or try_parse_json_object(resp.text) or {}
            status = obj.get("status") if isinstance(obj, dict) and isinstance(obj.get("status"), str) else "Failure"
            if status not in {"Success", "Failure", "Rollback"}:
                status = "Failure"
            log = obj.get("log") if isinstance(obj, dict) and isinstance(obj.get("log"), str) else resp.text
            execution = {"status": status, "log": log}
            if status != "Success":
                return {**state, "step": "ExecuteChange", "execution": execution, "should_remediate": False}
            return {**state, "step": "ExecuteChange", "execution": execution}
        except Exception as e:
            return {
                **state,
                "step": "ExecuteChange",
                "execution": {"status": "Failure", "log": str(e)},
                "error": str(e),
                "should_remediate": False,
            }

    async def regression_verify(state: GraphState) -> GraphState:
        if state.get("error") or not state.get("should_remediate", True):
            return {**state, "step": "RegressionVerify"}

        plan = state.get("plan") or {}
        execution = state.get("execution") or {}
        if isinstance(execution, dict) and execution.get("status") != "Success":
            return {
                **state,
                "step": "RegressionVerify",
                "verification": {
                    "status": "VIOLATED",
                    "bw_ok": False,
                    "latency_ok": False,
                    "rate": 0.0,
                    "latency": 0.0,
                    "reason": "Execution failed; skip verification",
                },
            }

        if not isinstance(plan, dict) or not isinstance(plan.get("neo4j_atomic_write"), dict):
            return {
                **state,
                "step": "RegressionVerify",
                "error": "Missing plan.neo4j_atomic_write",
                "verification": {
                    "status": "VIOLATED",
                    "bw_ok": False,
                    "latency_ok": False,
                    "rate": 0.0,
                    "latency": 0.0,
                    "reason": "Missing atomic write block",
                },
            }

        atomic = plan["neo4j_atomic_write"]
        query = atomic.get("query")
        params = atomic.get("params")
        if not isinstance(query, str) or not isinstance(params, dict):
            return {
                **state,
                "step": "RegressionVerify",
                "error": "Invalid plan.neo4j_atomic_write format",
            }

        try:

            def _write_summary_has_updates(obj: Any) -> bool:
                # neo4j-cypher MCP's write tool returns a write summary, not query records.
                # We treat "contains updates" / non-zero counters as "applied".
                if isinstance(obj, str):
                    try:
                        parsed = json.loads(obj)
                    except Exception:
                        return False
                    return _write_summary_has_updates(parsed)
                if isinstance(obj, (bytes, bytearray)):
                    try:
                        return _write_summary_has_updates(obj.decode("utf-8"))
                    except Exception:
                        return False
                if isinstance(obj, dict):
                    if "_contains_updates" in obj:
                        return bool(obj.get("_contains_updates"))
                    if "contains_updates" in obj:
                        return bool(obj.get("contains_updates"))
                    # Some variants nest summary under another key.
                    for k in ("summary", "content", "result", "data"):
                        v = obj.get(k)
                        if isinstance(v, dict) and ("_contains_updates" in v or "contains_updates" in v):
                            return bool(v.get("_contains_updates") or v.get("contains_updates"))
                    for k in (
                        "nodes_created",
                        "nodes_deleted",
                        "relationships_created",
                        "relationships_deleted",
                        "properties_set",
                        "labels_added",
                        "labels_removed",
                        "indexes_added",
                        "constraints_added",
                    ):
                        try:
                            if float(obj.get(k) or 0.0) > 0.0:
                                return True
                        except Exception:
                            continue
                    return False
                if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                    return _write_summary_has_updates(obj[0])
                return False

            out = await write_tool.ainvoke({"query": query, "params": params})
            applied = _write_summary_has_updates(out)

            # Re-read and compute the post state (authoritative snapshot)
            scenario = str(state.get("scenario") or "QOS_DEMO")
            flow_id = str(state.get("flow_id") or "")
            flow, paths = await load_flow_and_paths(read_tool=read_tool, scenario=scenario, flow_id=flow_id)
            if not flow:
                return {**state, "step": "RegressionVerify", "error": "Flow missing after write"}

            paths_by_id = {p.path_id: p for p in paths}
            derived_latency = float(flow.latency)
            if flow.allocations and paths_by_id:
                derived_latency = float(flow_latency_ms_max(paths_by_id=paths_by_id, allocations=flow.allocations))
            bw_ok = float(flow.requested_rate) >= float(flow.sla_min_bw)
            latency_ok = derived_latency <= float(flow.sla_max_latency)
            status = "OK" if (bw_ok and latency_ok) else "VIOLATED"
            verification = {
                "status": status,
                "bw_ok": bw_ok,
                "latency_ok": latency_ok,
                "rate": float(flow.requested_rate),
                "latency": derived_latency,
                "reason": (
                    f"atomic_write_applied={applied}; post_latency={derived_latency:.3f}ms "
                    f"sla_max_latency={flow.sla_max_latency}ms"
                ),
            }
            patch: Dict[str, Any] = {
                **state,
                "step": "RegressionVerify",
                "verification": verification,
                "should_remediate": False,
            }
            if not applied and status != "OK":
                patch["error"] = f"Atomic write produced no updates: {out}"
            return patch

        except Exception as e:
            return {
                **state,
                "step": "RegressionVerify",
                "error": str(e),
                "verification": {
                    "status": "VIOLATED",
                    "bw_ok": False,
                    "latency_ok": False,
                    "rate": 0.0,
                    "latency": 0.0,
                    "reason": str(e),
                },
            }

    g = StateGraph(GraphState)
    g.add_node("AlarmIngest", alarm_ingest)
    g.add_node("ConfirmAlarm", confirm_alarm)
    g.add_node("AnalyzePressure", analyze_pressure)
    g.add_node("PlanChange", plan_change)
    g.add_node("ExecuteChange", execute_change)
    g.add_node("RegressionVerify", regression_verify)

    g.set_entry_point("AlarmIngest")
    g.add_edge("AlarmIngest", "ConfirmAlarm")

    def _route_after_confirm(state: GraphState) -> str:
        return END if not state.get("should_remediate", True) else "AnalyzePressure"

    g.add_conditional_edges("ConfirmAlarm", _route_after_confirm, {"AnalyzePressure": "AnalyzePressure", END: END})

    def _route_after_analyze(state: GraphState) -> str:
        return END if not state.get("should_remediate", True) else "PlanChange"

    g.add_conditional_edges("AnalyzePressure", _route_after_analyze, {"PlanChange": "PlanChange", END: END})

    def _route_after_plan(state: GraphState) -> str:
        return END if not state.get("should_remediate", True) else "ExecuteChange"

    g.add_conditional_edges("PlanChange", _route_after_plan, {"ExecuteChange": "ExecuteChange", END: END})

    def _route_after_execute(state: GraphState) -> str:
        return END if not state.get("should_remediate", True) else "RegressionVerify"

    g.add_conditional_edges(
        "ExecuteChange",
        _route_after_execute,
        {"RegressionVerify": "RegressionVerify", END: END},
    )
    g.add_edge("RegressionVerify", END)

    return g.compile()
