from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.genai import types as genai_types
from typing_extensions import override

from qos_system_lg.app_config import get_prompt
from qos_system_lg.execution_contract import parse_executor_request, validate_executor_request
from qos_system_lg.llm_client import build_chat_llm, llm_label, try_parse_json_object

_DEFAULT_EXECUTOR_SYSTEM_PROMPT = (
    "You are a network automation executor (Executor). You will receive a JSON request containing capability, plan, and cli_config.\n"
    "Please simulate a real change execution process (may include checks, deployment, validation, rollback decision, etc.), and output strict JSON (no Markdown, no explanation):\n"
    "{\n"
    '  "status": "Success|Failure|Rollback",\n'
    '  "log": "...multi-line text..."\n'
    "}\n"
    "Requirements: the log must contain plan_id and briefly describe what was executed."
)


class QosConfigExecutorAgent(BaseAgent):
    """
    QoS demo “executor” Agent (real ADK Agent).

    Design goals:
    - By default, do not depend on an external LLM (ensure the demo can run offline); if a real LLM is configured, enable it.
    - Receive “change execution requests” sent by the Orchestrator via the A2A protocol and return execution results.

    Input contract (a JSON text sent by the Orchestrator; to be compatible with LLM outputs, multiple equivalent structures are supported):
    1) Recommended (flat structure):
       {"capability":"execute_change","plan":{...},"cli_config":"..."}
    2) Compatible (with params):
       {"capability":"execute_change","params":{"plan":{...},"cli_config":"..."}}

    Output contract (returned JSON text):
    {
      "status": "Success|Failure|Rollback",
      "log": "..."
    }
    """

    @override
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        user_text = _extract_latest_user_text(ctx)

        capability, plan, cli_config = parse_executor_request(user_text)

        # 2) Real LLM execution (optional): enabled when a real LLM is configured; otherwise keep offline simulation
        plan_id = plan.get("plan_id") if isinstance(plan, dict) else None
        ts = datetime.now(timezone.utc).isoformat()

        result: Dict[str, Any]
        request_error = validate_executor_request(
            capability=capability,
            plan=plan,
            cli_config=cli_config,
        )
        llm = build_chat_llm(temperature=0.2, streaming=False)
        if request_error:
            result = {
                "status": "Failure",
                "log": "\n".join(
                    [
                        f"[QosConfigExecutorAgent] ts={ts}",
                        f"capability={capability or 'unknown'}",
                        f"plan_id={plan_id or '<missing>'}",
                        f"error={request_error}",
                    ]
                ),
            }
        elif llm:
            try:
                from langchain_core.messages import HumanMessage
                from langgraph.prebuilt import create_react_agent

                system_text = get_prompt("executor_system", fallback=_DEFAULT_EXECUTOR_SYSTEM_PROMPT)
                user_payload = {
                    "ts": ts,
                    "capability": capability,
                    "plan": plan,
                    "cli_config": cli_config,
                }
                print(
                    f"[LLM][Executor] {llm_label()} INPUT:\n{json.dumps(user_payload, ensure_ascii=False, indent=2, default=str)}",
                    flush=True,
                )

                agent = create_react_agent(llm, tools=[], prompt=system_text, version="v2")
                result = await agent.ainvoke(
                    {"messages": [HumanMessage(content=json.dumps(user_payload, ensure_ascii=False, indent=2))]},
                    config={"recursion_limit": 50},
                )
                messages = result.get("messages") or []
                llm_text = ""
                for m in reversed(messages):
                    if getattr(m, "content", None):
                        llm_text = m.content if isinstance(m.content, str) else str(m.content)
                        break

                print(f"[LLM][Executor] {llm_label()} OUTPUT:\n{llm_text}", flush=True)

                parsed = try_parse_json_object(llm_text)

                if isinstance(parsed, dict) and isinstance(parsed.get("log"), str):
                    status = parsed.get("status") if isinstance(parsed.get("status"), str) else "Failure"
                    if status not in {"Success", "Failure", "Rollback"}:
                        status = "Failure"
                    result = {"status": status, "log": parsed["log"]}
                else:
                    # Fail closed when the LLM response does not satisfy the JSON contract.
                    result = {"status": "Failure", "log": f"Invalid executor response: {llm_text}"}
            except Exception as e:
                print(f"[LLM][Executor] {llm_label()} ERROR: {e}", flush=True)
                result = {
                    "status": "Failure",
                    "log": "\n".join(
                        [
                            f"[QosConfigExecutorAgent] ts={ts}",
                            f"capability={capability or 'unknown'}",
                            f"plan_id={plan_id or '<missing>'}",
                            f"error={e}",
                        ]
                    ),
                }
        else:
            # Offline simulated execution: no external LLM dependency, ensures the demo can run offline
            log_lines = [
                f"[QosConfigExecutorAgent] ts={ts}",
                f"capability={capability or 'unknown'}",
                f"plan_id={plan_id or '<missing>'}",
                "cli_config_preview=" + (cli_config[:200] + ("..." if len(cli_config) > 200 else "")),
                "result=Success (simulated)",
            ]
            result = {"status": "Success", "log": "\n".join(log_lines)}

        result_text = json.dumps(result, ensure_ascii=False)

        # 3) Return as an ADK Event (the A2A side will wrap it into a Task/Artifact)
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            branch=ctx.branch,
            content=genai_types.Content(
                role="model",
                parts=[genai_types.Part.from_text(text=result_text)],
            ),
        )


def _extract_latest_user_text(ctx: InvocationContext) -> str:
    """
    Try to extract the text content of the “latest user input” from the InvocationContext.
    """

    # Prefer the user_content of the current invocation (if populated by the Runner)
    if ctx.user_content and ctx.user_content.parts:
        for part in ctx.user_content.parts:
            if getattr(part, "text", None):
                return str(part.text)

    # Otherwise, search backwards in session events for the latest user event
    for event in reversed(ctx.session.events or []):
        if event.author != "user":
            continue
        if not event.content or not event.content.parts:
            continue
        for part in event.content.parts:
            if getattr(part, "text", None):
                return str(part.text)

    return ""
