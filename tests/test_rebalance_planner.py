import unittest

from qos_system_lg.multipath import (
    Allocation,
    Edge,
    Path,
    candidate_metrics_for_path,
    flow_latency_ms_max,
    normalize_allocations,
)
from qos_system_lg.multipath_analysis import build_candidate_analysis
from qos_system_lg.multipath_repo import FlowSnapshot
from qos_system_lg.rebalance_planner import build_plan_payload, plan_atomic_rebalance


class TestRebalancePlanner(unittest.TestCase):
    def test_normalize_allocations_reduces_across_multiple_paths(self) -> None:
        normalized, difference = normalize_allocations(
            requested_rate=3.0,
            allocations=[
                Allocation(path_id="PATH-A", rate_mbps=4.0),
                Allocation(path_id="PATH-B", rate_mbps=1.0),
            ],
        )

        self.assertEqual(difference, -2.0)
        self.assertAlmostEqual(sum(item.rate_mbps for item in normalized), 3.0)
        self.assertTrue(all(item.rate_mbps >= 0.0 for item in normalized))

    def test_candidate_metrics_include_edges_and_bottleneck(self) -> None:
        path = Path(
            path_id="PATH-A",
            nodes=["A", "B", "C"],
            hops=2,
            edges=[
                Edge(src="A", dst="B", capacity=10.0, load=4.0, base_latency=1.0),
                Edge(src="B", dst="C", capacity=8.0, load=6.0, base_latency=1.0),
            ],
        )

        metrics = candidate_metrics_for_path(path=path, delta_step_mbps=1.0)

        self.assertEqual(len(metrics["edges"]), 2)
        self.assertEqual(metrics["bottleneck_edge"], {"src": "B", "dst": "C"})

    def test_empty_candidate_path_is_never_selected(self) -> None:
        hot_path = Path(
            path_id="PATH-HOT",
            nodes=["A", "B"],
            hops=1,
            edges=[Edge(src="A", dst="B", capacity=10.0, load=9.5, base_latency=1.0)],
        )
        empty_path = Path(path_id="PATH-EMPTY", nodes=["A", "B"], hops=1, edges=[])
        flow = FlowSnapshot(
            flow_id="FLOW-EMPTY-PATH",
            scenario="QOS_DEMO",
            src="A",
            dst="B",
            requested_rate=2.0,
            sla_min_bw=0.0,
            sla_max_latency=5.0,
            latency=30.0,
            status="VIOLATED",
            allocations=[Allocation(path_id="PATH-HOT", rate_mbps=2.0)],
        )

        result = plan_atomic_rebalance(flow=flow, paths=[hot_path, empty_path])

        self.assertFalse(result.ok)
        self.assertIn("No alternative path", result.reason)
        self.assertNotIn("PATH-EMPTY", {item.path_id for item in result.allocations_after})

    def test_analysis_excludes_paths_without_edges(self) -> None:
        hot_path = Path(
            path_id="PATH-HOT",
            nodes=["A", "B"],
            hops=1,
            edges=[Edge(src="A", dst="B", capacity=10.0, load=9.0, base_latency=1.0)],
        )
        empty_path = Path(path_id="PATH-EMPTY", nodes=["A", "B"], hops=1, edges=[])
        flow = FlowSnapshot(
            flow_id="FLOW-001",
            scenario="QOS_DEMO",
            src="A",
            dst="B",
            requested_rate=1.0,
            sla_min_bw=0.0,
            sla_max_latency=20.0,
            latency=30.0,
            status="VIOLATED",
            allocations=[Allocation(path_id="PATH-HOT", rate_mbps=1.0)],
        )

        analysis = build_candidate_analysis(flow=flow, paths=[hot_path, empty_path])

        candidate_ids = {item["path_id"] for item in analysis["candidates"]}
        self.assertNotIn("PATH-EMPTY", candidate_ids)

    def test_flow_latency_rejects_missing_allocated_path_data(self) -> None:
        with self.assertRaisesRegex(ValueError, "no edge data"):
            flow_latency_ms_max(
                paths_by_id={"PATH-A": Path(path_id="PATH-A", nodes=["A", "B"], hops=1, edges=[])},
                allocations=[Allocation(path_id="PATH-A", rate_mbps=1.0)],
            )

    def test_plan_atomic_rebalance_success(self) -> None:
        # 3-branch paths: LB -> Router-X -> APP
        path_a = Path(
            path_id="PATH-A",
            nodes=["LB-EDGE-01", "Router-A", "LB-APP-01"],
            hops=2,
            edges=[
                Edge(src="LB-EDGE-01", dst="Router-A", capacity=100.0, load=9.6, base_latency=1.0),
                Edge(src="Router-A", dst="LB-APP-01", capacity=10.0, load=9.6, base_latency=1.0),
            ],
        )
        path_b = Path(
            path_id="PATH-B",
            nodes=["LB-EDGE-01", "Router-B", "LB-APP-01"],
            hops=2,
            edges=[
                Edge(src="LB-EDGE-01", dst="Router-B", capacity=100.0, load=1.2, base_latency=1.0),
                Edge(src="Router-B", dst="LB-APP-01", capacity=100.0, load=1.2, base_latency=1.0),
            ],
        )
        path_c = Path(
            path_id="PATH-C",
            nodes=["LB-EDGE-01", "Router-C", "LB-APP-01"],
            hops=2,
            edges=[
                Edge(src="LB-EDGE-01", dst="Router-C", capacity=100.0, load=1.2, base_latency=1.0),
                Edge(src="Router-C", dst="LB-APP-01", capacity=50.0, load=1.2, base_latency=2.0),
            ],
        )

        flow = FlowSnapshot(
            flow_id="FLOW-001",
            scenario="QOS_DEMO",
            src="LB-EDGE-01",
            dst="LB-APP-01",
            requested_rate=8.0,
            sla_min_bw=0.0,
            sla_max_latency=20.0,
            latency=30.0,
            status="VIOLATED",
            allocations=[
                Allocation(path_id="PATH-A", rate_mbps=6.4),
                Allocation(path_id="PATH-B", rate_mbps=0.8),
                Allocation(path_id="PATH-C", rate_mbps=0.8),
            ],
        )

        result = plan_atomic_rebalance(flow=flow, paths=[path_a, path_b, path_c], delta_step_mbps=1.0, max_iters=8)
        self.assertTrue(result.ok, msg=result.reason)

        after = {a.path_id: a.rate_mbps for a in result.allocations_after}
        # Expect at least 1 Mbps shifted away from the hot PATH-A.
        self.assertLess(after.get("PATH-A", 0.0), 6.4)
        # Conservation should still hold (within floating error).
        self.assertAlmostEqual(sum(after.values()), 8.0, places=6)

    def test_build_plan_payload_contains_atomic_write(self) -> None:
        path_a = Path(
            path_id="PATH-A",
            nodes=["LB-EDGE-01", "Router-A", "LB-APP-01"],
            hops=2,
            edges=[
                Edge(src="LB-EDGE-01", dst="Router-A", capacity=100.0, load=9.6, base_latency=1.0),
                Edge(src="Router-A", dst="LB-APP-01", capacity=10.0, load=9.6, base_latency=1.0),
            ],
        )
        path_b = Path(
            path_id="PATH-B",
            nodes=["LB-EDGE-01", "Router-B", "LB-APP-01"],
            hops=2,
            edges=[
                Edge(src="LB-EDGE-01", dst="Router-B", capacity=100.0, load=1.2, base_latency=1.0),
                Edge(src="Router-B", dst="LB-APP-01", capacity=100.0, load=1.2, base_latency=1.0),
            ],
        )
        flow = FlowSnapshot(
            flow_id="FLOW-001",
            scenario="QOS_DEMO",
            src="LB-EDGE-01",
            dst="LB-APP-01",
            requested_rate=8.0,
            sla_min_bw=0.0,
            sla_max_latency=20.0,
            latency=30.0,
            status="VIOLATED",
            allocations=[Allocation(path_id="PATH-A", rate_mbps=8.0)],
        )

        plan = build_plan_payload(flow=flow, paths=[path_a, path_b], delta_step_mbps=1.0)
        self.assertIn("neo4j_atomic_write", plan)
        atomic = plan["neo4j_atomic_write"]
        self.assertIn("query", atomic)
        self.assertIn("params", atomic)
        self.assertIsInstance(atomic["params"].get("updates"), list)
        self.assertIsInstance(atomic["params"].get("allocations_after"), list)
        # Neo4j 5+ importing WITH restrictions: do NOT attach WHERE to the importing WITH.
        self.assertNotIn("WITH ok, f WHERE", atomic["query"])
        # Neo4j requires a WITH boundary between FOREACH and CALL.
        self.assertIn("WITH ok, f, matched, expected\nCALL {", atomic["query"])
        self.assertIn("UNWIND CASE WHEN ok THEN $allocations_after ELSE [] END AS a", atomic["query"])


if __name__ == "__main__":
    unittest.main()
