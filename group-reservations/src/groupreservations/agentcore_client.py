"""Client boundary from the public API to the deployed AgentCore runtime."""

from __future__ import annotations

import json
import logging
import uuid

import boto3

from .config import settings

logger = logging.getLogger(__name__)


def invoke_agentcore(prompt: str, *, user_id: str, session_id: str | None = None,
                     mode: str = "full") -> str:
    """Invoke the configured AgentCore runtime and return its agent text."""
    runtime_arn = settings.agentcore_runtime_arn.strip()
    if not runtime_arn:
        raise RuntimeError(
            "AGENTCORE_RUNTIME_ARN is not configured; refusing to run the agent inside FastAPI."
        )

    client = boto3.client("bedrock-agentcore", region_name=settings.agentcore_region)
    session_id = session_id or f"wawe-{uuid.uuid4()}"
    payload = json.dumps({"prompt": prompt, "user_id": user_id, "mode": mode}).encode("utf-8")
    logger.info("agentcore invoke runtime_arn=%s session_id=%s prompt_chars=%d", runtime_arn, session_id, len(prompt))
    response = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        payload=payload,
        qualifier="DEFAULT",
        contentType="application/json",
        accept="application/json",
    )
    body = response["response"]
    raw = body.read() if hasattr(body, "read") else body
    decoded = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    envelope = json.loads(decoded)
    result = envelope.get("result") if isinstance(envelope, dict) else None
    if not isinstance(result, str):
        raise RuntimeError("AgentCore returned an invalid response envelope.")
    return result
