MATCH (node {scenario: "QOS_DEMO"}) DETACH DELETE node;

CREATE
  (edge:Device {id: "LB-EDGE-01", scenario: "QOS_DEMO"}),
  (router_a:Device {id: "Router-A", scenario: "QOS_DEMO"}),
  (router_b:Device {id: "Router-B", scenario: "QOS_DEMO"}),
  (router_c:Device {id: "Router-C", scenario: "QOS_DEMO"}),
  (app:Device {id: "LB-APP-01", scenario: "QOS_DEMO"});

MATCH (edge:Device {id: "LB-EDGE-01", scenario: "QOS_DEMO"}),
      (router:Device {id: "Router-A", scenario: "QOS_DEMO"}),
      (app:Device {id: "LB-APP-01", scenario: "QOS_DEMO"})
CREATE
  (edge)-[:CONNECTED_TO {scenario: "QOS_DEMO", capacity: 100.0, load: 9.6, base_latency: 1.0}]->(router),
  (router)-[:CONNECTED_TO {scenario: "QOS_DEMO", capacity: 100.0, load: 9.6, base_latency: 1.0}]->(edge),
  (router)-[:CONNECTED_TO {scenario: "QOS_DEMO", capacity: 10.0, load: 9.6, base_latency: 1.0}]->(app),
  (app)-[:CONNECTED_TO {scenario: "QOS_DEMO", capacity: 10.0, load: 9.6, base_latency: 1.0}]->(router);

MATCH (edge:Device {id: "LB-EDGE-01", scenario: "QOS_DEMO"}),
      (router:Device {id: "Router-B", scenario: "QOS_DEMO"}),
      (app:Device {id: "LB-APP-01", scenario: "QOS_DEMO"})
CREATE
  (edge)-[:CONNECTED_TO {scenario: "QOS_DEMO", capacity: 100.0, load: 1.2, base_latency: 1.0}]->(router),
  (router)-[:CONNECTED_TO {scenario: "QOS_DEMO", capacity: 100.0, load: 1.2, base_latency: 1.0}]->(edge),
  (router)-[:CONNECTED_TO {scenario: "QOS_DEMO", capacity: 100.0, load: 1.2, base_latency: 1.0}]->(app),
  (app)-[:CONNECTED_TO {scenario: "QOS_DEMO", capacity: 100.0, load: 1.2, base_latency: 1.0}]->(router);

MATCH (edge:Device {id: "LB-EDGE-01", scenario: "QOS_DEMO"}),
      (router:Device {id: "Router-C", scenario: "QOS_DEMO"}),
      (app:Device {id: "LB-APP-01", scenario: "QOS_DEMO"})
CREATE
  (edge)-[:CONNECTED_TO {scenario: "QOS_DEMO", capacity: 100.0, load: 1.2, base_latency: 1.0}]->(router),
  (router)-[:CONNECTED_TO {scenario: "QOS_DEMO", capacity: 100.0, load: 1.2, base_latency: 1.0}]->(edge),
  (router)-[:CONNECTED_TO {scenario: "QOS_DEMO", capacity: 50.0, load: 1.2, base_latency: 2.0}]->(app),
  (app)-[:CONNECTED_TO {scenario: "QOS_DEMO", capacity: 50.0, load: 1.2, base_latency: 2.0}]->(router);

CREATE
  (:Path {
    id: "PATH-A",
    scenario: "QOS_DEMO",
    src: "LB-EDGE-01",
    dst: "LB-APP-01",
    nodes: ["LB-EDGE-01", "Router-A", "LB-APP-01"],
    hops: 2
  }),
  (:Path {
    id: "PATH-B",
    scenario: "QOS_DEMO",
    src: "LB-EDGE-01",
    dst: "LB-APP-01",
    nodes: ["LB-EDGE-01", "Router-B", "LB-APP-01"],
    hops: 2
  }),
  (:Path {
    id: "PATH-C",
    scenario: "QOS_DEMO",
    src: "LB-EDGE-01",
    dst: "LB-APP-01",
    nodes: ["LB-EDGE-01", "Router-C", "LB-APP-01"],
    hops: 2
  });

CREATE (:Flow {
  id: "FLOW-001",
  flow_id: "FLOW-001",
  scenario: "QOS_DEMO",
  src: "LB-EDGE-01",
  dst: "LB-APP-01",
  requested_rate: 8.0,
  rate: 8.0,
  sla_min_bw: 5.0,
  sla_max_latency: 20.0,
  latency: 30.0,
  status: "VIOLATED"
});

MATCH (flow:Flow {flow_id: "FLOW-001", scenario: "QOS_DEMO"}),
      (path:Path {id: "PATH-A", scenario: "QOS_DEMO"})
CREATE (flow)-[:USES_PATH {rate_mbps: 6.4}]->(path);

MATCH (flow:Flow {flow_id: "FLOW-001", scenario: "QOS_DEMO"}),
      (path:Path {id: "PATH-B", scenario: "QOS_DEMO"})
CREATE (flow)-[:USES_PATH {rate_mbps: 0.8}]->(path);

MATCH (flow:Flow {flow_id: "FLOW-001", scenario: "QOS_DEMO"}),
      (path:Path {id: "PATH-C", scenario: "QOS_DEMO"})
CREATE (flow)-[:USES_PATH {rate_mbps: 0.8}]->(path);
