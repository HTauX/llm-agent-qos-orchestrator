from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Tuple


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    capacity: float
    load: float
    base_latency: float


@dataclass(frozen=True)
class Path:
    path_id: str
    nodes: List[str]
    hops: int
    edges: List[Edge]


@dataclass(frozen=True)
class Allocation:
    path_id: str
    rate_mbps: float


def queue_ms(*, capacity: float, load: float) -> float:
    if capacity <= 0:
        return 1000.0
    if load >= capacity:
        return 1000.0
    return 10.0 / (capacity - load)


def link_latency_ms(*, base_latency: float, capacity: float, load: float) -> float:
    return float(base_latency) + queue_ms(capacity=float(capacity), load=float(load))


def path_latency_ms(*, edges: Sequence[Edge], delta_mbps: float = 0.0) -> float:
    """
    Estimated path latency assuming the same delta is applied to every edge on the path.
    """

    total = 0.0
    for e in edges:
        total += link_latency_ms(
            base_latency=float(e.base_latency),
            capacity=float(e.capacity),
            load=float(e.load) + float(delta_mbps),
        )
    return total


def min_residual_mbps(*, edges: Sequence[Edge], delta_mbps: float = 0.0) -> float:
    if not edges:
        return 0.0
    return min(float(e.capacity) - (float(e.load) + float(delta_mbps)) for e in edges)


def flow_latency_ms_max(*, paths_by_id: Dict[str, Path], allocations: Sequence[Allocation]) -> float:
    """
    Demo model: a flow's observed latency is the max latency among its used paths.

    Positive allocations require complete path and edge data. Missing topology is
    an input error rather than a zero-latency path.
    """

    latencies: list[float] = []
    for a in allocations:
        if a.rate_mbps <= 0:
            continue
        p = paths_by_id.get(a.path_id)
        if not p:
            raise ValueError(f"Missing Path definition for allocation: {a.path_id}")
        if not p.edges:
            raise ValueError(f"Allocated path has no edge data: {a.path_id}")
        latencies.append(path_latency_ms(edges=p.edges, delta_mbps=0.0))
    return max(latencies) if latencies else 0.0


def normalize_allocations(
    *,
    requested_rate: float,
    allocations: Sequence[Allocation],
    epsilon: float = 1e-6,
) -> Tuple[List[Allocation], float]:
    """
    Return allocations normalized to sum(requested_rate). Positive differences are
    added to the final allocation; excess traffic is removed from the end across
    as many allocations as necessary.
    This is only for demo robustness; planning should try to keep exact conservation itself.
    """

    requested_rate = float(requested_rate)
    if requested_rate < 0.0:
        raise ValueError("requested_rate must be non-negative")

    out = [Allocation(path_id=a.path_id, rate_mbps=float(a.rate_mbps)) for a in allocations]
    if any(a.rate_mbps < 0.0 for a in out):
        raise ValueError("allocation rates must be non-negative")
    if not out:
        return out, requested_rate

    s = sum(a.rate_mbps for a in out)
    diff = requested_rate - s
    if abs(diff) <= epsilon:
        return out, diff

    if diff > 0.0:
        last = out[-1]
        out[-1] = Allocation(path_id=last.path_id, rate_mbps=last.rate_mbps + diff)
        return out, diff

    # If allocations exceed the requested rate, reduce them from the end while
    # keeping every allocation non-negative. Adjusting only the last item can
    # leave the total above requested_rate when that item is too small.
    remaining = -diff
    for index in range(len(out) - 1, -1, -1):
        item = out[index]
        reduction = min(item.rate_mbps, remaining)
        out[index] = Allocation(path_id=item.path_id, rate_mbps=item.rate_mbps - reduction)
        remaining -= reduction
        if remaining <= epsilon:
            break
    return out, diff


def coerce_allocations(value: Any) -> List[Allocation]:
    """
    Coerce allocations from Neo4j records:
    - [{"path_id":"PATH-A","rate_mbps":6.4}, ...]
    """

    out: List[Allocation] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if not isinstance(item, dict):
            continue
        pid = item.get("path_id")
        rate = item.get("rate_mbps")
        if not isinstance(pid, str) or not pid.strip():
            continue
        try:
            r = float(rate)
        except Exception:
            r = 0.0
        out.append(Allocation(path_id=pid.strip(), rate_mbps=r))
    return out


def coerce_paths(records: Iterable[Dict[str, Any]]) -> List[Path]:
    """
    Coerce Path records (as returned by Cypher) into typed objects.

    Expected keys:
    - path_id
    - nodes
    - hops
    - edges: [{src,dst,capacity,load,base_latency}, ...]
    """

    out: List[Path] = []
    for r in records:
        pid = r.get("path_id")
        if not isinstance(pid, str) or not pid.strip():
            continue
        nodes_raw = r.get("nodes")
        nodes = [str(x) for x in nodes_raw] if isinstance(nodes_raw, list) else []
        try:
            hops = int(r.get("hops") or (len(nodes) - 1))
        except Exception:
            hops = len(nodes) - 1 if nodes else 0

        edges_out: List[Edge] = []
        edges_raw = r.get("edges")
        if isinstance(edges_raw, list):
            for e in edges_raw:
                if not isinstance(e, dict):
                    continue
                src = e.get("src")
                dst = e.get("dst")
                if not isinstance(src, str) or not isinstance(dst, str):
                    continue
                try:
                    capacity = float(e.get("capacity", 0.0))
                    load = float(e.get("load", 0.0))
                    base_latency = float(e.get("base_latency", 0.0))
                except Exception:
                    continue
                edges_out.append(Edge(src=src, dst=dst, capacity=capacity, load=load, base_latency=base_latency))

        out.append(Path(path_id=pid.strip(), nodes=nodes, hops=hops, edges=edges_out))
    return out


def candidate_metrics_for_path(*, path: Path, delta_step_mbps: float) -> Dict[str, Any]:
    step = float(delta_step_mbps)
    edges = [
        {
            "src": edge.src,
            "dst": edge.dst,
            "capacity": float(edge.capacity),
            "load": float(edge.load),
            "base_latency": float(edge.base_latency),
        }
        for edge in path.edges
    ]
    bottleneck = min(
        path.edges,
        key=lambda edge: float(edge.capacity) - float(edge.load),
        default=None,
    )
    return {
        "path_id": path.path_id,
        "path_nodes": list(path.nodes),
        "hops": int(path.hops),
        "edges": edges,
        "min_residual_before": min_residual_mbps(edges=path.edges, delta_mbps=0.0),
        "max_additional_rate_mbps": max(0.0, min_residual_mbps(edges=path.edges, delta_mbps=0.0)),
        "est_latency_current_ms": path_latency_ms(edges=path.edges, delta_mbps=0.0),
        "est_latency_if_add_step_ms": path_latency_ms(edges=path.edges, delta_mbps=step),
        "delta_step_mbps": step,
        "bottleneck_edge": ({"src": bottleneck.src, "dst": bottleneck.dst} if bottleneck is not None else None),
    }
