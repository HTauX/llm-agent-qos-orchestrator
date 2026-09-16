from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
from a2a.client.card_resolver import A2ACardResolver
from a2a.client.client import ClientConfig
from a2a.client.client_factory import ClientFactory
from a2a.client.middleware import ClientCallContext
from a2a.types import DataPart, Message, Part, Role, Task, TextPart, TransportProtocol


@dataclass(frozen=True)
class A2AResponse:
    """
    Lightweight normalization over a2a-sdk return values (no new protocol, only for easier business consumption).
    """

    raw: Any
    text: str
    json_obj: Optional[Dict[str, Any]]


def _normalize_agent_card_url(agent_card_url_or_base: str) -> str:
    """
    Supports two kinds of inputs:
    - Full Agent Card URL: `http://host:port/.well-known/agent-card.json`
    - Base URL: `http://host:port` (automatically appends the well-known path)
    """

    u = agent_card_url_or_base.rstrip("/")
    if u.endswith(".json") or u.endswith("/.well-known/agent.json") or u.endswith("/.well-known/agent-card.json"):
        return u
    return u + "/.well-known/agent-card.json"


async def send_text_message(
    *,
    agent_card_url_or_base: str,
    text: str,
    context_id: Optional[str] = None,
    timeout_s: float = 30.0,
) -> A2AResponse:
    """
    Send a text message using the a2a-sdk Client (non-streaming, wait for the final result).
    """

    agent_card_url = _normalize_agent_card_url(agent_card_url_or_base)
    parsed = urlparse(agent_card_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid agent_card_url: {agent_card_url}")

    base_url = f"{parsed.scheme}://{parsed.netloc}"
    relative_path = parsed.path

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as http_client:
        resolver = A2ACardResolver(httpx_client=http_client, base_url=base_url)
        agent_card = await resolver.get_agent_card(relative_card_path=relative_path)

        factory = ClientFactory(
            config=ClientConfig(
                httpx_client=http_client,
                streaming=False,
                polling=False,
                supported_transports=[TransportProtocol.jsonrpc],
            )
        )
        client = factory.create(agent_card)

        req = Message(
            message_id=str(uuid.uuid4()),
            role=Role.user,
            parts=[Part(root=TextPart(text=text))],
            context_id=context_id,
        )

        last: Any = None
        async for resp in client.send_message(
            request=req,
            context=ClientCallContext(state={}),
        ):
            last = resp

        text_out = _extract_text_from_a2a_response(last)
        json_obj = _try_parse_json(text_out)
        return A2AResponse(raw=last, text=text_out, json_obj=json_obj)


def _try_parse_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _extract_text_from_a2a_response(resp: Any) -> str:
    """
    a2a-sdk send_message may return:
    - Message
    - (Task, TaskStatusUpdateEvent | TaskArtifactUpdateEvent | None)

    This extracts the "final readable text" for display and further parsing.
    """

    if resp is None:
        return ""

    # Tuple form: (Task, update)
    if isinstance(resp, tuple) and len(resp) == 2 and isinstance(resp[0], Task):
        task: Task = resp[0]
        # Prefer artifacts, then status.message, then history
        if task.artifacts:
            return _parts_to_text(task.artifacts[-1].parts or [])
        if task.status and task.status.message and task.status.message.parts:
            return _parts_to_text(task.status.message.parts)
        if task.history:
            return _parts_to_text(task.history[-1].parts or [])
        return ""

    # Message form
    if isinstance(resp, Message):
        return _parts_to_text(resp.parts or [])

    # Fallback: convert unknown types to string (useful for debugging)
    return str(resp)


def _parts_to_text(parts: list[Part]) -> str:
    texts: list[str] = []
    for p in parts:
        root = getattr(p, "root", None)
        if isinstance(root, TextPart):
            texts.append(root.text)
        elif isinstance(root, DataPart):
            # DataPart.data is usually a dict; convert it to a JSON string here
            try:
                texts.append(json.dumps(root.data, ensure_ascii=False))
            except Exception:
                texts.append(str(root.data))
    return "\n".join([t for t in texts if t])
