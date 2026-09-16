"""Parsing and validation for A2A executor requests."""

from __future__ import annotations

from typing import Any, Dict, Optional

from qos_system_lg.llm_client import try_parse_json_object


def parse_executor_request(user_text: str) -> tuple[Optional[str], Dict[str, Any], str]:
    """Extract capability, plan, and CLI configuration from an A2A message."""

    if not user_text:
        return None, {}, ""

    payload = try_parse_json_object(user_text)
    if not isinstance(payload, dict):
        return None, {}, ""

    capability: Optional[str] = None
    for key in ("capability", "action", "type"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            capability = value.strip()
            break

    def _coerce_plan(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            parsed = try_parse_json_object(value)
            if isinstance(parsed, dict):
                return parsed
        return {}

    def _coerce_cli(value: Any) -> str:
        if isinstance(value, str):
            return value
        if value is None:
            return ""
        return str(value)

    plan: Dict[str, Any] = {}
    cli_config = ""

    params = payload.get("params")
    if isinstance(params, dict):
        plan = _coerce_plan(params.get("plan"))
        cli_config = _coerce_cli(params.get("cli_config"))

    if not plan:
        plan = _coerce_plan(payload.get("plan"))
    if not cli_config:
        cli_config = _coerce_cli(payload.get("cli_config"))

    data = payload.get("data")
    if isinstance(data, dict):
        if not plan:
            plan = _coerce_plan(data.get("plan"))
        if not cli_config:
            cli_config = _coerce_cli(data.get("cli_config"))

    if not capability and plan:
        capability = "execute_change"

    if not cli_config:
        embedded = plan.get("cli_config")
        if isinstance(embedded, str) and embedded:
            cli_config = embedded

    return capability, plan, cli_config


def validate_executor_request(
    *,
    capability: Optional[str],
    plan: Dict[str, Any],
    cli_config: str,
) -> Optional[str]:
    """Return a validation error, or ``None`` when the request is executable."""

    if capability != "execute_change":
        return "unsupported or missing capability"
    if not plan:
        return "missing remediation plan"
    if not isinstance(plan.get("plan_id"), str) or not plan["plan_id"].strip():
        return "missing plan_id"
    if plan.get("plan_ok") is not True:
        return "plan_ok must be explicitly true"
    if not cli_config.strip():
        return "missing cli_config"
    return None
