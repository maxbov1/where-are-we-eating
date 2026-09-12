"""Environment-backed configuration for local agent development."""

from __future__ import annotations

import os
import json
from dataclasses import dataclass

import boto3

from dotenv import load_dotenv

load_dotenv()


def _google_places_api_key() -> str:
    """Load the Places key locally or from the configured Secrets Manager ARN."""
    secret_arn = os.getenv("GOOGLE_MAPS_SECRET_ARN", "").strip()
    if not secret_arn:
        return os.getenv("GOOGLE_MAPS_API_KEY", "")

    # The secret may live in a different region from the runtime. Using the
    # ARN's region avoids accidentally querying the runtime's default region.
    arn_parts = secret_arn.split(":")
    secret_region = arn_parts[3] if len(arn_parts) > 3 and arn_parts[3] else None
    client = boto3.client("secretsmanager", region_name=secret_region)
    response = client.get_secret_value(SecretId=secret_arn)
    secret_string = response.get("SecretString", "")
    try:
        secret_value = json.loads(secret_string)
    except json.JSONDecodeError:
        return secret_string
    if isinstance(secret_value, dict):
        return str(secret_value.get("GOOGLE_MAPS_API_KEY", ""))
    return ""


@dataclass(frozen=True)
class Settings:
    aws_region: str = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-west-2"))
    model_id: str = os.getenv(
        "GROUP_RESERVATIONS_MODEL_ID", "global.anthropic.claude-sonnet-4-6"
    )
    google_places_api_key: str = _google_places_api_key()
    database_path: str = os.getenv(
        "GROUP_RESERVATIONS_DATABASE_PATH", ".local/group-reservations.sqlite3"
    )
    public_app_url: str = os.getenv("PUBLIC_APP_URL", "http://localhost:4173")
    recommendation_timeout_seconds: int = int(
        os.getenv("GROUP_RESERVATIONS_RECOMMENDATION_TIMEOUT_SECONDS", "120")
    )
    cors_allowed_origins: str = os.getenv(
        "GROUP_RESERVATIONS_CORS_ORIGINS", "http://localhost:4173,http://127.0.0.1:4173"
    )
    require_auth: bool = os.getenv("GROUP_RESERVATIONS_REQUIRE_AUTH", "false").casefold() == "true"
    rate_limit_requests_per_minute: int = int(
        os.getenv("GROUP_RESERVATIONS_RATE_LIMIT_PER_MINUTE", "120")
    )


settings = Settings()
