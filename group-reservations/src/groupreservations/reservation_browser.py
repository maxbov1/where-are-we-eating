"""Agent-facing Playwright tools for adaptive restaurant booking pages."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Browser, Page, Playwright, sync_playwright
from strands import tool

from .auth import validate_user_id
from .booking import build_opentable_availability_url, classify_booking_provider

logger = logging.getLogger(__name__)


class ReservationBrowser:
    """One browser/page session shared across a single agent invocation."""

    def __init__(self, user_id: str) -> None:
        validate_user_id(user_id)
        root = Path(os.getenv("GROUP_RESERVATIONS_SESSION_ROOT", ".local/opentable-sessions"))
        self.profile_dir = (root / user_id / "reservation-browser").resolve()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.page: Page | None = None
        self.owner_thread_id: int | None = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reservation-browser")

    def _log(self, stage: str, **details: object) -> None:
        logger.info("reservation_browser stage=%s details=%s", stage, json.dumps(details, default=str))

    def _page(self) -> Page:
        if self.page:
            return self.page
        user_id_hash = hashlib.sha256(self.profile_dir.parent.name.encode()).hexdigest()[:12]
        self._log("browser_start", user_id_hash=user_id_hash)
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True)
        context = self.browser.new_context()
        self.page = context.new_page()
        self.owner_thread_id = threading.get_ident()
        return self.page

    def _run_on_browser_thread(self, operation, *args):
        """Run every sync Playwright operation on its owning thread."""
        if self.owner_thread_id == threading.get_ident():
            return operation(*args)
        return self.executor.submit(operation, *args).result()

    def close(self) -> None:
        # Playwright's sync API is thread-affine. Agent tools may run in a
        # worker thread, so cleanup from the caller must never crash the API
        # with greenlet.error if ownership belongs to that worker.
        if not self.browser and not self.playwright:
            self._log("browser_close_noop")
            return
        try:
            self._log("browser_close_start", has_browser=bool(self.browser))
            self._run_on_browser_thread(self._close_impl)
        except Exception:
            logger.exception("reservation_browser stage=browser_close_failed")
        self.browser = None
        self.playwright = None
        self.page = None
        self.owner_thread_id = None
        if threading.get_ident() != self.owner_thread_id:
            self.executor.shutdown(wait=True)

    def _close_impl(self) -> None:
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()

    def open(self, url: str) -> str:
        return self._run_on_browser_thread(self._open_impl, url)

    def _open_impl(self, url: str) -> str:
        self._log("open_start", url=url)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return json.dumps({"success": False, "error": "Only http(s) booking URLs are allowed."})
        page = self._page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            self._log("open_success", final_url=page.url, http_status=response.status if response else None)
            return json.dumps({
                "success": True,
                "url": page.url,
                "title": page.title(),
                "http_status": response.status if response else None,
            })
        except Exception as exc:
            self._log("open_failed", final_url=page.url, error=str(exc))
            return json.dumps({"success": False, "url": page.url, "error": str(exc)})

    def inspect(self) -> str:
        if not self.page:
            return json.dumps({"success": False, "error": "Open a booking URL first."})
        fields = self.page.locator("input, select, textarea, button").evaluate_all(
            """els => els.slice(0, 80).map(el => ({
                tag: el.tagName.toLowerCase(), type: el.type || null,
                name: el.name || null, id: el.id || null,
                placeholder: el.placeholder || null,
                label: el.getAttribute('aria-label') || el.innerText || null
            }))"""
        )
        frames = [frame.url for frame in self.page.frames if frame.url and frame.url != self.page.url]
        return json.dumps({
            "success": True,
            "url": self.page.url,
            "title": self.page.title(),
            "fields": fields,
            "iframes": frames,
            "text": " ".join(self.page.locator("body").inner_text().split())[:4000],
        })

    def find_booking_links(self) -> str:
        return self._run_on_browser_thread(self._find_booking_links_impl)

    def _find_booking_links_impl(self) -> str:
        """Extract explicit booking links and embedded provider IDs in-place."""
        if not self.page:
            return json.dumps({"success": False, "error": "Open a booking URL first."})
        self._log("dom_scan_start", url=self.page.url)
        candidates = self.page.locator(
            "a[href], iframe[src], [data-ot-restref], [data-ot-path], "
            "[data-reservation-url], [data-booking-url], form[action], "
            "button[data-url], button[data-href]"
        ).evaluate_all(
            """els => els.slice(0, 80).map(el => {
                const href = el.href || el.src || el.action ||
                    el.getAttribute('data-reservation-url') ||
                    el.getAttribute('data-booking-url') ||
                    el.getAttribute('data-url') || el.getAttribute('data-href') || null;
                const embedded = el.getAttribute('data-ot-restref');
                const path = el.getAttribute('data-ot-path') || '/booking/restref/availability';
                let opentableUrl = null;
                if (embedded) {
                    const params = new URLSearchParams(embedded);
                    params.delete('rid');
                    if (params.get('restref')) {
                        opentableUrl = `https://www.opentable.com${path}?${params.toString()}`;
                    }
                }
                const label = el.innerText || el.getAttribute('aria-label') ||
                    el.getAttribute('value') || '';
                const isBooking = !!(embedded || /reserve|reservation|book|table|opentable|resy|tock/i.test(`${href || ''} ${label}`));
                const googleReserve = href && href.includes('/maps/reserve/v/dine/') ? href : null;
                return {tag: el.tagName.toLowerCase(), href, googleReserve, opentableUrl,
                    restref: embedded || null, isBooking};
            }).filter(item => item.href || item.opentableUrl)"""
        )
        self._log("dom_scan_complete", candidate_count=len(candidates), url=self.page.url)
        return json.dumps({"success": True, "url": self.page.url, "candidates": candidates})

    def discover_booking(self, website_url: str, date: str, time: str, party_size: int) -> str:
        """Open one exact restaurant page and resolve its actionable booking path."""
        return self._run_on_browser_thread(
            self._discover_booking_impl, website_url, date, time, party_size
        )

    def _discover_booking_impl(self, website_url: str, date: str, time: str, party_size: int) -> str:
        self._log("booking_discovery_start", website_url=website_url, date=date, time=time, party_size=party_size)
        opened = json.loads(self._open_impl(website_url))
        if not opened.get("success"):
            return json.dumps({"success": False, "booking_url": website_url,
                               "provider": "Restaurant website", "error": opened.get("error")})
        discovered = json.loads(self._find_booking_links_impl())
        candidates = discovered.get("candidates", [])
        google = next((item.get("googleReserve") for item in candidates if item.get("googleReserve")), None)
        if google:
            self._log("booking_discovery_google_reserve", booking_url=google)
            return json.dumps({"success": True, "booking_url": google,
                               "provider": "Google Reserve", "evidence": "Google Maps reservation URL"})
        for item in candidates:
            candidate = item.get("opentableUrl") or item.get("href")
            if candidate and classify_booking_provider(candidate) == "OpenTable":
                prepared = build_opentable_availability_url(
                    candidate, date=date, time=time, party_size=party_size
                )
                self._log("booking_discovery_opentable", booking_url=prepared)
                return json.dumps({"success": True, "booking_url": prepared,
                                   "provider": "OpenTable", "evidence": "restaurant page DOM"})
        external = next((item.get("href") for item in candidates
                         if item.get("href") and item.get("isBooking")), None)
        fallback = external or website_url
        self._log("booking_discovery_fallback", booking_url=fallback,
                  provider=classify_booking_provider(external, website_url) if external else "Restaurant website")
        return json.dumps({"success": True, "booking_url": fallback,
                           "provider": classify_booking_provider(external, website_url)
                           if external else "Restaurant website",
                           "evidence": "restaurant page DOM" if external else "exact restaurant page"})

    def fill(self, field: str, value: str) -> str:
        if not self.page:
            return json.dumps({"success": False, "error": "Open a booking URL first."})
        try:
            locator = self.page.get_by_label(field, exact=False).first
            if not locator.count():
                locator = self.page.locator(
                    f"input[name='{field}'], input[id='{field}'], select[name='{field}'], textarea[name='{field}']"
                ).first
            if locator.count() == 0:
                return json.dumps({"success": False, "error": f"Booking field not found: {field}"})
            if locator.evaluate("el => el.tagName.toLowerCase()") == "select":
                locator.select_option(label=value)
            else:
                locator.fill(value)
            return json.dumps({"success": True, "field": field, "value": value})
        except Exception as exc:
            return json.dumps({"success": False, "field": field, "error": str(exc)})

    def click(self, label: str) -> str:
        """Click a non-submitting control such as Search or Find a table."""
        if not self.page:
            return json.dumps({"success": False, "error": "Open a booking URL first."})
        if re.search(r"book|reserve|confirm|submit|complete", label, re.I):
            return json.dumps({"success": False, "error": "Final booking controls require organizer confirmation."})
        try:
            locator = self.page.get_by_role("button", name=label, exact=False).first
            if locator.count() == 0:
                locator = self.page.get_by_text(label, exact=False).first
            if locator.count() == 0:
                return json.dumps({"success": False, "error": f"Booking control not found: {label}"})
            locator.click()
            return json.dumps({"success": True, "clicked": label, "url": self.page.url})
        except Exception as exc:
            return json.dumps({"success": False, "clicked": label, "error": str(exc)})


def create_reservation_browser_tools(user_id: str) -> tuple[ReservationBrowser, list[object]]:
    """Create tools whose closures share one organizer-scoped browser page."""
    browser = ReservationBrowser(user_id)

    @tool
    def reservation_open(url: str) -> str:
        """Open the exact restaurant booking URL. Do not use guessed URLs."""
        return browser.open(url)

    @tool
    def reservation_inspect(context: str = "") -> str:
        """Inspect visible booking controls, text, and embedded booking frames."""
        return browser.inspect()

    @tool
    def reservation_find_booking_links() -> str:
        """Find exact booking links, Google Reserve URLs, or embedded provider IDs without navigating."""
        return browser.find_booking_links()

    @tool
    def reservation_discover_booking(website_url: str, date: str, time: str, party_size: int) -> str:
        """Resolve one restaurant's exact booking URL and prefill its group details."""
        return browser.discover_booking(website_url, date, time, party_size)

    @tool
    def reservation_fill(field: str, value: str) -> str:
        """Fill one identified booking field; never submits the form."""
        return browser.fill(field, value)

    @tool
    def reservation_click(label: str) -> str:
        """Click a search/availability control, never a final booking control."""
        return browser.click(label)

    @tool
    def reservation_prepare(date: str, time: str, party_size: int) -> str:
        """Record the requested reservation details without submitting a booking."""
        return json.dumps({
            "success": True,
            "prepared": {"date": date, "time": time, "party_size": party_size},
            "next_step": "Inspect the page and fill its controls. Never submit without organizer confirmation.",
        })

    @tool
    def reservation_close() -> str:
        """Close the organizer-scoped browser session after the booking handoff."""
        browser.close()
        return json.dumps({"success": True, "message": "Reservation browser session closed."})

    return browser, [reservation_open, reservation_inspect, reservation_find_booking_links,
                      reservation_discover_booking, reservation_fill, reservation_click,
                      reservation_prepare, reservation_close]
