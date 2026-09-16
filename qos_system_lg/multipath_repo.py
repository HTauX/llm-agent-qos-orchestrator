from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from qos_system_lg.multipath import Allocation, Path, coerce_allocations, coerce_paths
from qos_system_lg.neo4j_tools import neo4j_read_records


@dataclass(frozen=True)
class FlowSnapshot:
    flow_id: str
    scenario: str
    src: str
    dst: str
    requested_rate: float
    sla_min_bw: float
    sla_max_latency: float
    latency: float
    status: str
    allocations: List[Allocation]


_FLOW_QUERY = """
MATCH (f:Flow {scenario: $scenario})
WHERE f.flow_id = $flow_id OR f.id = $flow_id
OPTIONAL MATCH (f)-[u:USES_PATH]->(p:Path {scenario: $scenario})
RETURN
  coalesce(f.flow_id, f.id) AS flow_id,
  f.scenario AS scenario,
  f.src AS src,
  f.dst AS dst,
  coalesce(f.requested_rate, f.rate) AS requested_rate,
  coalesce(f.sla_min_bw, 0.0) AS sla_min_bw,
  coalesce(f.sla_max_latency, 0.0) AS sla_max_latency,
  coalesce(f.latency, 0.0) AS latency,
  coalesce(f.status, "") AS status,
  collect(
    CASE
      WHEN p IS NULL THEN null
      ELSE {path_id: p.id, rate_mbps: coalesce(u.rate_mbps, 0.0)}
    END
  ) AS uses_path_allocations
LIMIT 1
""".strip()


_PATHS_QUERY = """
MATCH (p:Path {scenario: $scenario, src: $src, dst: $dst})
UNWIND range(0, size(p.nodes) - 2) AS idx
WITH p, idx, p.nodes[idx] AS src_id, p.nodes[idx + 1] AS dst_id
MATCH (a:Device {id: src_id, scenario: $scenario})
  -[r:CONNECTED_TO {scenario: $scenario}]->
  (b:Device {id: dst_id, scenario: $scenario})
WITH p, idx, src_id, dst_id, r
ORDER BY idx
RETURN
  p.id AS path_id,
  p.nodes AS nodes,
  coalesce(p.hops, size(p.nodes) - 1) AS hops,
  collect({src: src_id, dst: dst_id, capacity: r.capacity, load: r.load, base_latency: r.base_latency}) AS edges
ORDER BY hops ASC, path_id ASC
""".strip()


def _pick_allocations(row: Dict[str, Any]) -> List[Allocation]:
    uses = row.get("uses_path_allocations")
    alloc_from_rel = coerce_allocations([x for x in uses if isinstance(x, dict)] if isinstance(uses, list) else [])
    if alloc_from_rel:
        return alloc_from_rel

    # No allocation data: fall back to "single path" semantics with unknown path_id.
    return []


async def load_flow_snapshot(*, read_tool: Any, scenario: str, flow_id: str) -> Optional[FlowSnapshot]:
    rows = await neo4j_read_records(
        read_tool=read_tool, query=_FLOW_QUERY, params={"scenario": scenario, "flow_id": flow_id}
    )
    if not rows:
        return None
    row = rows[0]

    fid = row.get("flow_id")
    src = row.get("src")
    dst = row.get("dst")
    if not isinstance(fid, str) or not isinstance(src, str) or not isinstance(dst, str):
        return None

    try:
        requested_rate = float(row.get("requested_rate") or 0.0)
    except Exception:
        requested_rate = 0.0
    try:
        sla_min_bw = float(row.get("sla_min_bw") or 0.0)
    except Exception:
        sla_min_bw = 0.0
    try:
        sla_max_latency = float(row.get("sla_max_latency") or 0.0)
    except Exception:
        sla_max_latency = 0.0
    try:
        latency = float(row.get("latency") or 0.0)
    except Exception:
        latency = 0.0

    status = row.get("status") if isinstance(row.get("status"), str) else ""
    allocations = _pick_allocations(row)

    return FlowSnapshot(
        flow_id=fid,
        scenario=scenario,
        src=src,
        dst=dst,
        requested_rate=requested_rate,
        sla_min_bw=sla_min_bw,
        sla_max_latency=sla_max_latency,
        latency=latency,
        status=status,
        allocations=allocations,
    )


async def load_paths(*, read_tool: Any, scenario: str, src: str, dst: str) -> List[Path]:
    rows = await neo4j_read_records(
        read_tool=read_tool, query=_PATHS_QUERY, params={"scenario": scenario, "src": src, "dst": dst}
    )
    return coerce_paths(rows)


async def load_flow_and_paths(
    *, read_tool: Any, scenario: str, flow_id: str
) -> Tuple[Optional[FlowSnapshot], List[Path]]:
    flow = await load_flow_snapshot(read_tool=read_tool, scenario=scenario, flow_id=flow_id)
    if not flow:
        return None, []
    paths = await load_paths(read_tool=read_tool, scenario=scenario, src=flow.src, dst=flow.dst)
    return flow, paths
