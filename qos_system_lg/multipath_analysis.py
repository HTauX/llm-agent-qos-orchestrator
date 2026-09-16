from __future__ import annotations

from typing import Any, Dict, List, Sequence

from qos_system_lg.multipath import Path, candidate_metrics_for_path, flow_latency_ms_max, path_latency_ms
from qos_system_lg.multipath_repo import FlowSnapshot


def build_candidate_analysis(
    *,
    flow: FlowSnapshot,
    paths: Sequence[Path],
    delta_step_mbps: float = 1.0,
) -> Dict[str, Any]:
    """
    Build an "Agent3-style" analysis payload for multi-path:
    - current allocations (per-path rate + current est latency)
    - candidate paths (capacity/latency metrics under +delta_step)
    """

    paths_by_id = {p.path_id: p for p in paths}

    current_allocations: List[Dict[str, Any]] = []
    for a in flow.allocations:
        if a.rate_mbps <= 0.0:
            continue
        p = paths_by_id.get(a.path_id)
        current_allocations.append(
            {
                "path_id": a.path_id,
                "path_nodes": list(p.nodes) if p else [],
                "rate_mbps": float(a.rate_mbps),
                "est_latency_current_ms": (path_latency_ms(edges=p.edges, delta_mbps=0.0) if p else None),
            }
        )

    candidates = [candidate_metrics_for_path(path=p, delta_step_mbps=float(delta_step_mbps)) for p in paths if p.edges]

    current_flow_latency_ms = flow_latency_ms_max(paths_by_id=paths_by_id, allocations=flow.allocations)

    return {
        "flow_id": flow.flow_id,
        "scenario": flow.scenario,
        "src": flow.src,
        "dst": flow.dst,
        "requested_rate": float(flow.requested_rate),
        "sla_max_latency": float(flow.sla_max_latency),
        "sla_min_bw": float(flow.sla_min_bw),
        "current_flow_latency_ms": float(current_flow_latency_ms),
        "delta_step_mbps": float(delta_step_mbps),
        "current_allocations": current_allocations,
        "candidates": candidates,
    }
