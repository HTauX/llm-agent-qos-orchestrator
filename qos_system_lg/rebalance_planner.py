from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from qos_system_lg.multipath import Allocation, Path, link_latency_ms
from qos_system_lg.multipath_repo import FlowSnapshot

_ATOMIC_REBALANCE_CYPHER = """
MATCH (f:Flow {scenario: $scenario})
WHERE f.flow_id = $flow_id OR f.id = $flow_id
WITH f
UNWIND $updates AS u
MATCH (a:Device {id: u.src, scenario: $scenario})
  -[r:CONNECTED_TO {scenario: $scenario}]->
  (b:Device {id: u.dst, scenario: $scenario})
WITH f,
     collect({r: r, delta: toFloat(u.delta)}) AS changes,
     count(r) AS matched,
     size($updates) AS expected
WITH f, changes, matched, expected,
     (matched = expected) AS ok_match,
     all(c IN changes WHERE (c.r.load + c.delta) >= 0.0 AND (c.r.load + c.delta) <= c.r.capacity) AS ok_cap
WITH f, changes, matched, expected, (ok_match AND ok_cap) AS ok
FOREACH (c IN CASE WHEN ok THEN changes ELSE [] END |
  SET c.r.load = c.r.load + c.delta
)
FOREACH (_ IN CASE WHEN ok THEN [1] ELSE [] END |
  SET f.requested_rate = $requested_rate,
      f.rate = $requested_rate,
      f.latency = $post_latency,
      f.status = $post_status,
      f.last_moved_at = datetime()
)
WITH ok, f, matched, expected
CALL {
  WITH ok, f
  OPTIONAL MATCH (f)-[old:USES_PATH]->(:Path {scenario: $scenario})
  WITH ok, f, collect(old) AS olds
  FOREACH (r IN CASE WHEN ok THEN olds ELSE [] END | DELETE r)
  WITH ok, f
  UNWIND CASE WHEN ok THEN $allocations_after ELSE [] END AS a
  MATCH (p:Path {scenario: $scenario, id: a.path_id})
  MERGE (f)-[u:USES_PATH]->(p)
  SET u.rate_mbps = toFloat(a.rate_mbps)
  RETURN count(*) AS wrote
}
RETURN ok, matched, expected, wrote;
""".strip()


@dataclass(frozen=True)
class PlanResult:
    ok: bool
    reason: str
    allocations_after: List[Allocation]
    edge_deltas: Dict[Tuple[str, str], float]
    post_latency_ms: float
    post_status: str


def _edge_key(src: str, dst: str) -> Tuple[str, str]:
    return (src, dst)


def _path_latency_ms(*, path: Path, edge_deltas: Mapping[Tuple[str, str], float]) -> float:
    total = 0.0
    for e in path.edges:
        delta = float(edge_deltas.get(_edge_key(e.src, e.dst), 0.0))
        total += link_latency_ms(base_latency=e.base_latency, capacity=e.capacity, load=e.load + delta)
    return total


def _path_can_apply_delta(*, path: Path, edge_deltas: Mapping[Tuple[str, str], float], delta_mbps: float) -> bool:
    # Check bounds on every directed edge in the path.
    if not path.edges:
        return False
    for e in path.edges:
        cur = float(e.load) + float(edge_deltas.get(_edge_key(e.src, e.dst), 0.0))
        nxt = cur + float(delta_mbps)
        if nxt < 0.0:
            return False
        if nxt > float(e.capacity):
            return False
    return True


def _apply_path_delta(
    *,
    path: Path,
    delta_mbps: float,
    edge_deltas: Dict[Tuple[str, str], float],
    include_reverse: bool,
) -> None:
    for e in path.edges:
        edge_deltas[_edge_key(e.src, e.dst)] = float(edge_deltas.get(_edge_key(e.src, e.dst), 0.0)) + float(delta_mbps)
        if include_reverse:
            edge_deltas[_edge_key(e.dst, e.src)] = float(edge_deltas.get(_edge_key(e.dst, e.src), 0.0)) + float(
                delta_mbps
            )


def _flow_latency_ms_max(
    *, paths_by_id: Mapping[str, Path], allocations: Sequence[Allocation], edge_deltas: Mapping[Tuple[str, str], float]
) -> float:
    latencies: list[float] = []
    for a in allocations:
        if a.rate_mbps <= 0:
            continue
        p = paths_by_id.get(a.path_id)
        if not p:
            continue
        latencies.append(_path_latency_ms(path=p, edge_deltas=edge_deltas))
    return max(latencies) if latencies else 0.0


def plan_atomic_rebalance(
    *,
    flow: FlowSnapshot,
    paths: Sequence[Path],
    delta_step_mbps: float = 1.0,
    max_iters: int = 64,
    include_reverse_edges: bool = True,
) -> PlanResult:
    if not flow.allocations:
        return PlanResult(
            ok=False,
            reason="Flow has no USES_PATH allocations; cannot perform multi-path rebalance",
            allocations_after=[],
            edge_deltas={},
            post_latency_ms=0.0,
            post_status="VIOLATED",
        )

    negative_allocations = [a.path_id for a in flow.allocations if float(a.rate_mbps) < 0.0]
    if negative_allocations:
        return PlanResult(
            ok=False,
            reason=f"Allocation rates must be non-negative: {sorted(set(negative_allocations))}",
            allocations_after=list(flow.allocations),
            edge_deltas={},
            post_latency_ms=0.0,
            post_status="VIOLATED",
        )

    paths_by_id = {p.path_id: p for p in paths}
    if len(paths_by_id) != len(paths):
        return PlanResult(
            ok=False,
            reason="Duplicate path_id values are not allowed",
            allocations_after=list(flow.allocations),
            edge_deltas={},
            post_latency_ms=0.0,
            post_status="VIOLATED",
        )

    missing = [a.path_id for a in flow.allocations if a.path_id not in paths_by_id]
    if missing:
        return PlanResult(
            ok=False,
            reason=f"Missing Path definitions for allocations: {sorted(set(missing))}",
            allocations_after=list(flow.allocations),
            edge_deltas={},
            post_latency_ms=0.0,
            post_status="VIOLATED",
        )

    empty_allocated_paths = [a.path_id for a in flow.allocations if not paths_by_id[a.path_id].edges]
    if empty_allocated_paths:
        return PlanResult(
            ok=False,
            reason=f"Allocated paths have no edge data: {sorted(set(empty_allocated_paths))}",
            allocations_after=list(flow.allocations),
            edge_deltas={},
            post_latency_ms=0.0,
            post_status="VIOLATED",
        )

    requested = float(flow.requested_rate)
    if requested <= 0:
        return PlanResult(
            ok=False,
            reason="requested_rate is not positive",
            allocations_after=list(flow.allocations),
            edge_deltas={},
            post_latency_ms=0.0,
            post_status="VIOLATED",
        )

    # Current allocation map
    alloc_map: dict[str, float] = defaultdict(float)
    for a in flow.allocations:
        alloc_map[a.path_id] += float(a.rate_mbps)

    # Sanity: enforce conservation in-memory (planner logic relies on it)
    total_alloc = sum(alloc_map.values())
    if abs(total_alloc - requested) > 1e-6:
        return PlanResult(
            ok=False,
            reason=f"Allocation sum mismatch: sum={total_alloc} requested_rate={requested}",
            allocations_after=list(flow.allocations),
            edge_deltas={},
            post_latency_ms=0.0,
            post_status="VIOLATED",
        )

    edge_deltas: Dict[Tuple[str, str], float] = {}
    step = max(0.0, float(delta_step_mbps))
    if step <= 0:
        step = 1.0

    for _ in range(max_iters):
        # Evaluate current per-path latencies
        used_paths = [pid for pid, r in alloc_map.items() if r > 0]
        if not used_paths:
            break

        current_latency = _flow_latency_ms_max(
            paths_by_id=paths_by_id,
            allocations=[Allocation(path_id=pid, rate_mbps=alloc_map[pid]) for pid in used_paths],
            edge_deltas=edge_deltas,
        )
        if current_latency <= float(flow.sla_max_latency):
            break

        # Pick the hottest (max latency) used path
        hot_pid = max(used_paths, key=lambda pid: _path_latency_ms(path=paths_by_id[pid], edge_deltas=edge_deltas))
        hot_rate = float(alloc_map.get(hot_pid, 0.0))
        if hot_rate <= 0.0:
            break

        move = min(step, hot_rate)
        hot_path = paths_by_id[hot_pid]
        if not _path_can_apply_delta(path=hot_path, edge_deltas=edge_deltas, delta_mbps=-move):
            return PlanResult(
                ok=False,
                reason=f"Hot path {hot_pid} cannot reduce by {move} Mbps (would make link load negative)",
                allocations_after=[Allocation(path_id=k, rate_mbps=v) for k, v in alloc_map.items()],
                edge_deltas=edge_deltas,
                post_latency_ms=current_latency,
                post_status="VIOLATED",
            )

        # Pick the coolest path that can accept +move
        candidates: list[Tuple[str, float]] = []
        for p in paths:
            if p.path_id == hot_pid:
                continue
            if not p.edges:
                continue
            if not _path_can_apply_delta(path=p, edge_deltas=edge_deltas, delta_mbps=move):
                continue
            # Approximate "after adding" latency by applying +move on this path only
            lat_after = 0.0
            for e in p.edges:
                d = float(edge_deltas.get(_edge_key(e.src, e.dst), 0.0)) + move
                lat_after += link_latency_ms(base_latency=e.base_latency, capacity=e.capacity, load=e.load + d)
            candidates.append((p.path_id, lat_after))

        if not candidates:
            return PlanResult(
                ok=False,
                reason="No alternative path can accept additional traffic (capacity exhausted)",
                allocations_after=[Allocation(path_id=k, rate_mbps=v) for k, v in alloc_map.items()],
                edge_deltas=edge_deltas,
                post_latency_ms=current_latency,
                post_status="VIOLATED",
            )

        cool_pid, _ = min(candidates, key=lambda x: x[1])
        cool_path = paths_by_id[cool_pid]

        # Apply the move: allocations and edge deltas
        alloc_map[hot_pid] = hot_rate - move
        alloc_map[cool_pid] = float(alloc_map.get(cool_pid, 0.0)) + move

        _apply_path_delta(
            path=hot_path, delta_mbps=-move, edge_deltas=edge_deltas, include_reverse=include_reverse_edges
        )
        _apply_path_delta(
            path=cool_path, delta_mbps=+move, edge_deltas=edge_deltas, include_reverse=include_reverse_edges
        )

    allocations_after = [Allocation(path_id=pid, rate_mbps=float(r)) for pid, r in sorted(alloc_map.items()) if r > 0.0]
    post_latency = _flow_latency_ms_max(paths_by_id=paths_by_id, allocations=allocations_after, edge_deltas=edge_deltas)

    bw_ok = requested >= float(flow.sla_min_bw)
    latency_ok = post_latency <= float(flow.sla_max_latency)
    post_status = "OK" if (bw_ok and latency_ok) else "VIOLATED"

    if post_status != "OK":
        # Provide a readable reason but still return the best-effort allocations_after.
        violations: list[str] = []
        if not bw_ok:
            violations.append(f"requested_rate={requested} < sla_min_bw={flow.sla_min_bw}")
        if not latency_ok:
            violations.append(f"post_latency={post_latency:.3f} > sla_max_latency={flow.sla_max_latency}")
        return PlanResult(
            ok=False,
            reason=f"Rebalance could not satisfy SLA ({'; '.join(violations)})",
            allocations_after=allocations_after,
            edge_deltas=edge_deltas,
            post_latency_ms=post_latency,
            post_status=post_status,
        )

    return PlanResult(
        ok=True,
        reason="Rebalance plan satisfies SLA under current-load snapshot",
        allocations_after=allocations_after,
        edge_deltas=edge_deltas,
        post_latency_ms=post_latency,
        post_status=post_status,
    )


def build_plan_payload(
    *,
    flow: FlowSnapshot,
    paths: Sequence[Path],
    delta_step_mbps: float = 1.0,
) -> Dict[str, Any]:
    """
    Convert the deterministic rebalancing plan into a JSON-serializable plan dict.
    """

    paths_by_id = {p.path_id: p for p in paths}
    before = [a for a in flow.allocations if a.rate_mbps > 0]

    result = plan_atomic_rebalance(flow=flow, paths=paths, delta_step_mbps=delta_step_mbps)

    before_map = {a.path_id: float(a.rate_mbps) for a in before}
    after_map = {a.path_id: float(a.rate_mbps) for a in result.allocations_after}

    path_deltas: List[Dict[str, Any]] = []
    for pid in sorted(set(before_map.keys()) | set(after_map.keys())):
        d = float(after_map.get(pid, 0.0)) - float(before_map.get(pid, 0.0))
        if abs(d) < 1e-9:
            continue
        path = paths_by_id.get(pid)
        path_deltas.append({"path_id": pid, "path_nodes": (list(path.nodes) if path else []), "delta_mbps": d})

    # Convert edge deltas into updates (drop ~0 values)
    updates: List[Dict[str, Any]] = []
    for (src, dst), delta in sorted(result.edge_deltas.items()):
        if abs(delta) < 1e-9:
            continue
        updates.append({"src": src, "dst": dst, "delta": float(delta)})

    plan_id = f"PLAN-{uuid.uuid4().hex[:10].upper()}"
    allocations_after = [{"path_id": a.path_id, "rate_mbps": float(a.rate_mbps)} for a in result.allocations_after]
    allocations_before = [{"path_id": a.path_id, "rate_mbps": float(a.rate_mbps)} for a in before]

    cli_lines = [
        f"# plan_id={plan_id}",
        f"# flow_id={flow.flow_id} scenario={flow.scenario}",
        "qos flow rebalance",
    ]
    for a in allocations_after:
        cli_lines.append(f"  set path {a['path_id']} rate {a['rate_mbps']}Mbps")
    cli_lines.append("commit")
    cli_config = "\n".join(cli_lines)

    return {
        "plan_id": plan_id,
        "flow_id": flow.flow_id,
        "scenario": flow.scenario,
        "requested_rate": float(flow.requested_rate),
        "allocations_before": allocations_before,
        "allocations_after": allocations_after,
        "path_deltas": path_deltas,
        "updates": updates,
        "neo4j_atomic_write": {
            "query": _ATOMIC_REBALANCE_CYPHER,
            "params": {
                "scenario": flow.scenario,
                "flow_id": flow.flow_id,
                "requested_rate": float(flow.requested_rate),
                "allocations_after": allocations_after,
                "updates": updates,
                "post_latency": float(result.post_latency_ms),
                "post_status": result.post_status,
            },
        },
        "cli_config": cli_config,
        "reason": result.reason,
        "plan_ok": bool(result.ok),
        "post_latency_ms": float(result.post_latency_ms),
        "post_status": result.post_status,
    }
