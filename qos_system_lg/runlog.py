from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

_NODE_META: dict[str, dict[str, str]] = {
    "AlarmIngest": {"agent": "Agent1_AlarmIngest", "title": "Alarm Ingestion"},
    "ConfirmAlarm": {"agent": "Agent2_ConfirmAlarm", "title": "Confirm Alarm (QoS vs SLA)"},
    "AnalyzePressure": {"agent": "Agent3_AnalyzePressure", "title": "Identify Candidate Paths (Multi-Path)"},
    "PlanChange": {"agent": "Agent4_PlanChange", "title": "Build Rebalance Plan (Atomic Updates)"},
    "ExecuteChange": {"agent": "Agent5_ExecuteChange", "title": "Invoke Executor (A2A)"},
    "RegressionVerify": {"agent": "Agent6_RegressionVerify", "title": "Regression Verification + Write Back"},
}

_TRUNCATE_ENABLED = (os.getenv("QOS_RUNLOG_TRUNCATE") or "1").strip().lower() not in {"0", "false", "no", "off"}


def _truncate(text: str, max_len: int) -> str:
    if not _TRUNCATE_ENABLED:
        return text
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 3)] + "..."


def _json_preview(obj: Any, *, max_len: int = 240) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        s = str(obj)
    return _truncate(s, max_len=max_len)


def _calc_qos_sla(flow: Dict[str, Any]) -> Tuple[bool, bool, bool]:
    bw_ok = float(flow.get("rate", 0.0)) >= float(flow.get("sla_min_bw", 0.0))
    latency_ok = float(flow.get("latency", 1e9)) <= float(flow.get("sla_max_latency", 0.0))
    should_remediate = not (bw_ok and latency_ok)
    return bw_ok, latency_ok, should_remediate


def _next_node(node: str, state: Dict[str, Any]) -> Optional[str]:
    if node == "AlarmIngest":
        return "ConfirmAlarm"
    if node == "ConfirmAlarm":
        if state.get("error") or not state.get("should_remediate", True):
            return None
        return "AnalyzePressure"
    if node == "AnalyzePressure":
        if state.get("error") or not state.get("should_remediate", True):
            return None
        return "PlanChange"
    if node == "PlanChange":
        if state.get("error") or not state.get("should_remediate", True):
            return None
        return "ExecuteChange"
    if node == "ExecuteChange":
        if state.get("error") or not state.get("should_remediate", True):
            return None
        return "RegressionVerify"
    return None


def format_step_lines(step_no: int, node: str, state: Dict[str, Any]) -> List[str]:

    meta = _NODE_META.get(node) or {"agent": node, "title": node}
    agent = meta["agent"]
    title = meta["title"]

    lines: List[str] = []
    prefix = f"{step_no}、[{agent}]"

    err = state.get("error")
    if node == "AlarmIngest":
        alarm = state.get("alarm") or {}
        should_remediate = state.get("should_remediate")

        if should_remediate is False:
            lines.append(f"{prefix}{title}: No QoS alarm detected, flow_id={state.get('flow_id')}")
            if isinstance(alarm, dict) and alarm:
                lines.append(f"    Data: {_json_preview(alarm)}")
        elif isinstance(alarm, dict) and alarm:
            lines.append(
                f"{prefix}{title}: QoS alarm detected, flow_id={alarm.get('flow_id')} alarm_id={alarm.get('alarm_id')}"
            )
            lines.append(f"    Data: {_json_preview(alarm)}")
        else:
            lines.append(f"{prefix}{title}: No QoS alarm detected, flow_id={state.get('flow_id')}")
        notifs = state.get("notifications") or []
        if isinstance(notifs, list):
            email_notifs = [n for n in notifs if isinstance(n, dict) and n.get("type") == "email"]
            if email_notifs:
                last = email_notifs[-1]
                if last.get("error") or last.get("ok") is False:
                    lines.append(
                        f"    Email notification: failed "
                        f"({_truncate(str(last.get('error') or last.get('result') or ''), 160)})"
                    )
                else:
                    lines.append(
                        f"    Email notification: sent "
                        f"to={last.get('to')} subject={_truncate(str(last.get('subject') or ''), 120)}"
                    )

    elif node == "ConfirmAlarm":
        flow = state.get("flow") or {}
        verification = state.get("verification") or {}

        status = verification.get("status") if isinstance(verification, dict) else None
        bw_ok = verification.get("bw_ok") if isinstance(verification, dict) else None
        latency_ok = verification.get("latency_ok") if isinstance(verification, dict) else None
        rate = (verification.get("rate") if isinstance(verification, dict) else None) or (
            flow.get("rate") if isinstance(flow, dict) else None
        )
        latency = (verification.get("latency") if isinstance(verification, dict) else None) or (
            flow.get("latency") if isinstance(flow, dict) else None
        )

        should_remediate = state.get("should_remediate")
        if should_remediate is None and isinstance(flow, dict) and flow:
            # Log-only fallback (normally the LLM should provide conclusions
            # via state.verification + state.should_remediate)
            bw_ok_fallback, latency_ok_fallback, should_remediate_fallback = _calc_qos_sla(flow)
            if not isinstance(bw_ok, bool):
                bw_ok = bw_ok_fallback
            if not isinstance(latency_ok, bool):
                latency_ok = latency_ok_fallback
            should_remediate = should_remediate_fallback
            if not isinstance(status, str):
                status = "VIOLATED" if should_remediate else "OK"
        if not isinstance(status, str):
            if isinstance(should_remediate, bool):
                status = "VIOLATED" if should_remediate else "OK"
            else:
                status = "UNKNOWN"

        lines.append(
            f"{prefix}{title}: status={status} bw_ok={bw_ok} latency_ok={latency_ok} "
            f"(rate={rate} sla_min_bw={flow.get('sla_min_bw')}, "
            f"latency={latency} sla_max_latency={flow.get('sla_max_latency')})"
        )
        lines.append(f"    Data: {_json_preview(flow)}")
        if should_remediate is False and not err:
            lines.append("    End: SLA satisfied, no remediation required")

    elif node == "AnalyzePressure":
        analysis = state.get("analysis") or {}
        allocs = analysis.get("current_allocations") if isinstance(analysis, dict) else None
        cands = analysis.get("candidates") if isinstance(analysis, dict) else None
        alloc_cnt = len(allocs) if isinstance(allocs, list) else 0
        cand_cnt = len(cands) if isinstance(cands, list) else 0
        lines.append(f"{prefix}{title}: allocations={alloc_cnt} candidates={cand_cnt}")
        brief = {
            "src": analysis.get("src"),
            "dst": analysis.get("dst"),
            "requested_rate": analysis.get("requested_rate"),
            "current_flow_latency_ms": analysis.get("current_flow_latency_ms"),
            "current_allocations": allocs if isinstance(allocs, list) else [],
            "candidates_count": cand_cnt,
            "reason": analysis.get("reason"),
        }
        lines.append(f"    Data: {_json_preview(brief)}")
        if cand_cnt <= 0 and not err:
            lines.append("    End: No candidate paths found; automatic remediation not possible")

    elif node == "PlanChange":
        plan = state.get("plan") or {}
        lines.append(f"{prefix}{title}: plan_id={plan.get('plan_id')} ok={plan.get('plan_ok')}")
        brief = {
            "plan_id": plan.get("plan_id"),
            "flow_id": plan.get("flow_id"),
            "requested_rate": plan.get("requested_rate"),
            "allocations_before": plan.get("allocations_before"),
            "allocations_after": plan.get("allocations_after"),
            "updates_count": len(plan.get("updates") or []),
            "reason": plan.get("reason"),
            "cli_config_preview": _truncate(str(plan.get("cli_config") or ""), 180),
        }
        lines.append(f"    Data: {_json_preview(brief)}")

    elif node == "ExecuteChange":
        execution = state.get("execution") or {}
        lines.append(f"{prefix}{title}: status={execution.get('status')}")
        brief = {"status": execution.get("status"), "log_preview": _truncate(str(execution.get("log") or ""), 220)}
        lines.append(f"    Data: {_json_preview(brief)}")

    elif node == "RegressionVerify":
        verification = state.get("verification") or {}
        lines.append(
            f"{prefix}{title}: status={verification.get('status')} "
            f"rate={verification.get('rate')} latency={verification.get('latency')}"
        )
        brief = {
            "status": verification.get("status"),
            "bw_ok": verification.get("bw_ok"),
            "latency_ok": verification.get("latency_ok"),
            "rate": verification.get("rate"),
            "latency": verification.get("latency"),
            "reason": verification.get("reason"),
        }
        lines.append(f"    Data: {_json_preview(brief)}")

    else:
        # Unknown node: fallback to printing the step title and a short state preview
        lines.append(f"{prefix}{title}")
        lines.append(f"    Data: {_json_preview(state)}")

    if err:
        lines.append(f"    ERROR: {err}")

    next_node = _next_node(node, state)
    if next_node:
        next_meta = _NODE_META.get(next_node) or {"agent": next_node}
        lines.append(f"    Invoking [{next_meta['agent']}] ...")

    return lines


def format_steps_text(steps: Iterable[Tuple[int, str, Dict[str, Any]]]) -> str:

    out_lines: List[str] = []
    for step_no, node, state in steps:
        out_lines.extend(format_step_lines(step_no, node, state))
    return "\n".join(out_lines) + ("\n" if out_lines else "")
