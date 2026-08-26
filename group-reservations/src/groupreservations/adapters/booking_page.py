"""Copied booking-page evidence adapter from HungryRadar."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

_USER_AGENT = "GroupReservationsBot/0.1 (reservation evidence checks)"


def fetch_page_text(url: str, *, max_chars: int = 4000) -> dict:
    """Fetch visible page text; never claim live availability from a fetch."""
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        response = httpx.get(
            url, headers={"User-Agent": _USER_AGENT}, timeout=10.0, follow_redirects=True
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return {
            "source_uri": url,
            "checked_at": checked_at,
            "fetched": False,
            "error": str(exc),
            "page_text_snippet": "",
        }
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    return {
        "source_uri": url,
        "checked_at": checked_at,
        "fetched": True,
        "page_text_snippet": text[:max_chars],
    }
