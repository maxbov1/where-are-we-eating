"""Environment-backed configuration for local model and MCP development."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    aws_region: str = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-west-2"))
    model_id: str = os.getenv(
        "GROUP_RESERVATIONS_MODEL_ID", "global.anthropic.claude-sonnet-4-6"
    )
    google_places_api_key: str = os.getenv("GOOGLE_MAPS_API_KEY", "")
    opentable_location: str = os.getenv("OPENTABLE_LOCATION", "San Francisco, CA")
    database_path: str = os.getenv(
        "GROUP_RESERVATIONS_DATABASE_PATH", ".local/group-reservations.sqlite3"
    )
    public_app_url: str = os.getenv("PUBLIC_APP_URL", "http://localhost:4173")


settings = Settings()
