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
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit

from playwright.sync_api import Browser, Page, Playwright, sync_playwright
from strands import tool

from .auth import validate_user_id
from .agent_state import AgentState
from .booking import build_opentable_availability_url, classify_booking_provider

logger = logging.getLogger(__name__)

_RESERVATION_WORDS = re.compile(
    r"\b(?:reserv(?:e|ation|ations|ing)?|book(?:ing)?|find\s+a\s+table|"
    r"availability|waitlist|call\s+for\s+reservations?)\b",
    re.I,
)
_PHONE_RESERVATION_WORDS = re.compile(
    r"\b(?:call|phone|text)\b.{0,40}\b(?:reserv\w*|book\w*|table)\b|"
    r"\b(?:reserv\w*|book\w*|table)\b.{0,40}\b(?:call|phone|text)\b",
    re.I,
)
_PHONE_NUMBER = re.compile(r"(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-])\d{3}[\s.-]\d{4}")
_ID_ATTRIBUTE_WORDS = re.compile(
    r"(?:restref|restaurant[-_]?id|venue[-_]?id|location[-_]?id|provider[-_]?id)",
    re.I,
)
_NON_ACTIONABLE_TAGS = {"script", "link", "style", "meta", "noscript", "title", "html", "head", "body"}


def _candidate_score(candidate: dict[str, object], page_url: str) -> tuple[int, str]:
    """Score reservation evidence without requiring a known provider."""
    label = str(candidate.get("label") or "")
    nearby_text = str(candidate.get("nearby_text") or "")
    href = str(candidate.get("href") or "")
    attribute_map = candidate.get("attributes") or {}
    attributes = " ".join(
        f"{name} {value}" for name, value in attribute_map.items()
    ) if isinstance(attribute_map, dict) else str(attribute_map)
    signal_text = f"{label} {nearby_text} {href} {attributes}"
    score = 0
    if _RESERVATION_WORDS.search(label):
        score += 5
    if _RESERVATION_WORDS.search(nearby_text):
        score += 4
    if _RESERVATION_WORDS.search(href):
        score += 4
    if candidate.get("iframe"):
        # A rendered reservation iframe is an actionable booking surface.
        # Prefer it over page-resource URLs that only inherit reservation text.
        score += 5
    has_phone = bool(candidate.get("tel") or _PHONE_NUMBER.search(signal_text))
    if has_phone:
        score += 3 if _PHONE_RESERVATION_WORDS.search(signal_text) else 0
    if _ID_ATTRIBUTE_WORDS.search(attributes):
        score += 4
    host = (urlsplit(href).hostname or "").lower()
    page_host = (urlsplit(page_url).hostname or "").lower()
    if host and page_host and host.removeprefix("www.") != page_host.removeprefix("www."):
        score += 2
    confidence = "high" if score >= 8 else "possible" if score >= 4 else "low"
    return score, confidence


def _provider_identifiers(candidate: dict[str, object]) -> list[dict[str, str]]:
    """Extract provider-neutral IDs, leaving provider interpretation downstream."""
    attributes = candidate.get("attributes") or {}
    if not isinstance(attributes, dict):
        return []
    identifiers = []
    for name, value in attributes.items():
        if value and _ID_ATTRIBUTE_WORDS.search(str(name)):
            identifiers.append({"attribute": str(name), "value": str(value)})
    return identifiers


def _matches_location(candidate: dict[str, object], location: str) -> bool:
    """Match a discovered booking card to the event location when available."""
    terms = [term for term in re.findall(r"[a-z0-9]+", location.casefold()) if len(term) > 2]
    if not terms:
        return True
    context = " ".join(
        str(candidate.get(key) or "")
        for key in ("label", "nearby_text", "container_text", "href")
    ).casefold()
    return all(term in context for term in terms)


def _candidate_id(url: str, label: str = "") -> str:
    """Create a stable, provider-neutral identity for one observed candidate."""
    value = f"{url.strip()}|{label.strip()}"
    return f"candidate-{hashlib.sha256(value.encode()).hexdigest()[:16]}"


class ReservationBrowser:
    """One browser/page session shared across a single agent invocation."""

    def __init__(self, user_id: str, state: AgentState | None = None) -> None:
        validate_user_id(user_id)
        root = Path(os.getenv("GROUP_RESERVATIONS_SESSION_ROOT", ".local/opentable-sessions"))
        self.profile_dir = (root / user_id / "reservation-browser").resolve()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.page: Page | None = None
        self.owner_thread_id: int | None = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reservation-browser")
        self.state = state

    def _response(self, payload: dict[str, object], actions: tuple[tuple[str, str], ...] = (),
                  *, phase: str | None = None, reason: str = "browser observation") -> str:
        candidate = self.state.current_candidate if self.state else {}
        result_url = str(payload.get("url") or candidate.get("url") or "")
        if payload.get("candidate_id"):
            result_id = str(payload["candidate_id"])
        elif payload.get("url"):
            # An explicit URL is a new observation. Never attach it to the
            # previous page's candidate identity after a navigation failure.
            result_id = _candidate_id(result_url)
        else:
            result_id = str(candidate.get("candidate_id") or "")
        if result_url and not result_id:
            result_id = _candidate_id(result_url)
        if result_id:
            payload.setdefault("candidate_id", result_id)
            payload.setdefault("candidate", {"candidate_id": result_id, "url": result_url})
        if self.state:
            if phase:
                if "url" in payload:
                    self.state.browser.update({
                        key: payload[key] for key in ("url", "title", "http_status")
                        if key in payload
                    })
                if "booking_url" in payload:
                    self.state.reservation.update({
                        key: payload[key] for key in ("booking_url", "prepared_booking_url", "provider", "evidence")
                        if key in payload
                    })
                if isinstance(payload.get("candidate"), dict):
                    self.state.current_candidate = dict(payload["candidate"])
                self.state.status = (
                    "failed" if payload.get("success") is False
                    else "complete" if phase == "cleanup"
                    else "running"
                )
                transition_phase = f"{phase}_failure" if payload.get("success") is False else phase
                self.state.transition(transition_phase, reason)
                if payload.get("success") is False and payload.get("error"):
                    error = str(payload["error"])
                    self.state.last_error = error
                    if error not in self.state.blockers:
                        self.state.blockers.append(error)
            self.state.set_actions(*actions)
            payload["agent_state"] = self.state.snapshot()
        payload["available_actions"] = [
            {"tool": tool, "reason": action_reason} for tool, action_reason in actions
        ]
        return json.dumps(payload, default=str)

    def _verified(self, candidate_id: str, url: str) -> bool:
        """Require both identity and active URL to match the last verification."""
        return bool(
            self.page
            and self.page.url == url
            and self.state
            and self.state.verification.get("verified") is True
            and self.state.verification.get("candidate_id") == candidate_id
            and self.state.verification.get("url") == url
        )

    def _verification_required(self, candidate_id: str, url: str) -> str:
        return self._response({
            "success": False,
            "candidate_id": candidate_id,
            "candidate": {"candidate_id": candidate_id, "url": url},
            "url": url,
            "error": "Verified candidate and active URL are required before this action.",
        }, (("reservation_verify", "Verify this candidate and URL first"),
            ("reservation_open", "Open the exact candidate URL"),
            ("reservation_close", "End the browser session")),
            phase="reservation_precondition", reason="unverified browser action rejected")

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
            return self._response({"success": False, "error": "Only http(s) booking URLs are allowed."},
                                  (("reservation_open", "Retry with an exact http(s) URL"),),
                                  phase="reservation_scan", reason="invalid booking URL")
        page = self._page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            candidate_id = _candidate_id(page.url)
            if self.state:
                self.state.current_candidate = {"candidate_id": candidate_id, "url": page.url}
            self._log("open_success", final_url=page.url, http_status=response.status if response else None)
            return self._response({
                "success": True,
                "candidate_id": candidate_id,
                "candidate": {"candidate_id": candidate_id, "url": page.url},
                "url": page.url,
                "title": page.title(),
                "http_status": response.status if response else None,
            }, (("reservation_scan_dom", "Scan this rendered page for booking actions"),
                ("reservation_verify", "Verify this candidate and URL before acting"),
                ("reservation_inspect", "Inspect fields, buttons, and frames"),
                ("reservation_close", "End the browser session")),
                phase="reservation_scan", reason="page opened")
        except Exception as exc:
            self._log("open_failed", final_url=page.url, error=str(exc))
            return self._response({"success": False, "url": page.url, "error": str(exc)},
                                  (("reservation_open", "Retry opening the exact URL"),
                                   ("reservation_close", "End the browser session")),
                                  phase="reservation_scan", reason="page open failed")

    def inspect(self) -> str:
        return self._run_on_browser_thread(self._inspect_impl)

    def _inspect_impl(self) -> str:
        if not self.page:
            return self._response({"success": False, "error": "Open a booking URL first."},
                                  (("reservation_open", "Open an exact restaurant URL"),),
                                  phase="reservation_scan", reason="inspect requested without a page")
        fields = self.page.locator("input, select, textarea, button").evaluate_all(
            """els => els.slice(0, 80).map(el => ({
                tag: el.tagName.toLowerCase(), type: el.type || null,
                name: el.name || null, id: el.id || null,
                placeholder: el.placeholder || null,
                label: el.getAttribute('aria-label') || el.innerText || null
            }))"""
        )
        frames = [frame.url for frame in self.page.frames if frame.url and frame.url != self.page.url]
        candidate_id = _candidate_id(self.page.url)
        return self._response({
            "success": True,
            "candidate_id": candidate_id,
            "candidate": {"candidate_id": candidate_id, "url": self.page.url},
            "url": self.page.url,
            "title": self.page.title(),
            "fields": fields,
            "iframes": frames,
            "text": " ".join(self.page.locator("body").inner_text().split())[:4000],
        }, (("reservation_fill", "Fill an identified non-sensitive booking field"),
            ("reservation_click", "Click search or availability, never final booking"),
            ("reservation_scan_dom", "Rescan after a page transition"),
            ("reservation_verify", "Verify this candidate and URL before acting"),
            ("reservation_close", "End the browser session")),
            phase="reservation_inspection", reason="page controls inspected")

    def scan_dom(self, website_url: str = "") -> str:
        """Scan one URL atomically so concurrent candidate work cannot mix pages."""
        return self._run_on_browser_thread(self._scan_dom_for_url_impl, website_url)

    def _scan_dom_for_url_impl(self, website_url: str) -> str:
        if website_url:
            opened = json.loads(self._open_impl(website_url))
            if not opened.get("success"):
                return self._response({
                    "success": False,
                    "url": website_url,
                    "error": opened.get("error", "Unable to open page."),
                }, (("reservation_open", "Retry the exact URL"),
                   ("reservation_close", "End the browser session")),
                    phase="reservation_scan", reason="page open failed during scan")
        try:
            return self._scan_dom_impl()
        except Exception as exc:
            self._log("dom_scan_failed", url=self.page.url if self.page else website_url, error=str(exc))
            return self._response({"success": False, "url": self.page.url if self.page else website_url,
                               "error": f"DOM scan failed: {exc}"},
                                  (("reservation_scan_dom", "Retry the serialized DOM scan"),
                                   ("reservation_inspect", "Inspect the page for an alternate control"),
                                   ("reservation_close", "End the browser session")),
                                  phase="reservation_scan", reason="DOM scan failed")

    def _scan_dom_impl(self) -> str:
        """Extract reservation actions from the rendered page without provider assumptions."""
        if not self.page:
            return self._response({"success": False, "error": "Open a booking URL first."},
                                  (("reservation_open", "Open an exact restaurant URL"),),
                                  phase="reservation_scan", reason="scan requested without a page")
        self._log("dom_scan_start", url=self.page.url)
        # Reservation directories and Wix widgets commonly lazy-load their
        # location cards below the fold. Let the page render those controls
        # before collecting evidence; this is deliberately provider-neutral.
        self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        self.page.wait_for_timeout(1000)
        raw_candidates = self.page.locator("*").evaluate_all(
            """els => els.slice(0, 1200).flatMap(el => {
                const attrs = Object.fromEntries([...el.attributes]
                    .filter(a => /^(href|src|action|value|aria-label|title|data-)/i.test(a.name))
                    .map(a => [a.name, a.value]));
                const rawHref = el.href || el.src || el.action ||
                    attrs['data-reservation-url'] || attrs['data-booking-url'] ||
                    attrs['data-url'] || attrs['data-href'] || null;
                const href = rawHref == null ? null : String(rawHref);
                const tel = href && href.startsWith('tel:') ? href.slice(4) : null;
                const label = (el.innerText || attrs['aria-label'] || attrs.title || attrs.value || '').trim().slice(0, 300);
                const parentText = (el.parentElement?.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 500);
                let container = el;
                let containerText = '';
                for (let i = 0; i < 7 && container; i++, container = container.parentElement) {
                    const text = (container.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (text.length > containerText.length && text.length <= 1200) containerText = text;
                }
                const candidate = {tag: el.tagName.toLowerCase(), href, tel, label,
                    nearby_text: parentText, container_text: containerText, attributes: attrs,
                    iframe: el.tagName.toLowerCase() === 'iframe'};
                const hasProviderData = Object.keys(attrs).some(name => /data-(?:.*(?:restref|restaurant|venue|location|provider).*(?:id|ref)?|reservation|booking)/i.test(name));
                return (href || tel || hasProviderData || /reserv|book|table|availability|waitlist/i.test(`${label} ${parentText}`)) ? [candidate] : [];
            })"""
        )
        candidates = []
        phone_hints = []
        provider_identifiers = []
        seen = set()
        for raw in raw_candidates:
            if str(raw.get("tag") or "").casefold() in _NON_ACTIONABLE_TAGS:
                continue
            score, confidence = _candidate_score(raw, self.page.url)
            identifiers = _provider_identifiers(raw)
            for identifier in identifiers:
                provider_identifiers.append({**identifier, "url": str(raw.get("href") or ""), "label": str(raw.get("label") or "")})
            phone_match = _PHONE_NUMBER.search(
                " ".join(str(raw.get(key) or "") for key in ("label", "nearby_text", "href"))
            )
            phone = raw.get("tel") or (phone_match.group(0) if phone_match else None)
            if phone and _PHONE_RESERVATION_WORDS.search(
                f"{raw.get('label', '')} {raw.get('nearby_text', '')} {raw.get('href', '')}"
            ) and confidence != "low":
                phone_hints.append({
                    "phone": phone, "label": raw.get("label", ""),
                    "nearby_text": raw.get("nearby_text", ""),
                    "score": score, "confidence": confidence,
                })
            href = raw.get("href")
            attributes = raw.get("attributes") or {}
            embedded = attributes.get("data-ot-restref") if isinstance(attributes, dict) else None
            dedupe_key = href or (
                "provider",
                tuple((item["attribute"], item["value"]) for item in identifiers),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            item = {**raw, "score": score, "confidence": confidence, "provider_identifiers": identifiers}
            item["candidate_id"] = _candidate_id(self.page.url, str(raw.get("href") or raw.get("label") or ""))
            item["source_url"] = self.page.url
            item["url"] = str(raw.get("href") or self.page.url)
            if embedded:
                params = dict(parse_qsl(str(embedded), keep_blank_values=True))
                params.pop("rid", None)
                if params.get("restref"):
                    path = str(attributes.get("data-ot-path") or "/booking/restref/availability")
                    item["restref"] = params["restref"]
                    item["opentableUrl"] = f"https://www.opentable.com{path}?{urlencode(params)}"
            item["googleReserve"] = href if "/maps/reserve/v/dine/" in str(href) else None
            item["isBooking"] = score >= 4
            if confidence != "low":
                candidates.append(item)
        candidates.sort(key=lambda item: (-int(item["score"]), str(item.get("url") or item.get("href") or "")))
        phone_hints.sort(key=lambda item: -int(item["score"]))
        if self.state:
            page_candidate_id = _candidate_id(self.page.url)
            self.state.scanned_candidates[page_candidate_id] = {
                "candidate_id": page_candidate_id,
                "url": self.page.url,
                "source_url": self.page.url,
                "label": "scanned page",
                "tag": "page",
            }
            for item in candidates:
                self.state.scanned_candidates[str(item["candidate_id"])] = {
                    "candidate_id": item["candidate_id"],
                    "url": item["url"],
                    "source_url": item["source_url"],
                    "label": item.get("label", ""),
                    "tag": item.get("tag", ""),
                    "score": item.get("score", 0),
                }
        self._log("dom_scan_complete", candidate_count=len(candidates), phone_hint_count=len(phone_hints), url=self.page.url)
        actions: tuple[tuple[str, str], ...] = (
            ("reservation_verify", "Verify the page candidate and URL"),
            ("reservation_inspect", "Inspect the page before choosing a path"),
            ("reservation_close", "End the browser session"),
        ) if candidates else (
            ("reservation_inspect", "Inspect visible text and controls"),
            ("reservation_close", "End the browser session"),
        )
        next_candidate = candidates[0] if candidates else None
        next_action = None
        if next_candidate:
            next_action = {
                "tool": "reservation_open",
                "arguments": {"url": next_candidate["url"]},
                "candidate_id": next_candidate["candidate_id"],
                "url": next_candidate["url"],
                "reason": "highest-confidence actionable reservation candidate",
            }
        return self._response({
            "success": True,
            "candidate_id": _candidate_id(self.page.url),
            "candidate": {"candidate_id": _candidate_id(self.page.url), "url": self.page.url},
            "url": self.page.url,
            "candidates": candidates,
            "phone_hints": phone_hints,
            "provider_identifiers": provider_identifiers,
            "next_action": next_action,
            "navigation_actions": [
                {
                    "tool": "reservation_open",
                    "candidate_id": item["candidate_id"],
                    "url": item["url"],
                    "label": item.get("label", ""),
                }
                for item in candidates
                if item.get("tag") in {"a", "button"} and item.get("url") != self.page.url
            ],
        }, actions, phase="reservation_scan", reason="DOM actions observed")

    def find_booking_links(self) -> str:
        """Compatibility alias for callers that still use the old tool name."""
        return self.scan_dom()

    def verify(self, candidate_id: str, url: str) -> str:
        """Verify that the active page still belongs to the claimed candidate."""
        return self._run_on_browser_thread(self._verify_impl, candidate_id, url)

    def _verify_impl(self, candidate_id: str, url: str) -> str:
        actual_url = self.page.url if self.page else ""
        expected_id = _candidate_id(actual_url) if actual_url else ""
        valid = bool(self.page and actual_url == url and candidate_id == expected_id)
        if self.state:
            self.state.verification = {
                "candidate_id": candidate_id,
                "url": actual_url or url,
                "verified": valid,
            }
        return self._response({
            "success": valid,
            "candidate_id": candidate_id,
            "candidate": {"candidate_id": candidate_id, "url": url},
            "url": actual_url or url,
            "verified": valid,
            **({} if valid else {"error": "Active page does not match candidate_id and URL."}),
        }, (("reservation_inspect", "Inspect the active candidate page"),
            ("reservation_scan_dom", "Scan the verified candidate URL"),
            ("reservation_close", "End the browser session")) if valid else (
            ("reservation_open", "Open the exact candidate URL again"),
            ("agent_get_state", "Read the active candidate and failure state"),
            ("reservation_close", "End the browser session")),
            phase="reservation_verification", reason="candidate URL identity checked")

    def fill(self, candidate_id: str, url: str, field: str, value: str) -> str:
        return self._run_on_browser_thread(self._fill_impl, candidate_id, url, field, value)

    def _fill_impl(self, candidate_id: str, url: str, field: str, value: str) -> str:
        if not self._verified(candidate_id, url):
            return self._verification_required(candidate_id, url)
        if not self.page:
            return self._response({"success": False, "error": "Open a booking URL first."},
                                  (("reservation_open", "Open an exact restaurant URL"),),
                                  phase="reservation_preparation", reason="fill requested without a page")
        try:
            locator = self.page.get_by_label(field, exact=False).first
            if not locator.count():
                locator = self.page.locator(
                    f"input[name='{field}'], input[id='{field}'], select[name='{field}'], textarea[name='{field}']"
                ).first
            if locator.count() == 0:
                return self._response({"success": False, "error": f"Booking field not found: {field}"},
                                      (("reservation_inspect", "Inspect available field labels"),
                                       ("reservation_close", "End the browser session")),
                                      phase="reservation_preparation", reason="requested field not found")
            if locator.evaluate("el => el.tagName.toLowerCase()") == "select":
                locator.select_option(label=value)
            else:
                locator.fill(value)
            return self._response({"success": True, "field": field, "value": value},
                                  (("reservation_fill", "Fill another identified field"),
                                   ("reservation_click", "Search availability without submitting"),
                                   ("reservation_close", "End the browser session")),
                                  phase="reservation_preparation", reason="booking field filled")
        except Exception as exc:
            return self._response({"success": False, "field": field, "error": str(exc)},
                                  (("reservation_inspect", "Inspect the page after the fill failed"),
                                   ("reservation_close", "End the browser session")),
                                  phase="reservation_preparation", reason="booking field fill failed")

    def click(self, candidate_id: str, url: str, label: str) -> str:
        return self._run_on_browser_thread(self._click_impl, candidate_id, url, label)

    def prepare(self, candidate_id: str, url: str, booking_url: str,
                date: str, time: str, party_size: int) -> str:
        """Prepare a handoff only from a verified page and observed URL."""
        return self._run_on_browser_thread(
            self._prepare_impl, candidate_id, url, booking_url, date, time, party_size
        )

    def _prepare_impl(self, candidate_id: str, url: str, booking_url: str,
                      date: str, time: str, party_size: int) -> str:
        if not self._verified(candidate_id, url):
            return self._verification_required(candidate_id, url)
        observed = next((item for item in self.state.scanned_candidates.values()  # type: ignore[union-attr]
                         if item.get("url") == booking_url and item.get("source_url") == url), None) \
            if self.state else None
        if not observed or observed.get("source_url") != url:
            return self._response({
                "success": False,
                "candidate_id": candidate_id,
                "candidate": {"candidate_id": candidate_id, "url": url},
                "url": url,
                "booking_url": booking_url,
                "error": "Booking URL was not observed on the verified page.",
            }, (("reservation_scan_dom", "Scan the verified page for an exact booking URL"),
                ("reservation_close", "End the browser session")),
                phase="reservation_preparation", reason="unobserved booking URL rejected")
        provider = classify_booking_provider(booking_url, url)
        prepared_url = booking_url
        if provider == "OpenTable":
            prepared_url = build_opentable_availability_url(
                booking_url, date=date, time=time, party_size=party_size
            )
        return self._response({
            "success": True,
            "candidate_id": candidate_id,
            "candidate": {"candidate_id": candidate_id, "url": url},
            "url": url,
            "booking_url": booking_url,
            "prepared_booking_url": prepared_url,
            "provider": provider,
            "prepared": {"date": date, "time": time, "party_size": party_size},
            "next_step": "Hand this verified booking URL to the organizer; do not submit.",
        }, (("reservation_close", "End the browser session"),),
            phase="reservation_preparation", reason="verified booking handoff prepared")

    def _click_impl(self, candidate_id: str, url: str, label: str) -> str:
        """Click a non-submitting control such as Search or Find a table."""
        if not self._verified(candidate_id, url):
            return self._verification_required(candidate_id, url)
        if not self.page:
            return self._response({"success": False, "error": "Open a booking URL first."},
                                  (("reservation_open", "Open an exact restaurant URL"),),
                                  phase="reservation_preparation", reason="click requested without a page")
        if re.search(r"book|reserve|confirm|submit|complete", label, re.I):
            return self._response({"success": False, "error": "Final booking controls require organizer confirmation."},
                                  (("reservation_inspect", "Inspect the confirmation state"),
                                   ("reservation_close", "End the browser session")),
                                  phase="reservation_preparation", reason="final booking action gated")
        try:
            locator = self.page.get_by_role("button", name=label, exact=False).first
            if locator.count() == 0:
                locator = self.page.get_by_text(label, exact=False).first
            if locator.count() == 0:
                return self._response({"success": False, "error": f"Booking control not found: {label}"},
                                      (("reservation_inspect", "Inspect available controls"),
                                       ("reservation_close", "End the browser session")),
                                      phase="reservation_preparation", reason="requested control not found")
            locator.click()
            return self._response({"success": True, "clicked": label, "url": self.page.url},
                                  (("reservation_inspect", "Inspect the post-click page"),
                                   ("reservation_scan_dom", "Scan the post-click page"),
                                   ("reservation_close", "End the browser session")),
                                  phase="reservation_preparation", reason="non-final booking control clicked")
        except Exception as exc:
            return self._response({"success": False, "clicked": label, "error": str(exc)},
                                  (("reservation_inspect", "Inspect the page after the click failed"),
                                   ("reservation_close", "End the browser session")),
                                  phase="reservation_preparation", reason="booking control click failed")


def create_reservation_browser_tools(user_id: str, state: AgentState | None = None) -> tuple[ReservationBrowser, list[object]]:
    """Create tools whose closures share one organizer-scoped browser page."""
    browser = ReservationBrowser(user_id, state)

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
        """Compatibility alias for reservation_scan_dom."""
        return browser.find_booking_links()

    @tool
    def reservation_verify(candidate_id: str, url: str) -> str:
        """Verify candidate identity and active page URL before taking the next action."""
        return browser.verify(candidate_id, url)

    @tool
    def reservation_scan_dom(website_url: str = "") -> str:
        """Open and scan one exact restaurant URL as one serialized operation."""
        return browser.scan_dom(website_url)

    @tool
    def reservation_fill(candidate_id: str, url: str, field: str, value: str) -> str:
        """Fill one identified booking field; never submits the form."""
        return browser.fill(candidate_id, url, field, value)

    @tool
    def reservation_click(candidate_id: str, url: str, label: str) -> str:
        """Click a search/availability control, never a final booking control."""
        return browser.click(candidate_id, url, label)

    @tool
    def reservation_prepare(candidate_id: str, url: str, booking_url: str,
                            date: str, time: str, party_size: int) -> str:
        """Prepare an observed booking URL from a verified page, without submitting."""
        return browser.prepare(candidate_id, url, booking_url, date, time, party_size)

    @tool
    def reservation_close() -> str:
        """Close the organizer-scoped browser session after the booking handoff."""
        browser.close()
        return browser._response({"success": True, "message": "Reservation browser session closed."},
                                 (), phase="cleanup", reason="browser session closed")

    return browser, [reservation_open, reservation_inspect, reservation_scan_dom, reservation_find_booking_links,
                      reservation_verify, reservation_fill, reservation_click,
                      reservation_prepare, reservation_close]
