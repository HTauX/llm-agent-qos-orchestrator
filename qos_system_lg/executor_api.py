from __future__ import annotations

import os

from dotenv import load_dotenv
from google.adk.a2a.utils.agent_to_a2a import to_a2a

from qos_system_lg.executor_agent import QosConfigExecutorAgent

"""
Real A2A executor service (based on Google ADK + a2a-sdk).

Description:
- `to_a2a()` converts an ADK Agent into a standard A2A service (Starlette app).
- This service automatically provides:
  - Agent Card (default `/.well-known/agent-card.json`)
  - A2A JSON-RPC Endpoint (default `/`)

Run example:
  EXECUTOR_HOST=127.0.0.1 EXECUTOR_PORT=8010 \\
    python -m uvicorn qos_system_lg.executor_api:app --host 127.0.0.1 --port 8010

Notes:
- `EXECUTOR_PORT` must match uvicorn’s `--port`, otherwise the URL in the Agent Card will not match.
"""

_here = os.path.dirname(os.path.abspath(__file__))
# First load `qos-system-lg/.env` (for self-contained configuration in this directory),
# then load `.env` from the CWD (for backward compatibility).
load_dotenv(os.path.join(_here, "..", ".env"))
load_dotenv()


def _get_host_port() -> tuple[str, int]:
    host = os.getenv("EXECUTOR_HOST", "127.0.0.1")
    port = int(os.getenv("EXECUTOR_PORT", "8010"))
    return host, port


_host, _port = _get_host_port()

# Executor Agent: use a real LLM when configured (Gemini/OpenAI-compatible); otherwise use offline simulation
# (to ensure the demo can run offline).
executor_agent = QosConfigExecutorAgent(
    name="qos_config_executor",
    description="Execute remediation plans / push configurations (demo: simulated execution).",
)

# Generate a standard A2A service (Starlette app)
app = to_a2a(executor_agent, host=_host, port=_port, protocol="http")
