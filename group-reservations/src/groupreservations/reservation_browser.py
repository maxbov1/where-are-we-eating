"""Agent-facing Playwright tools for adaptive restaurant booking pages."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
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
_INTERACTIVE_SELECTOR = (
    "button, [role='button'], [role='option'], [role='menuitem'], a, select, input, "
    "[role='combobox'], [data-value], [data-date], [data-time]"
)


def _is_skip_control(candidate: dict[str, object]) -> bool:
    """Identify accessibility/navigation controls that cannot be booking evidence."""
    text = " ".join(str(candidate.get(key) or "") for key in
                     ("label", "href", "nearby_text", "surface_heading")).casefold()
    return bool(re.search(r"skip\s+to\s+(?:main|content)|accessibility|screen[- ]reader|audioeye", text))


def _reservation_signal(candidate: dict[str, object]) -> bool:
    """Return whether this element itself belongs to a reservation surface."""
    if _is_skip_control(candidate):
        return False
    text = " ".join(str(candidate.get(key) or "") for key in
                     ("label", "nearby_text", "form_text", "surface_heading", "href"))
    return bool(candidate.get("form") or _RESERVATION_WORDS.search(text))


def _candidate_score(candidate: dict[str, object], page_url: str) -> tuple[int, str]:
    """Score reservation evidence without requiring a known provider."""
    label = str(candidate.get("label") or "")
    nearby_text = str(candidate.get("nearby_text") or "")
    form_text = str(candidate.get("form_text") or "")
    href = str(candidate.get("href") or "")
    attribute_map = candidate.get("attributes") or {}
    attributes = " ".join(
        f"{name} {value}" for name, value in attribute_map.items()
    ) if isinstance(attribute_map, dict) else str(attribute_map)
    signal_text = f"{label} {nearby_text} {form_text} {href} {attributes}"
    score = 0
    if _is_skip_control(candidate):
        return -100, "excluded"
    if _RESERVATION_WORDS.search(label):
        score += 5
    if _RESERVATION_WORDS.search(nearby_text):
        score += 4
    if _RESERVATION_WORDS.search(form_text):
        score += 4
    if candidate.get("form"):
        score += 3
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
        # Handoffs may be prepared concurrently. Keep page and verification
        # identity per candidate instead of allowing the last navigation to
        # invalidate every other workflow.
        self.candidate_pages: dict[str, Page] = {}
        self.workflow_pages: dict[str, Page] = {}
        # Detailed sweep records are retained for follow-up actions but are
        # deliberately not serialized into every model-facing response.
        self.surface_records: dict[str, dict[str, object]] = {}
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
            # Full state is useful for failures/debugging, but repeating every
            # workflow and observation on successful tool calls dominates the
            # model context. Successful calls expose their local transition
            # fields instead; the state tool remains the explicit full-state
            # escape hatch.
            if payload.get("success") is False or payload.get("status") == "blocked":
                payload["agent_state"] = self.state.snapshot()
        payload["available_actions"] = [
            {"tool": tool, "reason": action_reason} for tool, action_reason in actions
        ]
        return json.dumps(payload, default=str)

    def _verified(self, candidate_id: str, url: str) -> bool:
        """Require identity and an active top-level page or embedded frame."""
        target = self._verified_target(candidate_id, url)
        return target is not None

    def _surface_id(self, identifier: str) -> str:
        """Resolve the stable workflow handle to its current surface ID."""
        if self.state:
            workflow = self.state.workflows.get(identifier)
            if workflow and workflow.get("surface_id"):
                return str(workflow["surface_id"])
        return identifier

    def _verified_target(self, candidate_id: str, url: str):
        """Return the verified page/frame target for a browser action."""
        surface_id = self._surface_id(candidate_id)
        observed = self.state.scanned_candidates.get(surface_id) if self.state else None
        candidate_page = self.candidate_pages.get(surface_id) or self.page
        verification = (
            self.state.verifications.get(surface_id)
            if self.state and hasattr(self.state, "verifications") else None
        ) or (self.state.verification if self.state else {})
        valid = bool(
            candidate_page and observed
            and observed.get("url") == url
            and observed.get("source_url") == candidate_page.url
            and self.state
            and verification.get("verified") is True
            and verification.get("candidate_id") == surface_id
            and verification.get("url") == url
        )
        if not valid or not candidate_page:
            return None
        if url == candidate_page.url:
            return candidate_page
        return next((frame for frame in candidate_page.frames if frame.url == url), None)

    def _workflow_id(self, candidate_id: str) -> str | None:
        if not self.state:
            return None
        if candidate_id in self.state.workflows:
            return candidate_id
        candidate = self.state.scanned_candidates.get(candidate_id) or {}
        workflow_id = candidate.get("workflow_id")
        return str(workflow_id) if workflow_id else None

    def _handoff_workflow_id(self, candidate_id: str) -> str | None:
        """Return the original handoff ledger key for a provider-page workflow."""
        workflow_id = self._workflow_id(candidate_id)
        if not self.state or not workflow_id:
            return workflow_id
        workflow = self.state.workflows.get(workflow_id) or {}
        return str(workflow.get("parent_workflow_id") or workflow_id)

    def _transition_workflow(self, candidate_id: str, state: str, **changes: object) -> None:
        if not self.state:
            return
        workflow_id = self._workflow_id(candidate_id)
        if not workflow_id:
            return
        workflow = self.state.workflows.setdefault(workflow_id, {"workflow_id": workflow_id})
        previous_state = workflow.get("state")
        workflow.update({"state": state, **changes})
        self._log("workflow_transition", workflow_id=workflow_id,
                  from_state=previous_state, to_state=state,
                  surface_id=workflow.get("surface_id"), changes=changes)

    def _structured_error(self, candidate_id: str, url: str, error_code: str,
                          message: str, recovery_tool: str, recovery_reason: str) -> dict[str, object]:
        workflow_id = self._workflow_id(candidate_id)
        error = {
            "success": False,
            "status": "blocked",
            "error_code": error_code,
            "workflow_id": workflow_id,
            "surface_id": self._surface_id(candidate_id),
            "candidate_id": candidate_id,
            "candidate": {"candidate_id": candidate_id, "url": url},
            "url": url,
            "error": message,
            "recovery": {"tool": recovery_tool, "reason": recovery_reason},
        }
        self._log("workflow_blocked", workflow_id=workflow_id,
                  surface_id=self._surface_id(candidate_id), error_code=error_code,
                  recovery_tool=recovery_tool)
        return error

    def _verification_required(self, candidate_id: str, url: str) -> str:
        return self._response(self._structured_error(
            candidate_id, url, "CANDIDATE_NOT_VERIFIED",
            "Verified candidate and active URL are required before this action.",
            "reservation_sweep", "Resweep the workflow and use the returned workflow_id/surface_id.",
        ), (("reservation_sweep", "Resweep the workflow and use the returned workflow_id"),
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
        self.candidate_pages.clear()
        self.workflow_pages.clear()
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
        had_page = self.page is not None
        page = self._page()
        if self.browser and had_page:
            # A new page is cheap and preserves independent candidate state.
            # The first page is created by _page(); subsequent opens get their
            # own page rather than overwriting an in-flight handoff.
            page = self.browser.contexts[0].new_page()
            self.page = page
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            candidate_id = _candidate_id(page.url)
            self.candidate_pages[candidate_id] = page
            if self.state:
                self.state.current_candidate = {"candidate_id": candidate_id, "url": page.url}
            self._log("open_success", final_url=page.url, page_id=id(page),
                      http_status=response.status if response else None)
            return self._response({
                "success": True,
                "candidate_id": candidate_id,
                "candidate": {"candidate_id": candidate_id, "url": page.url},
                "url": page.url,
                "title": page.title(),
                "http_status": response.status if response else None,
            }, (("reservation_scan_dom", "Scan this rendered page for booking actions"),
                ("reservation_verify", "Verify this candidate and URL before acting"),
                ("reservation_observe", "Observe DOM, accessibility, and visual evidence"),
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
        frame_fields = []
        frames = []
        for frame in self.page.frames:
            if not frame.url or frame.url == self.page.url:
                continue
            frames.append(frame.url)
            try:
                frame_fields.extend(frame.locator("input, select, textarea, button").evaluate_all(
                    """els => els.slice(0, 80).map(el => ({
                        tag: el.tagName.toLowerCase(), type: el.type || null,
                        name: el.name || null, id: el.id || null,
                        placeholder: el.placeholder || null,
                        label: el.getAttribute('aria-label') || el.innerText || null,
                        frame_url: location.href
                    }))"""
                ))
            except Exception:
                continue
        candidate_id = _candidate_id(self.page.url)
        return self._response({
            "success": True,
            "candidate_id": candidate_id,
            "candidate": {"candidate_id": candidate_id, "url": self.page.url},
            "url": self.page.url,
            "title": self.page.title(),
            "fields": fields + frame_fields,
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

    def sweep(self, website_url: str = "") -> str:
        """Return an unclassified map of the rendered page and its frames."""
        return self._run_on_browser_thread(self._sweep_for_url_impl, website_url)

    def _sweep_for_url_impl(self, website_url: str) -> str:
        if website_url:
            opened = json.loads(self._open_impl(website_url))
            if not opened.get("success"):
                return self._response({"success": False, "url": website_url,
                                       "error": opened.get("error", "Unable to open page.")},
                                      (("reservation_open", "Retry the exact restaurant URL"),),
                                      phase="reservation_scan", reason="page open failed during sweep")
        if not self.page:
            return self._response({"success": False, "error": "Open a restaurant URL first."},
                                  (("reservation_open", "Open an exact restaurant URL"),),
                                  phase="reservation_scan", reason="sweep requested without a page")

        page = self.page
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)
        # Give asynchronously mounted widgets a bounded opportunity to appear.
        for _ in range(10):
            page.wait_for_timeout(500)

        regions: list[dict[str, object]] = []
        targets = [page] + [frame for frame in page.frames if frame != page.main_frame]
        selector = (
            "a, button, input, select, textarea, form, iframe, "
            "[role], [contenteditable='true'], h1, h2, h3, h4, nav, main, section"
        )
        for target in targets:
            try:
                rows = target.locator(selector).evaluate_all(
                    """els => els.slice(0, 1800).map((el, index) => {
                        const rect = el.getBoundingClientRect();
                        const attrs = Object.fromEntries([...el.attributes]
                          .filter(a => /^(href|src|action|name|id|type|role|aria-|data-)/i.test(a.name))
                          .map(a => [a.name, a.value]));
                        return {
                          index, tag: el.tagName.toLowerCase(), role: el.getAttribute('role'),
                          label: (el.getAttribute('aria-label') || el.innerText || el.getAttribute('title') ||
                            el.getAttribute('placeholder') || el.getAttribute('value') || '').replace(/\\s+/g, ' ').trim().slice(0, 500),
                          href: el.href || el.src || el.action || null,
                          attributes: attrs,
                          nearby_text: (el.parentElement?.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 700),
                          form_text: (() => {
                            const form = el.closest('form, dialog, [role="dialog"]');
                            return (form?.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 1200);
                          })(),
                          surface_key: (() => {
                            const root = el.closest('form, dialog, [role="dialog"], main, section, nav, header, footer');
                            if (!root) return `frame:${location.href}`;
                            return `${root.tagName.toLowerCase()}|${root.id || root.getAttribute('aria-label') ||
                              (root.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 180)}`;
                          })(),
                          surface_heading: (() => {
                            const root = el.closest('form, dialog, [role="dialog"], main, section, nav, header, footer') || el;
                            const heading = root.querySelector('h1,h2,h3,h4,[role="heading"]');
                            return (heading?.innerText || root.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().slice(0, 180);
                          })(),
                          visible: !!(rect.width && rect.height && getComputedStyle(el).visibility !== 'hidden'),
                          x: Math.round(rect.x), y: Math.round(rect.y),
                          width: Math.round(rect.width), height: Math.round(rect.height),
                          frame_url: location.href
                        };
                    })"""
                )
            except Exception as exc:
                self._log("page_sweep_target_skipped", frame_url=getattr(target, "url", ""), error=str(exc))
                continue
            for row in rows:
                region_id = _candidate_id(
                    page.url, f"{row.get('frame_url')}|{row.get('index')}|{row.get('tag')}|{row.get('label')}"
                )
                row.update({
                    "candidate_id": region_id,
                    "source_url": page.url,
                    "url": str(row.get("frame_url") or page.url),
                })
                regions.append(row)
                self.candidate_pages[region_id] = page
                if self.state:
                    self.state.scanned_candidates[region_id] = {
                        "candidate_id": region_id, "url": row["url"],
                        "source_url": page.url, "label": row.get("label", ""),
                        "tag": row.get("tag", ""), "role": row.get("role"),
                        "action_url": row.get("href"),
                    }
                    self.state.verifications[region_id] = {
                        "candidate_id": region_id, "url": row["url"],
                        "source_url": page.url, "verified": True,
                    }

        grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
        for region in regions:
            key = (str(region.get("frame_url") or page.url), str(region.get("surface_key") or "root"))
            grouped.setdefault(key, []).append(region)

        surfaces: list[dict[str, object]] = []
        for (frame_url, surface_key), members in grouped.items():
            surface_id = _candidate_id(page.url, f"surface|{frame_url}|{surface_key}")
            workflow_id = f"workflow-{hashlib.sha256(f'{page.url}|{surface_id}'.encode()).hexdigest()[:16]}"
            labels = [str(item.get("label") or "") for item in members if item.get("label")]
            controls = [item for item in members if str(item.get("tag") or "") in
                        {"a", "button", "input", "select", "textarea", "iframe"}
                        or item.get("role")]
            text = " ".join(labels + [str(item.get("nearby_text") or "") for item in members])
            lower = text.casefold()
            action_url_evidence = [{
                "url": str(item.get("href")),
                "region_id": item.get("candidate_id"),
                "tag": item.get("tag"),
                "label": item.get("label"),
                "reservation_signal": True,
            } for item in members if item.get("href") and _reservation_signal(item)]
            action_urls = sorted({item["url"] for item in action_url_evidence})[:12]
            xs = [int(item.get("x") or 0) for item in members]
            ys = [int(item.get("y") or 0) for item in members]
            rights = [int(item.get("x") or 0) + int(item.get("width") or 0) for item in members]
            bottoms = [int(item.get("y") or 0) + int(item.get("height") or 0) for item in members]
            kind = "form" if any(item.get("tag") == "form" for item in members) else (
                "frame" if frame_url != page.url else "section"
            )
            surface = {
                "surface_id": surface_id,
                "workflow_id": workflow_id,
                "kind": kind,
                "frame_url": frame_url,
                "heading": next((str(item.get("surface_heading")) for item in members
                                  if item.get("surface_heading")), ""),
                "bounds": {"x": min(xs or [0]), "y": min(ys or [0]),
                           "width": max(rights or [0]) - min(xs or [0]),
                           "height": max(bottoms or [0]) - min(ys or [0])},
                "controls": [{"region_id": item.get("candidate_id"), "tag": item.get("tag"),
                              "role": item.get("role"), "label": item.get("label"),
                              "visible": item.get("visible"), "href": item.get("href")}
                             for item in controls[:12]],
                "action_urls": action_urls,
                "action_url_evidence": action_url_evidence[:20],
                "region_ids": [item.get("candidate_id") for item in members[:80]],
                "signals": {
                    "has_date_control": bool(re.search(r"date|day|calendar", lower)),
                    "has_time_control": bool(re.search(r"time|hour|pm|am", lower)),
                    "has_party_size_control": bool(re.search(r"party|guest|people|person|covers", lower)),
                    "has_form": any(item.get("tag") == "form" for item in members),
                    "has_external_action_url": any(urlsplit(url).hostname and
                                                    urlsplit(url).hostname != urlsplit(page.url).hostname
                                                    for url in action_urls),
                    "contains_reservation_language": bool(re.search(r"reserv|book|table|availab|waitlist", lower)),
                    "reservation_control_count": sum(1 for item in members if _reservation_signal(item)),
                },
            }
            surfaces.append(surface)
            self.candidate_pages[surface_id] = page
            self.workflow_pages[workflow_id] = page
            self.surface_records[surface_id] = {
                "surface_id": surface_id,
                "workflow_id": workflow_id,
                "frame_url": frame_url,
                "surface_key": surface_key,
                "records": members,
            }
            if self.state:
                parent_workflow_id = next(
                    (handoff_id for handoff_id, handoff in self.state.reservation_handoffs.items()
                     if str(handoff.get("prepared_booking_url") or "") == frame_url),
                    None,
                )
                self.state.workflows[workflow_id] = {
                    "workflow_id": workflow_id,
                    "state": "swept",
                    "surface_id": surface_id,
                    "observation_url": frame_url,
                    "source_url": page.url,
                    "action_urls": list(surface["action_urls"]),
                    "action_url_evidence": list(surface["action_url_evidence"]),
                    "parent_workflow_id": parent_workflow_id,
                }
                self.state.scanned_candidates[surface_id] = {
                    "candidate_id": surface_id, "url": frame_url,
                    "source_url": page.url, "label": surface["heading"], "tag": kind,
                    "surface_id": surface_id,
                    "workflow_id": workflow_id,
                    "action_urls": list(surface["action_urls"]),
                    "action_url_evidence": list(surface["action_url_evidence"]),
                    "signals": dict(surface["signals"]),
                }
                self.state.verifications[surface_id] = {
                    "candidate_id": surface_id, "url": frame_url,
                    "source_url": page.url, "verified": True,
                }
        surfaces = [item for item in surfaces if item["signals"].get("reservation_control_count", 0) or
                    item["signals"].get("has_form")]
        surfaces.sort(key=lambda item: (
            -int(bool(item["signals"].get("contains_reservation_language"))),
            -len(item.get("controls", [])), str(item.get("heading") or ""),
        ))
        frame_map = [{"url": frame.url, "name": frame.name} for frame in page.frames]
        screenshot_path = None
        try:
            evidence_dir = self.profile_dir / "observations"
            evidence_dir.mkdir(parents=True, exist_ok=True)
            screenshot_file = evidence_dir / f"sweep-{time.time_ns()}.jpg"
            page.screenshot(path=str(screenshot_file), type="jpeg", quality=55, full_page=False)
            screenshot_path = str(screenshot_file)
        except Exception as exc:
            self._log("page_sweep_screenshot_failed", error=str(exc))
        self._log("page_sweep_complete", url=page.url, page_id=id(page),
                  region_count=len(regions), surface_count=len(surfaces), frame_count=len(frame_map),
                  workflow_ids=[str(item.get("workflow_id")) for item in surfaces])
        # Keep detailed region records server-side. The model only needs a
        # compact index to select the next surface; reservation_expand can
        # retrieve the detailed controls for that surface later.
        model_surfaces = []
        for surface in surfaces:
            controls = surface.get("controls") or []
            signals = surface.get("signals") or {}
            model_surfaces.append({
                "surface_id": surface["surface_id"],
                "workflow_id": surface["workflow_id"],
                "kind": surface["kind"],
                "frame_url": surface["frame_url"],
                "heading": surface.get("heading", ""),
                "signals": signals,
                "control_summary": {
                    "total": len(controls),
                    "buttons": sum(1 for item in controls if item.get("tag") == "button"),
                    "forms": int(bool(signals.get("has_form"))),
                    "inputs": sum(1 for item in controls if item.get("tag") in {"input", "select", "textarea"}),
                    "iframes": sum(1 for item in controls if item.get("tag") == "iframe"),
                },
                "reservation_control_labels": [
                    str(item.get("label") or "")[:160]
                    for item in controls
                    if _reservation_signal(item)
                ][:12],
                "action_urls": list(surface.get("action_urls") or [])[:8],
            })
        link_inventory = []
        seen_links: set[str] = set()
        region_surface_ids = {
            str(record.get("candidate_id")): surface["surface_id"]
            for surface in surfaces
            for record in self.surface_records.get(surface["surface_id"], {}).get("records", [])
            if isinstance(record, dict) and record.get("candidate_id")
        }
        for region in regions:
            link = str(region.get("href") or "")
            if not link or link in seen_links or _is_skip_control(region):
                continue
            seen_links.add(link)
            link_inventory.append({
                "url": link,
                "kind": "iframe" if region.get("tag") == "iframe" else
                        "form" if region.get("tag") == "form" else "link",
                "label": str(region.get("label") or "")[:160],
                "reservation_signal": _reservation_signal(region),
                "surface_id": region_surface_ids.get(str(region.get("candidate_id"))),
            })
            if len(link_inventory) >= 40:
                break
        return self._response({
            "success": True, "url": page.url, "candidate_id": _candidate_id(page.url),
            "candidate": {"candidate_id": _candidate_id(page.url), "url": page.url},
            "surfaces": model_surfaces, "links": link_inventory, "frames": frame_map,
            "region_count": len(regions),
            "screenshot_path": screenshot_path,
        }, (("reservation_expand", "Expand an agent-selected page or frame region"),
            ("reservation_sweep", "Repeat the complete page sweep after loading changes"),
            ("reservation_close", "End the browser session")),
            phase="reservation_scan", reason="complete rendered page sweep")

    def expand(self, surface_id: str, url: str, include_screenshot: bool = False) -> str:
        """Expand an agent-selected sweep region into detailed evidence."""
        return self._run_on_browser_thread(self._observe_impl, surface_id, url, include_screenshot)

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
        owner_page = self.page
        # Reservation directories and Wix widgets commonly lazy-load their
        # location cards below the fold. Let the page render those controls
        # before collecting evidence; this is deliberately provider-neutral.
        self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        self.page.wait_for_timeout(1000)
        # Scan actionable and reservation-marked elements directly. A bounded
        # `*` scan misses links that appear after large navigation/footer DOMs,
        # including obvious "Reserve Table Online" anchors.
        raw_candidates = []
        # Widgets often arrive as an empty wrapper iframe first, then create a
        # nested provider frame. Wait for the dynamic surface to stabilize,
        # rather than assuming DOMContentLoaded means the widget is present.
        previous_surface = None
        stable_polls = 0
        for _ in range(12):
            self.page.wait_for_timeout(500)
            surface = []
            for frame in self.page.frames:
                if frame == self.page.main_frame or not frame.url:
                    continue
                try:
                    control_count = frame.locator(
                        "input, select, button, a, [role='button'], [role='option'], [role='menuitem']"
                    ).count()
                except Exception:
                    control_count = -1
                surface.append((frame.url, control_count))
            surface_signature = tuple(surface)
            if surface_signature == previous_surface:
                stable_polls += 1
            else:
                stable_polls = 0
                previous_surface = surface_signature
            # Require at least two identical observations. This catches late
            # nested frames while still returning quickly for ordinary pages.
            if _ >= 10 and stable_polls >= 2:
                break
        scan_targets = [self.page] + [frame for frame in self.page.frames if frame != self.page.main_frame]
        selector = (
            "a, button, input, select, form, iframe, [role='button'], [role='option'], "
            "[role='menuitem'], [role='combobox'], "
            "[data-reservation-url], [data-booking-url], [data-ot-restref], "
            "[data-venue-id], [data-location-id], [data-restaurant-id], [data-provider-id], "
            "[href*='reserve' i], [href*='reserv' i], [href*='book' i], "
            "[src*='reserve' i], [src*='reserv' i], [src*='book' i]"
        )
        extraction_script = (
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
                const form = el.closest('form');
                const formText = (form?.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 1600);
                const formSignals = (formText.match(/reserv|book|table|availab|date|time|guest|people/gi) || []).length;
                const candidate = {tag: el.tagName.toLowerCase(), href, tel, label,
                    nearby_text: parentText, container_text: containerText, form_text: formText,
                    form: !!form, frame_url: location.href, attributes: attrs,
                    iframe: el.tagName.toLowerCase() === 'iframe'};
                const hasProviderData = Object.keys(attrs).some(name => /data-(?:.*(?:restref|restaurant|venue|location|provider).*(?:id|ref)?|reservation|booking)/i.test(name));
                const reservationForm = !!form && formSignals >= 2;
                return (href || tel || hasProviderData || reservationForm || /reserv|book|table|availability|waitlist/i.test(`${label} ${parentText}`)) ? [candidate] : [];
            })"""
        )
        for target in scan_targets:
            try:
                raw_candidates.extend(target.locator(selector).evaluate_all(extraction_script))
            except Exception as exc:
                self._log("dom_scan_frame_skipped", frame_url=getattr(target, "url", ""), error=str(exc))
        candidates = []
        phone_hints = []
        provider_identifiers = []
        seen = set()
        for raw in raw_candidates:
            if str(raw.get("tag") or "").casefold() in _NON_ACTIONABLE_TAGS:
                continue
            if _is_skip_control(raw):
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
                str(raw.get("frame_url") or self.page.url),
                str(raw.get("label") or ""),
                "provider",
                tuple((item["attribute"], item["value"]) for item in identifiers),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            item = {**raw, "score": score, "confidence": confidence, "provider_identifiers": identifiers}
            item["candidate_id"] = _candidate_id(self.page.url, str(raw.get("href") or raw.get("label") or ""))
            item["source_url"] = self.page.url
            item["url"] = str(raw.get("href") or raw.get("frame_url") or self.page.url)
            if embedded:
                params = dict(parse_qsl(str(embedded), keep_blank_values=True))
                params.pop("rid", None)
                if params.get("restref"):
                    path = str(attributes.get("data-ot-path") or "/booking/restref/availability")
                    item["restref"] = params["restref"]
                    item["opentableUrl"] = f"https://www.opentable.com{path}?{urlencode(params)}"
            item["googleReserve"] = href if "/maps/reserve/v/dine/" in str(href) else None
            item["isBooking"] = score >= 4 and _reservation_signal(item)
            self.candidate_pages[str(item["candidate_id"])] = owner_page
            if confidence != "low" and item["isBooking"]:
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
            self.candidate_pages[page_candidate_id] = owner_page
            for item in candidates:
                self.state.scanned_candidates[str(item["candidate_id"])] = {
                    "candidate_id": item["candidate_id"],
                    "url": item["url"],
                    "source_url": item["source_url"],
                    "action_urls": [item["url"]],
                    "action_url_evidence": [{
                        "url": item["url"],
                        "tag": item.get("tag", ""),
                        "label": item.get("label", ""),
                    }],
                    "label": item.get("label", ""),
                    "tag": item.get("tag", ""),
                    "score": item.get("score", 0),
                }
        self._log("dom_scan_complete", candidate_count=len(candidates), phone_hint_count=len(phone_hints), url=self.page.url)
        actions: tuple[tuple[str, str], ...] = (
            ("reservation_verify", "Verify the page candidate and URL"),
            ("reservation_observe", "Observe DOM, accessibility, and visual evidence"),
            ("reservation_inspect", "Inspect the page before choosing a path"),
            ("reservation_close", "End the browser session"),
        ) if candidates else (
            ("reservation_inspect", "Inspect visible text and controls"),
            ("reservation_close", "End the browser session"),
        )
        next_candidate = candidates[0] if candidates else None
        next_action = None
        if next_candidate:
            is_embedded = next_candidate.get("tag") == "iframe"
            next_action = {
                "tool": "reservation_verify" if is_embedded else "reservation_open",
                "arguments": {
                    ("candidate_id" if is_embedded else "url"): next_candidate["candidate_id"]
                    if is_embedded else next_candidate["url"],
                    **({"url": next_candidate["url"]} if is_embedded else {}),
                },
                "candidate_id": next_candidate["candidate_id"],
                "url": next_candidate["url"],
                "reason": "highest-confidence embedded or navigable reservation candidate",
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
                    "tool": "reservation_verify" if item.get("tag") == "iframe" else "reservation_open",
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

    def observe(self, candidate_id: str, url: str, include_screenshot: bool = True) -> str:
        """Collect DOM, accessibility, and optional visual evidence for a verified candidate."""
        return self._run_on_browser_thread(
            self._observe_impl, candidate_id, url, include_screenshot
        )

    def _observe_impl(self, candidate_id: str, url: str, include_screenshot: bool) -> str:
        target = self._verified_target(candidate_id, url)
        if target is None:
            return self._verification_required(candidate_id, url)
        self._transition_workflow(candidate_id, "expanded", last_observation_url=url)
        if not self.page:
            return self._response({"success": False, "error": "Open a booking URL first."},
                                  (("reservation_open", "Open an exact restaurant URL"),),
                                  phase="reservation_inspection", reason="observation requested without a page")

        self._log("observation_start", candidate_id=candidate_id, url=url)
        surface_record = getattr(self, "surface_records", {}).get(self._surface_id(candidate_id), {})
        surface_key = str(surface_record.get("surface_key") or "")
        controls = target.locator(
            "input, select, textarea, button, iframe, form, "
            "[role='button'], [role='option'], [role='combobox'], "
            "a[href*='reserv' i], a[href*='book' i], a[href*='table' i], "
            "a[href*='opentable' i], a[href*='resy' i], a[href*='tock' i]"
        ).evaluate_all(
            """(els, expectedSurfaceKey) => els.filter(el => {
                if (!expectedSurfaceKey || expectedSurfaceKey.startsWith('frame:')) return true;
                const root = el.closest('form, dialog, [role="dialog"], main, section, nav, header, footer');
                if (!root) return false;
                const key = `${root.tagName.toLowerCase()}|${root.id || root.getAttribute('aria-label') ||
                  (root.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 180)}`;
                return key === expectedSurfaceKey;
            }).slice(0, 60).map(el => ({
                tag: el.tagName.toLowerCase(), type: el.type || null,
                name: el.name || null, id: el.id || null,
                href: el.href || null, placeholder: el.placeholder || null,
                role: el.getAttribute('role') || null,
                label: el.getAttribute('aria-label') || el.innerText || el.title || null,
                disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'
            }))""", surface_key
        )
        # Retain only controls relevant to the selected reservation surface.
        controls = [item for item in controls if not _is_skip_control(item)]
        dom = {
            "url": url,
            "text": " ".join(target.locator("body").inner_text().split())[:1000],
            "controls": controls,
        }
        ax_snapshot = None
        warnings = []
        try:
            ax_snapshot = target.locator("body").aria_snapshot(timeout=5_000)
        except Exception as exc:
            warnings.append(f"Accessibility snapshot unavailable: {exc}")

        screenshot_path = None
        if include_screenshot:
            try:
                evidence_dir = self.profile_dir / "observations"
                evidence_dir.mkdir(parents=True, exist_ok=True)
                screenshot_file = evidence_dir / (
                    f"{candidate_id.removeprefix('candidate-')}-{time.time_ns()}.jpg"
                )
                (target.page if hasattr(target, "page") else target).screenshot(
                    path=str(screenshot_file), type="jpeg", quality=55, full_page=False
                )
                screenshot_path = str(screenshot_file)
            except Exception as exc:
                warnings.append(f"Screenshot unavailable: {exc}")

        self._log(
            "observation_complete",
            candidate_id=candidate_id,
            url=url,
            controls=len(controls),
            has_ax_snapshot=ax_snapshot is not None,
            has_screenshot=screenshot_path is not None,
        )
        return self._response({
            "success": True,
            "candidate_id": candidate_id,
            "workflow_id": self._workflow_id(candidate_id),
            "candidate": {"candidate_id": candidate_id, "url": url},
            "url": url,
            "dom": dom,
            "accessibility_snapshot": str(ax_snapshot or "")[:1200],
            "screenshot_path": screenshot_path,
            "warnings": warnings,
            }, (("reservation_fill", "Fill an identified non-sensitive booking field"),
            ("reservation_click", "Click search or availability, never submitting"),
            ("reservation_scan_dom", "Rescan after a page transition"),
            ("reservation_close", "End the browser session")),
            phase="reservation_inspection", reason="DOM, accessibility, and visual evidence observed")

    def _verify_impl(self, candidate_id: str, url: str) -> str:
        candidate_page = self.candidate_pages.get(candidate_id) or self.page
        actual_url = candidate_page.url if candidate_page else ""
        observed = self.state.scanned_candidates.get(candidate_id) if self.state else None
        valid = bool(
            candidate_page and observed and observed.get("url") == url
            and observed.get("source_url") == actual_url
            and (url == actual_url or any(frame.url == url for frame in candidate_page.frames))
        )
        if self.state:
            self.state.verification = {
                "candidate_id": candidate_id,
                "url": url,
                "source_url": actual_url,
                "verified": valid,
            }
            self.state.verifications[candidate_id] = dict(self.state.verification)
        return self._response({
            "success": valid,
            "candidate_id": candidate_id,
            "candidate": {"candidate_id": candidate_id, "url": url},
            "url": actual_url or url,
            "verified": valid,
            **({} if valid else {"error": "Active page does not match candidate_id and URL."}),
        }, (("reservation_observe", "Observe DOM, accessibility, and visual evidence"),
            ("reservation_inspect", "Inspect the active candidate page"),
            ("reservation_scan_dom", "Scan the verified candidate URL"),
            ("reservation_close", "End the browser session")) if valid else (
            ("reservation_open", "Open the exact candidate URL again"),
            ("agent_get_state", "Read the active candidate and failure state"),
            ("reservation_close", "End the browser session")),
            phase="reservation_verification", reason="candidate URL identity checked")

    def fill(self, candidate_id: str, url: str, field: str, value: str) -> str:
        return self._run_on_browser_thread(self._fill_impl, candidate_id, url, field, value)

    def _fill_impl(self, candidate_id: str, url: str, field: str, value: str) -> str:
        target = self._verified_target(candidate_id, url)
        if target is None:
            return self._verification_required(candidate_id, url)
        if not self.page:
            return self._response({"success": False, "error": "Open a booking URL first."},
                                  (("reservation_open", "Open an exact restaurant URL"),),
                                  phase="reservation_preparation", reason="fill requested without a page")
        try:
            locator = target.get_by_label(field, exact=False).first
            if not locator.count():
                locator = target.locator(
                    f"input[name='{field}'], input[id='{field}'], select[name='{field}'], textarea[name='{field}'], "
                    f"input[type='{field.casefold()}']"
                ).first
            if locator.count() == 0:
                return self._response({"success": False, "error": f"Fillable booking field not found: {field}. Inspect controls and use reservation_click for custom buttons."},
                                      (("reservation_inspect", "Inspect available field labels"),
                                       ("reservation_click", "Use the matching date/time/party-size control if it is a custom button"),
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

    def act(self, candidate_id: str, url: str, action: str,
            target: str, value: str = "") -> str:
        """Apply one semantic action selected from the latest observation."""
        return self._run_on_browser_thread(
            self._act_impl, candidate_id, url, action, target, value
        )

    def _act_impl(self, candidate_id: str, url: str, action: str,
                  target: str, value: str) -> str:
        action_name = action.casefold().strip()
        if action_name in {"fill", "set", "select"}:
            return self._fill_impl(candidate_id, url, target, value)
        if action_name == "click":
            result = json.loads(self._click_impl(candidate_id, url, target))
            if result.get("success") and re.search(
                    r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", target, re.I):
                if self.state:
                    for handoff in self.state.reservation_handoffs.values():
                        if (handoff.get("source_url") == url or
                                handoff.get("prepared_booking_url") == url):
                            handoff["interactive_complete"] = True
                            handoff["availability_verified"] = True
                            handoff["availability_action"] = f"selected observed time: {target}"
                            handoff["last_action"] = "select_available_time"
                            workflow_id = handoff.get("workflow_id")
                            if workflow_id and self.state:
                                self.state.workflows.setdefault(workflow_id, {}).update({
                                    "state": "handoff_ready",
                                    "availability_verified": True,
                                    "selected_time": target,
                                })
                    active_workflow_id = self._workflow_id(candidate_id)
                    handoff_workflow_id = self._handoff_workflow_id(candidate_id)
                    if (active_workflow_id and handoff_workflow_id
                            and active_workflow_id != handoff_workflow_id):
                        self.state.workflows.setdefault(handoff_workflow_id, {}).update({
                            "state": "handoff_ready",
                            "availability_verified": True,
                            "selected_time": target,
                        })
                        parent_handoff = self.state.reservation_handoffs.get(handoff_workflow_id)
                        if parent_handoff:
                            parent_handoff["interactive_complete"] = True
                            parent_handoff["availability_verified"] = True
                            parent_handoff["availability_action"] = f"selected observed time: {target}"
                            parent_handoff["last_action"] = "select_available_time"
            return json.dumps(result, default=str)
        error = self._structured_error(
            candidate_id, url, "UNSUPPORTED_ACTION",
            "Unsupported reservation action. Use click or set/fill/select.",
            "reservation_expand", "Expand the workflow and choose an observed semantic action.",
        )
        return self._response(error, (("reservation_expand", "Observe semantic controls and current state"),
            ("reservation_close", "End the browser session")),
            phase="reservation_preparation", reason="unsupported semantic browser action")

    def prepare(self, candidate_id: str, url: str, booking_url: str,
                date: str, time: str, party_size: int) -> str:
        """Prepare a handoff only from a verified page and observed URL."""
        return self._run_on_browser_thread(
            self._prepare_impl, candidate_id, url, booking_url, date, time, party_size
        )

    def abandon(self, workflow_id: str, reason: str) -> str:
        """Explicitly close one attempted workflow without claiming availability."""
        return self._run_on_browser_thread(self._abandon_impl, workflow_id, reason)

    def _abandon_impl(self, workflow_id: str, reason: str) -> str:
        if not self.state or workflow_id not in self.state.workflows:
            return self._response({
                "success": False,
                "status": "blocked",
                "error_code": "WORKFLOW_NOT_FOUND",
                "workflow_id": workflow_id,
                "error": "The workflow_id does not identify an existing reservation workflow.",
                "recovery": {"tool": "reservation_sweep", "reason": "Sweep the exact restaurant URL first."},
            }, (("reservation_sweep", "Create or recover a workflow"),),
                phase="reservation_precondition", reason="unknown workflow abandonment rejected")
        handoff_id = self._handoff_workflow_id(workflow_id)
        handoff = self.state.reservation_handoffs.get(handoff_id or "")
        if not handoff:
            return self._response({
                "success": False,
                "status": "blocked",
                "error_code": "HANDOFF_NOT_PREPARED",
                "workflow_id": workflow_id,
                "error": "A workflow must be prepared before it can be abandoned.",
                "recovery": {"tool": "reservation_prepare", "reason": "Prepare the observed reservation surface first."},
            }, (("reservation_prepare", "Prepare this observed workflow"),),
                phase="reservation_precondition", reason="unprepared workflow abandonment rejected")
        handoff["status"] = "abandoned"
        handoff["abandoned"] = True
        handoff["abandon_reason"] = reason[:500]
        self.state.workflows[workflow_id].update({
            "state": "abandoned",
            "abandoned": True,
            "abandon_reason": reason[:500],
        })
        if handoff_id and handoff_id != workflow_id:
            self.state.workflows.setdefault(handoff_id, {}).update({
                "state": "abandoned", "abandoned": True, "abandon_reason": reason[:500],
            })
        return self._response({
            "success": True,
            "status": "abandoned",
            "workflow_id": workflow_id,
            "handoff_workflow_id": handoff_id,
            "reason": reason,
        }, (("reservation_sweep", "Start a replacement workflow if needed"),
            ("reservation_close", "End the browser session")),
            phase="reservation_preparation", reason="workflow explicitly abandoned")

    def prepare_handoff(self, place_id: str, restaurant_name: str, restaurant_url: str,
                        date: str, time: str, party_size: int) -> str:
        """Discover and prepare one Google-grounded reservation handoff atomically."""
        return self._run_on_browser_thread(
            self._prepare_handoff_impl, place_id, restaurant_name, restaurant_url,
            date, time, party_size
        )

    def _select_availability_time(self, requested_time: str, target=None) -> dict[str, object]:
        """Select a visible matching time, but never a final booking control."""
        target = target or self.page
        if not target:
            return {"selected": False, "error": "Availability page is not open."}
        requested = requested_time.casefold()
        hour, minute = requested_time.split(":", 1)
        hour_int = int(hour)
        meridiem = "am" if hour_int < 12 else "pm"
        variants = {
            requested, f"{hour_int}:{minute}", f"{hour_int % 12 or 12}:{minute}",
            f"{hour_int % 12 or 12}:{minute} {meridiem}",
            f"{hour_int % 12 or 12} {meridiem}",
        }
        # Inputs/selects represent the requested search value, not an
        # availability slot. Only click actual option-like controls after the
        # provider has rendered its availability results.
        controls = target.locator(
            "button, [role='button'], [role='option'], [role='menuitem'], a, [data-time], [data-value]"
        )
        labels = controls.all_inner_texts()
        click_errors: list[str] = []
        visible_times: list[str] = []
        for index, label in enumerate(labels):
            compact = " ".join(label.split()).casefold()
            if re.search(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", compact, re.I):
                visible_times.append(" ".join(label.split()))
            if any(variant in compact for variant in variants) and not re.search(
                r"book|reserve|confirm|submit|complete|continue", compact, re.I
            ):
                try:
                    if not controls.nth(index).is_visible():
                        continue
                    controls.nth(index).click(timeout=1_500)
                    (target.page if hasattr(target, "page") else target).wait_for_timeout(250)
                    return {"selected": True, "label": " ".join(label.split()), "url": self.page.url}
                except Exception as exc:
                    click_errors.append(str(exc)[:240])
        return {
            "selected": False,
            "error": f"No visible availability control matched {requested_time}.",
            "url": self.page.url,
            "click_errors": click_errors[:3],
            "visible_times": visible_times[:20],
        }

    def _activate_surface(self, target):
        """Open a hidden reservation panel before searching its controls."""
        try:
            forms = target.locator("form")
            for index in range(min(forms.count(), 12)):
                form = forms.nth(index)
                if form.is_visible():
                    return form
        except Exception:
            pass

        triggers = target.locator("button, a, [role='button']")
        labels = triggers.all_inner_texts()
        for index, raw_label in enumerate(labels):
            label = " ".join(raw_label.split())
            if not re.search(r"reserv|make\s+a\s+reservation|book\s+a\s+table|reserve\s+a\s+table", label, re.I):
                continue
            if re.search(r"confirm|submit|complete|continue", label, re.I):
                continue
            try:
                control = triggers.nth(index)
                if not control.is_visible():
                    continue
                control.click(timeout=1_500)
                (target.page if hasattr(target, "page") else target).wait_for_timeout(500)
                self._log("reservation_surface_activated", label=label,
                          url=getattr(target, "url", ""))
                forms = target.locator("form")
                for form_index in range(min(forms.count(), 12)):
                    form = forms.nth(form_index)
                    if form.is_visible():
                        return form
                return target
            except Exception as exc:
                self._log("reservation_surface_activation_failed", label=label,
                          error=str(exc)[:240])
        return target

    def _rebind_workflow_surface(self, workflow_id: str, surface: dict[str, object]) -> str:
        """Attach a stable workflow to a newly observed provider surface."""
        if not self.state:
            return str(surface.get("frame_url") or self.page.url)
        old_surface_id = self._surface_id(workflow_id)
        new_surface_id = str(surface.get("surface_id") or old_surface_id)
        frame_url = str(surface.get("frame_url") or self.page.url)
        workflow = self.state.workflows.setdefault(workflow_id, {"workflow_id": workflow_id})
        workflow.update({
            "surface_id": old_surface_id,
            "observation_url": frame_url,
            "source_url": self.page.url,
            "action_urls": list(surface.get("action_urls") or []),
            "action_url_evidence": list(surface.get("action_url_evidence") or []),
            "rebound_surface_id": new_surface_id,
        })
        self.state.scanned_candidates[old_surface_id] = {
            "candidate_id": old_surface_id,
            "url": frame_url,
            "source_url": self.page.url,
            "label": surface.get("heading") or "reservation surface",
            "tag": surface.get("kind") or "section",
            "surface_id": old_surface_id,
            "workflow_id": workflow_id,
            "action_urls": list(surface.get("action_urls") or []),
            "action_url_evidence": list(surface.get("action_url_evidence") or []),
            "signals": dict(surface.get("signals") or {}),
        }
        self.candidate_pages[old_surface_id] = self.page
        self.workflow_pages[workflow_id] = self.page
        self.state.verifications[old_surface_id] = {
            "candidate_id": old_surface_id,
            "url": frame_url,
            "source_url": self.page.url,
            "verified": True,
        }
        self._log("workflow_surface_rebound", workflow_id=workflow_id,
                  old_surface_id=old_surface_id, new_surface_id=new_surface_id,
                  frame_url=frame_url, page_id=id(self.page))
        return frame_url

    def _continue_impl(self, workflow_id: str, url: str, date: str, time: str,
                       party_size: int, max_steps: int) -> str:
        """Rebind prepared workflows and run the bounded interaction loop."""
        handoff_id = self._handoff_workflow_id(workflow_id)
        handoff = self.state.reservation_handoffs.get(handoff_id or "") if self.state else None
        prepared_url = str(handoff.get("prepared_booking_url") or "") if handoff else ""
        provider = str(handoff.get("provider") or "") if handoff else ""
        active_url = url
        workflow_page = self.workflow_pages.get(workflow_id)
        if workflow_page:
            self.page = workflow_page
            self._log("workflow_page_selected", workflow_id=workflow_id,
                      page_id=id(workflow_page), current_url=workflow_page.url)
        if prepared_url and provider != "Restaurant website" and prepared_url != url:
            opened = json.loads(self._open_impl(prepared_url))
            if not opened.get("success"):
                if provider == "OpenTable":
                    # OpenTable's parameterized route can be rejected by the
                    # browser/network even though the restaurant page works.
                    # Fall back to the verified restaurant page and operate
                    # its embedded widget instead of abandoning immediately.
                    self._log("prepared_provider_fallback_start", workflow_id=workflow_id,
                              provider=provider, prepared_url=prepared_url, fallback_url=url,
                              error=opened.get("error"))
                    fallback = json.loads(self._open_impl(url))
                    if fallback.get("success"):
                        self.workflow_pages[workflow_id] = self.page
                        sweep = json.loads(self._sweep_for_url_impl(""))
                        surfaces = sweep.get("surfaces") or []
                        reservation_surfaces = [item for item in surfaces if
                            (item.get("signals") or {}).get("reservation_control_count", 0) or
                            (item.get("signals") or {}).get("has_form")]
                        if reservation_surfaces:
                            surface = max(reservation_surfaces, key=lambda item: (
                                int(bool((item.get("signals") or {}).get("has_date_control"))) * 4
                                + int(bool((item.get("signals") or {}).get("has_time_control"))) * 4
                                + int(bool((item.get("signals") or {}).get("has_party_size_control"))) * 4
                                + int((item.get("signals") or {}).get("reservation_control_count") or 0),
                            ))
                            active_url = self._rebind_workflow_surface(workflow_id, surface)
                            self._log("prepared_provider_fallback_ready", workflow_id=workflow_id,
                                      provider=provider, fallback_url=self.page.url,
                                      surface_id=surface.get("surface_id"), page_id=id(self.page))
                            return self._operate_impl(workflow_id, active_url, date, time,
                                                      party_size, max_steps)
                return self._response({
                    "success": False, "status": "blocked",
                    "error_code": "PREPARED_URL_OPEN_FAILED",
                    "workflow_id": workflow_id, "url": prepared_url,
                    "error": opened.get("error", "Prepared provider URL could not be opened."),
                    "recovery": {"tool": "reservation_abandon", "reason": "Abandon this provider and select a replacement."},
                }, (("reservation_abandon", "Abandon this workflow"),
                    ("reservation_close", "End the browser session")),
                    phase="reservation_availability", reason="prepared provider URL open failed")
            self.workflow_pages[workflow_id] = self.page
            self._log("workflow_provider_page_opened", workflow_id=workflow_id,
                      page_id=id(self.page), url=self.page.url)
            sweep = json.loads(self._sweep_for_url_impl(""))
            surfaces = sweep.get("surfaces") or []
            if not surfaces:
                return self._response({
                    "success": False, "status": "blocked",
                    "error_code": "AVAILABILITY_SURFACE_NOT_FOUND",
                    "workflow_id": workflow_id, "url": prepared_url,
                    "error": "The prepared provider page rendered no interactive reservation surface.",
                    "recovery": {"tool": "reservation_abandon", "reason": "Abandon this provider and select a replacement."},
                }, (("reservation_abandon", "Abandon this workflow"),),
                    phase="reservation_availability", reason="provider surface missing")
            surface = max(surfaces, key=lambda item: (
                int(bool((item.get("signals") or {}).get("contains_reservation_language"))) * 8
                + int(bool((item.get("signals") or {}).get("has_date_control"))) * 4
                + int(bool((item.get("signals") or {}).get("has_time_control"))) * 4
                + int(bool((item.get("signals") or {}).get("has_party_size_control"))) * 4
                + len(item.get("controls") or []),
                -int("skip" in str(item.get("heading") or "").casefold()),
            ))
            active_url = self._rebind_workflow_surface(workflow_id, surface)
        return self._operate_impl(workflow_id, active_url, date, time, party_size, max_steps)

    def operate(self, candidate_id: str, url: str, date: str, time: str,
                party_size: int, max_steps: int = 8) -> str:
        """Run bounded, semantic reservation UI actions and return a trace."""
        try:
            return self._run_on_browser_thread(
                self._continue_impl, candidate_id, url, date, time, party_size,
                max(1, min(max_steps, 12)),
            )
        except Exception as exc:
            error = self._structured_error(
                candidate_id, url, "RESERVATION_CONTINUE_FAILED",
                f"Bounded reservation interaction failed: {exc}",
                "reservation_expand",
                "Re-expand the selected workflow and inspect the current surface before retrying.",
            )
            error["retryable"] = True
            return self._response(error, (("reservation_expand", "Re-expand the selected surface"),
                ("reservation_act", "Use one observed control with a short action"),
                ("reservation_close", "End the browser session")),
                phase="reservation_availability", reason="bounded reservation interaction raised")

    def _operate_impl(self, candidate_id: str, url: str, date: str, time: str,
                      party_size: int, max_steps: int) -> str:
        target = self._verified_target(candidate_id, url)
        if target is None:
            return self._verification_required(candidate_id, url)
        surface_id = self._surface_id(candidate_id)
        observed_surface = self.state.scanned_candidates.get(surface_id, {}) if self.state else {}
        surface_kind = str(observed_surface.get("tag") or "")
        target = self._activate_surface(target)
        if surface_kind == "form":
            forms = target.locator("form")
            visible_forms = []
            for index in range(min(forms.count(), 12)):
                form = forms.nth(index)
                try:
                    if form.is_visible():
                        visible_forms.append(form)
                except Exception:
                    continue
            if visible_forms:
                target = visible_forms[0]
        trace: list[dict[str, object]] = []

        def observe_transition(action: str) -> dict[str, object]:
            """Capture a compact post-action observation without another model call."""
            try:
                page_target = target.page if hasattr(target, "page") else target
                body = " ".join(page_target.locator("body").inner_text(timeout=1_000).split())
                return {
                    "after": action,
                    "url": getattr(target, "url", url),
                    "text": body[:300],
                    "control_count": target.locator(_INTERACTIVE_SELECTOR).count(),
                    "accessibility": str(page_target.locator("body").aria_snapshot(timeout=1_000) or "")[:600],
                }
            except Exception as exc:
                return {"after": action, "observation_error": str(exc)[:240]}

        def record(action: str, success: bool, **details: object) -> bool:
            # Keep checkpoints on failures and the final availability choice;
            # repeating body/AX snippets after every field fill inflates the
            # conversation without helping the next deterministic action.
            checkpoint = observe_transition(action)
            if not success or action == "select_available_time":
                details.setdefault("post_action", checkpoint)
            trace.append({"step": len(trace) + 1, "action": action, "success": success, **details})
            return success

        def click_text(patterns: list[str], action: str, *, final_words: bool = True) -> bool:
            controls = target.locator(_INTERACTIVE_SELECTOR)
            labels = controls.all_inner_texts()
            for index, raw_label in enumerate(labels):
                label = " ".join(raw_label.split())
                lower = label.casefold()
                if any(pattern.casefold() in lower for pattern in patterns):
                    if final_words and re.search(r"book|reserve|confirm|submit|complete|continue", lower):
                        continue
                    try:
                        if not controls.nth(index).is_visible():
                            continue
                        controls.nth(index).click(timeout=1_500)
                        return record(action, True, label=label)
                    except Exception as exc:
                        return record(action, False, label=label, error=str(exc))
            return record(action, False, error=f"No control matched: {patterns}")

        def set_native_field(kind: str, value: str, action: str, labels: list[str]) -> bool:
            """Set a native form control by semantic metadata, regardless of provider."""
            controls = target.locator("select, input, textarea, [role='combobox']")
            metadata = controls.evaluate_all(
                """els => els.map(el => ({
                    tag: el.tagName.toLowerCase(), type: el.type || '', name: el.name || '',
                    id: el.id || '', aria: el.getAttribute('aria-label') || '',
                    placeholder: el.placeholder || '',
                    context: (el.parentElement?.innerText || '').replace(/\\s+/g, ' ').slice(0, 300)
                }))"""
            )
            needles = [item.casefold() for item in labels]
            for index, meta in enumerate(metadata):
                haystack = " ".join(str(meta.get(key) or '') for key in
                                     ("name", "id", "aria", "placeholder", "context")).casefold()
                if not any(needle in haystack for needle in needles):
                    continue
                locator = controls.nth(index)
                try:
                    if not locator.is_visible():
                        continue
                    if str(meta.get("tag")) == "select":
                        options = locator.locator("option").evaluate_all(
                            """els => els.map(o => ({label: o.textContent.trim(), value: o.value}))"""
                        )
                        wanted = value.casefold()
                        option = next((item for item in options if
                                       wanted in str(item["label"]).casefold() or
                                       wanted == str(item["value"]).casefold()), None)
                        if not option and kind == "party":
                            option = next((item for item in options if
                                           re.search(rf"\\b{re.escape(value)}\\b", str(item["label"]))), None)
                        if not option:
                            continue
                        locator.select_option(value=option["value"], timeout=1_500)
                    else:
                        locator.fill(value, timeout=1_500)
                    return record(action, True, control=haystack[:160], value=value)
                except Exception as exc:
                    return record(action, False, control=haystack[:160], error=str(exc))
            return False

        def choose_value(value_patterns: list[str], action: str) -> bool:
            return click_text(value_patterns, action)

        if len(trace) < max_steps:
            native_party = set_native_field("party", str(party_size), "set_guest_count",
                                            ["guest", "people", "party"])
            if not native_party and click_text(["Guests", "People"], "open_guest_picker"):
                if len(trace) < max_steps:
                    choose_value([f"{party_size} guest", f"{party_size} people"], "select_guest_count")
        if len(trace) < max_steps:
            native_date = set_native_field("date", date, "set_date", ["date", "day"])
            if not native_date and click_text(["Date"], "open_date_picker"):
                month_day = datetime.strptime(date, "%Y-%m-%d").strftime("%B %-d")
                short_month_day = datetime.strptime(date, "%Y-%m-%d").strftime("%b %-d")
                day = str(datetime.strptime(date, "%Y-%m-%d").day)
                if len(trace) < max_steps:
                    choose_value([month_day, short_month_day, date, day], "select_date")
        if len(trace) < max_steps:
            hour, minute = time.split(":", 1)
            hour_int = int(hour)
            meridiem = "AM" if hour_int < 12 else "PM"
            display = f"{hour_int % 12 or 12}:{minute} {meridiem}"
            native_time = set_native_field("time", display, "set_time", ["time"])
            if not native_time and click_text(["Time", "All Times"], "open_time_picker"):
                if len(trace) < max_steps:
                    choose_value([display, f"{hour_int % 12 or 12} {meridiem}", time], "select_time")
        if len(trace) < max_steps:
            click_text(["Search", "Find a table", "Availability", "Show times"], "search_availability",
                       final_words=False)
        if len(trace) < max_steps:
            (target.page if hasattr(target, "page") else target).wait_for_timeout(750)
            selected = self._select_availability_time(time, target)
            record("select_available_time", bool(selected.get("selected")), **selected)

        observation = json.loads(self._observe_impl(candidate_id, url, include_screenshot=False))
        result = {
            "success": any(item["action"] == "select_available_time" and item["success"] for item in trace),
            "candidate_id": candidate_id,
            "workflow_id": self._workflow_id(candidate_id),
            "url": self.page.url if self.page else url,
            "date": date,
            "time": time,
            "party_size": party_size,
            "action_trace": trace,
            "continuation_required": True,
            "final_action_blocked": True,
            "availability_verified": False,
            "accessibility_snapshot": observation.get("accessibility_snapshot"),
            "screenshot_path": observation.get("screenshot_path"),
            "warnings": observation.get("warnings", []),
        }
        if result["success"] and self.state:
            result["availability_verified"] = True
            workflow_id = self._workflow_id(candidate_id)
            if workflow_id:
                self._transition_workflow(
                    candidate_id, "handoff_ready", availability_verified=True,
                    selected_time=time,
                )
            handoff_workflow_id = self._handoff_workflow_id(candidate_id)
            for handoff_id, handoff in self.state.reservation_handoffs.items():
                if (handoff.get("candidate_id") == self._surface_id(candidate_id)
                        or handoff_id == handoff_workflow_id):
                    handoff["interactive_complete"] = True
                    handoff["availability_verified"] = True
                    handoff["availability_action"] = "requested time selected"
                    handoff["last_action"] = "select_available_time"
        if not result["success"]:
            result["error"] = "The requested available time was not selected."
            result["status"] = "blocked"
            selected_step = next((item for item in trace if item.get("action") == "select_available_time"), {})
            visible_times = selected_step.get("visible_times") or []
            result["error_code"] = (
                "REQUESTED_TIME_UNAVAILABLE" if visible_times else "AVAILABLE_TIME_NOT_FOUND"
            )
            if visible_times:
                result["nearby_available_times"] = visible_times
            result["recovery"] = {
                "tool": "reservation_act" if visible_times else "reservation_expand",
                "reason": (
                    "Requested time was not offered; use the visible alternatives or abandon this workflow."
                    if visible_times else
                    "Inspect the post-search availability controls and select an observed time."
                ),
            }
            if self.state:
                handoff_workflow_id = self._handoff_workflow_id(candidate_id)
                for handoff_id, handoff in self.state.reservation_handoffs.items():
                    if (handoff.get("candidate_id") == self._surface_id(candidate_id)
                            or handoff_id == handoff_workflow_id):
                        handoff["last_action"] = trace[-1].get("action") if trace else None
                        handoff["failure"] = result["error"]
        return self._response(result, (("reservation_operate", "Retry the bounded reservation interaction"),
                                       ("reservation_inspect", "Inspect the current reservation controls"),
                                       ("reservation_close", "End the browser session")),
                              phase="reservation_availability",
                              reason="bounded reservation interaction completed")

    def _prepare_handoff_impl(self, place_id: str, restaurant_name: str,
                              restaurant_url: str, date: str, time: str,
                              party_size: int) -> str:
        if not place_id or not restaurant_name:
            return self._response({
                "success": False,
                "error": "place_id and restaurant_name are required from Google Places.",
            }, (("google_places_details", "Hydrate the Google restaurant candidate"),),
                phase="reservation_preparation", reason="Google candidate identity missing")
        if not restaurant_url:
            return self._response({
                "success": False,
                "place_id": place_id,
                "restaurant_name": restaurant_name,
                "error": "An exact Google-provided restaurant or booking URL is required.",
            }, (("google_places_details", "Hydrate the Google restaurant candidate"),),
                phase="reservation_preparation", reason="exact restaurant URL missing")

        opened = json.loads(self._open_impl(restaurant_url))
        if not opened.get("success"):
            return self._response({
                "success": False, "place_id": place_id, "restaurant_name": restaurant_name,
                "url": restaurant_url, "error": opened.get("error", "Unable to open restaurant URL."),
            }, (("reservation_open", "Retry the exact Google-provided restaurant URL"),
                ("reservation_close", "End the browser session")),
                phase="reservation_preparation", reason="restaurant page open failed")

        scan = json.loads(self._scan_dom_impl())
        candidates = [item for item in scan.get("candidates", []) if item.get("isBooking")]
        self._log(
            "reservation_url_candidates",
            place_id=place_id,
            restaurant_name=restaurant_name,
            count=len(candidates),
            candidates=[{
                "candidate_id": item.get("candidate_id"),
                "tag": item.get("tag"),
                "label": item.get("label"),
                "href": item.get("href"),
                "url": item.get("url"),
                "opentableUrl": item.get("opentableUrl"),
                "score": item.get("score"),
                "form": item.get("form"),
            } for item in candidates[:20]],
        )
        if not candidates:
            self._log("reservation_url_generation_skipped", place_id=place_id, reason="no_booking_candidate")
            return self._response({
                "success": False, "place_id": place_id, "restaurant_name": restaurant_name,
                "url": self.page.url if self.page else restaurant_url,
                "error": "No reservation candidate was found on the exact restaurant page.",
                "scan": scan,
            }, (("reservation_observe", "Observe the verified restaurant page"),
                ("reservation_scan_dom", "Rescan the exact restaurant page"),
                ("reservation_close", "End the browser session")),
                phase="reservation_preparation", reason="reservation candidate not found")

        def candidate_priority(item: dict[str, object]) -> tuple[int, int, int]:
            """Prefer exact provider/frame actions over a parent-page form."""
            href = str(item.get("href") or "")
            item_url = str(item.get("url") or "")
            provider_url = href or item_url
            provider = classify_booking_provider(provider_url, restaurant_url)
            exact_provider = int(provider in {"OpenTable", "Resy", "Tock", "Toast", "Google Reserve"})
            embedded_surface = int(
                bool(item.get("form")) and str(item.get("frame_url") or "") != str(self.page.url if self.page else "")
            )
            return exact_provider, embedded_surface, int(item.get("score") or 0)

        candidate = max(candidates, key=candidate_priority)
        candidate_id = str(candidate["candidate_id"])
        candidate_url = str(candidate["url"])
        self._log(
            "reservation_url_candidate_selected",
            place_id=place_id,
            candidate_id=candidate_id,
            tag=candidate.get("tag"),
            label=candidate.get("label"),
            href=candidate.get("href"),
            candidate_url=candidate_url,
            opentable_url=candidate.get("opentableUrl"),
        )
        # A normal reservation anchor is an exact observed navigation action,
        # not an active target yet. Follow it, then bind the handoff to the
        # resulting page candidate so later semantic actions remain verified.
        if candidate_url != (self.page.url if self.page else "") and candidate.get("tag") != "iframe":
            followed = json.loads(self._open_impl(candidate_url))
            if not followed.get("success"):
                return self._response({
                    "success": False, "place_id": place_id, "restaurant_name": restaurant_name,
                    "url": candidate_url,
                    "error": followed.get("error", "Unable to follow the observed reservation link."),
                }, (("reservation_close", "End the browser session"),),
                    phase="reservation_preparation", reason="observed reservation link failed")
            self._scan_dom_impl()
            candidate_url = self.page.url
            candidate_id = _candidate_id(candidate_url)
        verified = json.loads(self._verify_impl(candidate_id, candidate_url))
        if not verified.get("success"):
            return self._response({
                "success": False, "place_id": place_id, "restaurant_name": restaurant_name,
                "url": self.page.url if self.page else restaurant_url,
                "error": verified.get("error", "Reservation candidate verification failed."),
            }, (("reservation_scan_dom", "Rescan the exact restaurant page"),
                ("reservation_close", "End the browser session")),
                phase="reservation_preparation", reason="reservation candidate verification failed")

        observation = json.loads(self._observe_impl(candidate_id, candidate_url, include_screenshot=False))
        booking_url = str(candidate.get("opentableUrl") or candidate_url)
        provider = classify_booking_provider(booking_url, self.page.url if self.page else restaurant_url)
        prepared_url = booking_url
        prefilled = False
        url_origin = "observed_opentable_url" if candidate.get("opentableUrl") else (
            "observed_href" if candidate.get("href") else "page_or_frame_url"
        )
        if provider == "OpenTable":
            prepared_url = build_opentable_availability_url(
                booking_url, date=date, time=time, party_size=party_size
            )
            prefilled = True
        self._log(
            "reservation_url_generated",
            place_id=place_id,
            candidate_id=candidate_id,
            url_origin=url_origin,
            provider=provider,
            source_url=self.page.url if self.page else restaurant_url,
            booking_url=booking_url,
            prepared_url=prepared_url,
            prefilled=prefilled,
            reason=("opentable_parameters_applied" if prefilled else
                    "no_supported_prefill_or_exact_provider_url"),
        )

        availability_action: dict[str, object] = {"selected": False, "skipped": True}

        result = {
            "success": True,
            "place_id": place_id,
            "restaurant_name": restaurant_name,
            "source_url": self.page.url if self.page else restaurant_url,
            "candidate_id": candidate_id,
            "booking_url": booking_url,
            "prepared_booking_url": prepared_url,
            "provider": provider,
            "date": date,
            "time": time,
            "party_size": party_size,
            "prefilled": prefilled,
            "handoff_ready": prefilled,
            "availability_action": availability_action,
            "reservation_candidate": candidate,
            "accessibility_snapshot": observation.get("accessibility_snapshot"),
            "screenshot_path": observation.get("screenshot_path"),
            "warnings": observation.get("warnings", []),
        }
        if self.state:
            self.state.reservation_handoffs[place_id] = {
                "place_id": place_id,
                "restaurant_name": restaurant_name,
                "candidate_id": candidate_id,
                "prepared_booking_url": prepared_url,
                "provider": provider,
                "prefilled": prefilled,
                "interactive_complete": False,
            }
        if not prefilled:
            result["warning"] = "Provider URL needs interactive reservation_operate actions to select availability."
        return self._response(result, (("reservation_operate", "Set details and select the requested available time"),
                                       ("reservation_close", "End the browser session")),
                              phase="reservation_preparation", reason="reservation handoff prepared")

    def _prepare_impl(self, candidate_id: str, url: str, booking_url: str,
                      date: str, time: str, party_size: int) -> str:
        target = self._verified_target(candidate_id, url)
        if target is None:
            return self._verification_required(candidate_id, url)
        surface_id = self._surface_id(candidate_id)
        observed = self.state.scanned_candidates.get(surface_id) if self.state else None
        observed_urls = set()
        observed_action_urls = set()
        if observed:
            observed_urls.add(str(observed.get("url") or ""))
            observed_action_urls.update(str(item) for item in (observed.get("action_urls") or []))
            observed_action_urls.update(
                str(item.get("url")) for item in (observed.get("action_url_evidence") or [])
                if isinstance(item, dict) and item.get("url")
            )
            if observed.get("action_url"):
                observed_action_urls.add(str(observed["action_url"]))
            observed_urls.update(observed_action_urls)
        observed_page_urls = {
            str(observed.get("url") or "") if observed else "",
            str(observed.get("source_url") or "") if observed else "",
        }
        if not observed or url not in observed_page_urls or booking_url not in observed_action_urls:
            error = self._structured_error(
                candidate_id, url, "ACTION_URL_NOT_AUTHORIZED",
                "Booking URL was not observed in the selected surface.",
                "reservation_expand", "Expand the surface and use one of its observed action_urls.",
            )
            error["booking_url"] = booking_url
            return self._response(error, (("reservation_expand", "Expand the selected surface and inspect action_urls"),
                ("reservation_close", "End the browser session")),
                phase="reservation_preparation", reason="unobserved booking URL rejected")
        provider = classify_booking_provider(booking_url, url)
        prepared_url = booking_url
        if provider == "OpenTable":
            prepared_url = build_opentable_availability_url(
                booking_url, date=date, time=time, party_size=party_size
            )
        workflow_id = self._workflow_id(candidate_id) or f"workflow-{candidate_id}"
        self._transition_workflow(
            candidate_id, "url_prepared", prepared_booking_url=prepared_url,
            provider=provider, requested_date=date, requested_time=time,
            party_size=party_size,
        )
        if self.state:
            self.state.reservation_handoffs[workflow_id] = {
                "workflow_id": workflow_id,
                "candidate_id": surface_id,
                "restaurant_name": str(observed.get("label") or "selected restaurant surface"),
                "prepared_booking_url": prepared_url,
                "provider": provider,
                "url_prefilled": provider == "OpenTable",
                "prefilled": False,
                "interactive_complete": False,
                "availability_verified": False,
                "source_url": url,
                "requested_date": date,
                "requested_time": time,
                "party_size": party_size,
            }
        next_actions = (("reservation_open", "Open the exact prepared provider URL and resweep it"),
                        ("reservation_sweep", "Sweep the prepared page for the availability surface"),
                        ("reservation_close", "End the browser session")) if provider != "Restaurant website" else (
                        ("reservation_expand", "Expand the current reservation surface"),
                        ("reservation_act", "Set fields and select an observed available time"),
                        ("reservation_close", "End the browser session"))
        return self._response({
            "success": True,
            "status": "needs_action",
            "workflow_id": workflow_id,
            "candidate_id": surface_id,
            "surface_id": surface_id,
            "candidate": {"candidate_id": surface_id, "workflow_id": workflow_id, "url": url},
            "url": url,
            "booking_url": booking_url,
            "prepared_booking_url": prepared_url,
            "provider": provider,
            "url_prefilled": provider == "OpenTable",
            "prefilled": False,
            "handoff_ready": False,
            "availability_verified": False,
            "prepared": {"date": date, "time": time, "party_size": party_size},
            "next_step": "Open or expand the prepared flow, select the requested available time, and stop before final booking.",
        }, next_actions,
            phase="reservation_preparation", reason="verified booking handoff prepared")

    def _click_impl(self, candidate_id: str, url: str, label: str) -> str:
        """Click a non-submitting control such as Search or Find a table."""
        target = self._verified_target(candidate_id, url)
        if target is None:
            return self._verification_required(candidate_id, url)
        if not self.page:
            return self._response({"success": False, "error": "Open a booking URL first."},
                                  (("reservation_open", "Open an exact restaurant URL"),),
                                  phase="reservation_preparation", reason="click requested without a page")
        is_availability_action = re.search(
            r"find\s+a\s+table|search|availability|show\s+times|check\s+availability",
            label, re.I,
        )
        if re.search(r"book|reserve|confirm|submit|complete|continue", label, re.I) and not is_availability_action:
            return self._response({"success": False, "error": "Final booking controls require organizer confirmation."},
                                  (("reservation_inspect", "Inspect the confirmation state"),
                                   ("reservation_close", "End the browser session")),
                                  phase="reservation_preparation", reason="final booking action gated")
        try:
            locator = target.get_by_role("button", name=label, exact=False).first
            if locator.count() == 0:
                locator = target.get_by_text(label, exact=False).first
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

def _model_screenshot(path_value: object) -> dict[str, object] | None:
    """Return a bounded image block safe for the model provider payload."""
    if not isinstance(path_value, str):
        return None
    path = Path(path_value)
    if not path.is_file():
        return None
    data = path.read_bytes()
    # Bedrock rejects images at 5 MiB; keep headroom for transport overhead.
    if len(data) >= 4_500_000:
        logger.warning("reservation_browser stage=model_screenshot_omitted path=%s bytes=%d",
                       path, len(data))
        return None
    return {
        "image": {
            "format": "jpeg" if path.suffix.casefold() in {".jpg", ".jpeg"} else "png",
            "source": {"bytes": data},
        }
    }


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
    def reservation_observe(candidate_id: str, url: str, include_screenshot: bool = False) -> dict[str, object]:
        """Observe a verified candidate with DOM, accessibility-tree, and optional screenshot evidence."""
        result = json.loads(browser.observe(candidate_id, url, include_screenshot))
        content: list[dict[str, object]] = [{"text": json.dumps(result, default=str)}]
        screenshot = _model_screenshot(result.get("screenshot_path"))
        if screenshot:
            content.append(screenshot)
        return {"status": "success", "content": content}

    @tool
    def reservation_scan_dom(website_url: str = "") -> str:
        """Open and scan one exact restaurant URL as one serialized operation."""
        return browser.scan_dom(website_url)

    @tool
    def reservation_sweep(website_url: str = "") -> dict[str, object]:
        """Sweep the complete rendered page and frames without classifying booking candidates."""
        result = json.loads(browser.sweep(website_url))
        # Screenshots remain on disk as evidence, but are not sent on every
        # sweep. They are expensive model input and rarely add information to
        # the structured surface map.
        return {"status": "success", "content": [{"text": json.dumps(result, default=str)}]}

    @tool
    def reservation_expand(workflow_id: str, url: str,
                           include_screenshot: bool = False) -> dict[str, object]:
        """Expand one agent-selected surface with DOM, AX, and screenshot evidence."""
        result = json.loads(browser.expand(workflow_id, url, include_screenshot))
        content: list[dict[str, object]] = [{"text": json.dumps(result, default=str)}]
        return {"status": "success", "content": content}

    @tool
    def reservation_fill(candidate_id: str, url: str, field: str, value: str) -> str:
        """Fill one identified booking field; never submits the form."""
        return browser.fill(candidate_id, url, field, value)

    @tool
    def reservation_click(candidate_id: str, url: str, label: str) -> str:
        """Click a search/availability control, never a final booking control."""
        return browser.click(candidate_id, url, label)

    @tool
    def reservation_act(workflow_id: str, url: str, action: str,
                        target: str, value: str = "") -> str:
        """Apply one semantic action from reservation_observe: click or set/fill/select a labeled control."""
        return browser.act(workflow_id, url, action, target, value)

    @tool
    def reservation_prepare(workflow_id: str, url: str, booking_url: str,
                            date: str, time: str, party_size: int) -> str:
        """Prepare an observed booking URL from a verified page, without submitting."""
        return browser.prepare(workflow_id, url, booking_url, date, time, party_size)

    @tool
    def reservation_continue(workflow_id: str, url: str, date: str, time: str,
                             party_size: int, max_steps: int = 8) -> dict[str, object]:
        """Continue one verified workflow through availability selection, never final booking."""
        result = json.loads(browser.operate(
            workflow_id, url, date, time, party_size, max_steps
        ))
        content: list[dict[str, object]] = [{"text": json.dumps(result, default=str)}]
        return {"status": "success", "content": content}

    @tool
    def reservation_abandon(workflow_id: str, reason: str) -> str:
        """Explicitly abandon one prepared workflow without claiming availability."""
        return browser.abandon(workflow_id, reason)

    @tool
    def reservation_prepare_handoff(place_id: str, restaurant_name: str,
                                    restaurant_url: str, date: str, time: str,
                                    party_size: int) -> dict[str, object]:
        """Prepare one verified handoff from an exact Google Places restaurant URL."""
        result = json.loads(browser.prepare_handoff(
            place_id, restaurant_name, restaurant_url, date, time, party_size
        ))
        content: list[dict[str, object]] = [{"text": json.dumps(result, default=str)}]
        return {"status": "success", "content": content}

    @tool
    def reservation_operate(candidate_id: str, url: str, date: str, time: str,
                            party_size: int, max_steps: int = 8) -> dict[str, object]:
        """Run bounded semantic reservation actions without final booking or continuation."""
        result = json.loads(browser.operate(candidate_id, url, date, time, party_size, max_steps))
        content: list[dict[str, object]] = [{"text": json.dumps(result, default=str)}]
        return {"status": "success", "content": content}

    @tool
    def reservation_close() -> str:
        """Close the organizer-scoped browser session after the booking handoff."""
        browser.close()
        return browser._response({"success": True, "message": "Reservation browser session closed."},
                                 (), phase="cleanup", reason="browser session closed")

    # Keep the normal agent surface focused on intent-level browser work. The
    # primitive tools remain implemented for recovery/tests, but exposing all
    # of them by default invites the model back into manual choreography.
    return browser, [reservation_open, reservation_sweep, reservation_expand,
                     reservation_prepare, reservation_continue, reservation_act,
                     reservation_abandon,
                     reservation_close]
