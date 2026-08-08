from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.parse import urlencode, urlsplit, quote
from pathlib import Path
import json
import os
import threading
import time
import requests
from requests.adapters import HTTPAdapter

from playwright.sync_api import sync_playwright

from config import (
    EMAIL,
    PASSWORD,
    LOGIN_URL,
)


_SESSION_LOCK = threading.RLock()
_AUTH_CACHE_LOCK = threading.RLock()
_AUTH_OK_UNTIL = 0.0
_AUTH_CACHE_SECONDS = 4 * 60 * 60.0
# Shared pressure guard across reports, searches and identity syncs. Multiple
# browser tabs can run at once, but OPOST becomes slower when flooded.
_GLOBAL_OPOST_REQUESTS = threading.BoundedSemaphore(18)
_PROJECT_DIR = Path(__file__).resolve().parent
_SESSION_DIR = _PROJECT_DIR / "data"
_SESSION_STATE_PATH = Path(
    os.getenv("OPOST_SESSION_STATE", str(_SESSION_DIR / "opost_session.json"))
)


class OpostClient:

    def __init__(self):

        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.deadline_monotonic = None
        self.http = requests.Session()
        self.http.headers.update({
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "App-Language": "en",
            "User-Agent": "Mozilla/5.0 Optimus-OPOST-Client/2.3.2",
        })
        # Keep a large reusable connection pool. OPOST reports can contain more
        # than one hundred pages; opening a fresh TCP/TLS connection per page is
        # one of the largest avoidable delays.
        self._http_adapter = HTTPAdapter(
            pool_connections=64,
            pool_maxsize=64,
            max_retries=0,
            pool_block=True,
        )
        self.http.mount("https://", self._http_adapter)
        self.http.mount("http://", self._http_adapter)
        parts = urlsplit(LOGIN_URL)
        self.origin = f"{parts.scheme}://{parts.netloc}"

    # ==========================================
    # Browser
    # ==========================================

    def start(self):
        """Prepare only the lightweight authenticated HTTP session.

        Chromium is launched only if the saved OPOST login has expired. This
        removes repeated browser startup from report, search and identity work.
        """
        _SESSION_DIR.mkdir(parents=True, exist_ok=True)
        self._load_http_cookies()

    def _load_http_cookies(self) -> None:
        self.http.cookies.clear()
        if not _SESSION_STATE_PATH.exists():
            return
        try:
            payload = json.loads(_SESSION_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for cookie in payload.get("cookies", []) or []:
            try:
                self.http.cookies.set(
                    str(cookie.get("name")),
                    str(cookie.get("value")),
                    domain=cookie.get("domain") or None,
                    path=cookie.get("path") or "/",
                )
            except Exception:
                continue

    def _launch_login_browser(self) -> None:
        if self.playwright is None:
            self.playwright = sync_playwright().start()
        if self.browser is None:
            self.browser = self.playwright.chromium.launch(headless=True)
        options = {"viewport": {"width": 1600, "height": 900}}
        if _SESSION_STATE_PATH.exists():
            options["storage_state"] = str(_SESSION_STATE_PATH)
        self.context = self.browser.new_context(**options)
        self.page = self.context.new_page()
        self.page.set_default_timeout(30000)

    # ==========================================
    # Persistent Login Session
    # ==========================================

    @staticmethod
    def _api_probe_url() -> str:
        parts = urlsplit(LOGIN_URL)
        return f"{parts.scheme}://{parts.netloc}/en/w/resources/businesses?page=1&limit=1"

    def _is_authenticated(self) -> bool:
        try:
            response = self.http.get(self._api_probe_url(), timeout=(3, 5), allow_redirects=False)
            content_type = response.headers.get("content-type", "").lower()
            return response.ok and "json" in content_type
        except requests.RequestException:
            return False

    def _save_session_state(self) -> None:
        if self.context is None:
            return
        _SESSION_DIR.mkdir(parents=True, exist_ok=True)
        temporary = _SESSION_STATE_PATH.with_name(
            f"{_SESSION_STATE_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            self.context.storage_state(path=str(temporary))
            for attempt in range(6):
                try:
                    os.replace(temporary, _SESSION_STATE_PATH)
                    return
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.15 * (attempt + 1))
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _reload_saved_session(self) -> None:
        self._load_http_cookies()

    def login(self, force_check: bool = False):
        global _AUTH_OK_UNTIL
        if not EMAIL or not PASSWORD:
            raise RuntimeError(
                "بيانات دخول OPOST غير موجودة. ضع OPOST_EMAIL و OPOST_PASSWORD في ملف .env."
            )

        now = time.monotonic()
        with _AUTH_CACHE_LOCK:
            recently_verified = bool(self.http.cookies) and now < _AUTH_OK_UNTIL
        if recently_verified and not force_check:
            print("✅ Reused warm OPOST session")
            return

        # A persisted cookie jar is normally valid. Use it immediately instead of
        # adding a blocking probe to every first action after app startup. API
        # calls still detect 401/403 and trigger the existing one-time relogin.
        if self.http.cookies and not force_check:
            with _AUTH_CACHE_LOCK:
                _AUTH_OK_UNTIL = time.monotonic() + _AUTH_CACHE_SECONDS
            print("✅ Reused persisted OPOST session optimistically")
            return

        if self._is_authenticated():
            with _AUTH_CACHE_LOCK:
                _AUTH_OK_UNTIL = time.monotonic() + _AUTH_CACHE_SECONDS
            print("✅ Reused saved OPOST HTTP session")
            return

        with _SESSION_LOCK:
            self._reload_saved_session()
            with _AUTH_CACHE_LOCK:
                recently_verified = bool(self.http.cookies) and time.monotonic() < _AUTH_OK_UNTIL
            if recently_verified and not force_check:
                print("✅ Reused refreshed warm OPOST session")
                return
            if self._is_authenticated():
                with _AUTH_CACHE_LOCK:
                    _AUTH_OK_UNTIL = time.monotonic() + _AUTH_CACHE_SECONDS
                print("✅ Reused refreshed OPOST HTTP session")
                return

            print("🔑 Login to OPOST (session expired or first run)...")
            self._launch_login_browser()
            try:
                self.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
                if not self._is_authenticated():
                    email_input = self.page.locator('input[type="email"]').first
                    password_input = self.page.locator('input[type="password"]').first
                    email_input.wait_for(state="visible", timeout=30000)
                    password_input.wait_for(state="visible", timeout=30000)
                    email_input.fill(EMAIL)
                    password_input.fill(PASSWORD)
                    self.page.locator('button[type="submit"]').first.click()

                for _ in range(20):
                    self.page.wait_for_timeout(1000)
                    try:
                        self.context.storage_state(path=str(_SESSION_STATE_PATH))
                    except Exception:
                        pass
                    self._load_http_cookies()
                    if self._is_authenticated():
                        self._save_session_state()
                        self._load_http_cookies()
                        with _AUTH_CACHE_LOCK:
                            _AUTH_OK_UNTIL = time.monotonic() + _AUTH_CACHE_SECONDS
                        print("✅ Logged In and saved persistent OPOST session")
                        # All normal work uses the HTTP session. Release Chromium
                        # immediately after refreshing the login cookies.
                        try:
                            self.context.close()
                            self.browser.close()
                            self.playwright.stop()
                        except Exception:
                            pass
                        self.context = self.browser = self.playwright = self.page = None
                        return
                raise RuntimeError("فشل التحقق من تسجيل الدخول إلى OPOST.")
            except Exception:
                # Close only on failed login. On success the page must remain
                # alive because report/search/identity methods use page.evaluate.
                if self.context is not None:
                    try: self.context.close()
                    except Exception: pass
                if self.browser is not None:
                    try: self.browser.close()
                    except Exception: pass
                if self.playwright is not None:
                    try: self.playwright.stop()
                    except Exception: pass
                self.context = self.browser = self.playwright = self.page = None
                raise

    # ==========================================
    # Normalize API Payload
    # ==========================================

    @staticmethod
    def _normalize_payload(
        result: Any,
        resource: str
    ) -> Dict[str, Any]:

        if isinstance(result, list):

            if not result:
                raise RuntimeError(
                    "Empty API response for: "
                    + resource
                )

            payload = result[0]

        elif isinstance(result, dict):

            payload = result

        else:

            raise RuntimeError(
                "Unexpected API response for: "
                + resource
            )

        if not isinstance(payload, dict):

            raise RuntimeError(
                "Invalid API payload for: "
                + resource
            )

        return payload

    # ==========================================
    # API Request
    # ==========================================

    def _request(
        self,
        resource: str
    ) -> Dict[str, Any]:
        """Request OPOST JSON through the persistent HTTP session.

        Older builds executed ``fetch('/en/w/...')`` inside an about:blank
        Playwright page. Relative URLs cannot be resolved there and caused
        "Failed to parse URL". Using the authenticated requests session is
        also considerably faster and keeps search, identities and reports on
        one shared login session.
        """
        resource = str(resource or "").lstrip("/")
        url = f"{self.origin}/en/w/resources/{resource}"

        def perform():
            response = self.http.get(url, timeout=35, allow_redirects=False)
            content_type = response.headers.get("content-type", "").lower()
            if response.status_code in (401, 403) or response.status_code in (301, 302, 303, 307, 308):
                return None, response
            response.raise_for_status()
            if "json" not in content_type:
                raise RuntimeError(
                    f"OPOST returned a non-JSON response for {resource}: HTTP {response.status_code}"
                )
            return response.json(), response

        result, response = perform()
        if result is None:
            # Session expired. Refresh it once under the shared login lock and retry.
            global _AUTH_OK_UNTIL
            with _AUTH_CACHE_LOCK:
                _AUTH_OK_UNTIL = 0.0
            self.login(force_check=True)
            self._load_http_cookies()
            result, response = perform()
        if result is None:
            raise RuntimeError(
                f"OPOST session is not authenticated for {resource}: HTTP {response.status_code}"
            )
        return self._normalize_payload(result, resource)

    # ==========================================
    # Add Page Parameter
    # ==========================================

    @staticmethod
    def _with_page(
        resource: str,
        page: int
    ) -> str:

        separator = (
            "&"
            if "?" in resource
            else "?"
        )

        return (
            f"{resource}"
            f"{separator}"
            f"page={page}"
        )

    # ==========================================
    # One Page
    # ==========================================

    def get_page(
        self,
        resource: str,
        page: int = 1
    ) -> Dict[str, Any]:

        return self._request(
            self._with_page(
                resource,
                page
            )
        )

    # ==========================================
    # Fast Remaining Pages
    # ==========================================

    def _get_pages_parallel(
        self,
        resource: str,
        start_page: int,
        end_page: int,
        concurrency: int = 20,
        progress_callback=None,
    ) -> List[Dict[str, Any]]:
        if start_page > end_page:
            return []
        concurrency = max(1, min(int(concurrency or 20), 32))
        pages = list(range(start_page, end_page + 1))
        all_pages: List[Dict[str, Any]] = []

        cookies = requests.utils.dict_from_cookiejar(self.http.cookies)
        headers = dict(self.http.headers)

        def load(page_number: int):
            session = requests.Session()
            session.headers.update(headers)
            session.cookies.update(cookies)
            url = f"{self.origin}/en/w/resources/{self._with_page(resource, page_number)}"
            last = None
            for attempt in range(1, 4):
                try:
                    response = session.get(url, timeout=35)
                    response.raise_for_status()
                    payload = self._normalize_payload(response.json(), f"{resource} page={page_number}")
                    return page_number, payload
                except Exception as error:
                    last = error
                    if attempt < 3:
                        time.sleep(0.5 * attempt)
            raise RuntimeError(f"Page {page_number} failed: {last}")

        completed = 0
        with ThreadPoolExecutor(max_workers=min(concurrency, len(pages))) as executor:
            futures = {executor.submit(load, page): page for page in pages}
            for future in as_completed(futures):
                page_number, payload = future.result()
                all_pages.append({"page": page_number, "payload": payload})
                completed += 1
                if progress_callback is not None:
                    progress_callback(completed, len(pages))
        all_pages.sort(key=lambda item: item["page"])
        return all_pages

    # ==========================================
    # Load All Pages Fast
    # ==========================================

    def get_all(
        self,
        resource: str,
        concurrency: int = 20,
        progress_callback=None,
    ) -> List[Dict[str, Any]]:
        print(f"\n📥 Loading {resource}")
        first_page = self.get_page(resource, 1)
        data = list(first_page.get("data", []) or [])
        pagination = first_page.get("pagination", {}) or {}
        last_page = int(pagination.get("last_page", 1) or 1)
        print(f"Page 1/{last_page}")
        if progress_callback is not None:
            progress_callback(1, last_page)

        if last_page > 1:
            remaining_pages = self._get_pages_parallel(
                resource=resource,
                start_page=2,
                end_page=last_page,
                concurrency=concurrency,
                progress_callback=(
                    (lambda loaded, total: progress_callback(loaded + 1, last_page))
                    if progress_callback is not None else None
                ),
            )
            for page_item in remaining_pages:
                payload = page_item.get("payload", {}) or {}
                data.extend(list(payload.get("data", []) or []))

        print(f"✅ Loaded {len(data)} records")
        return data

    # ==========================================
    # Businesses Created During Period
    # ==========================================

    def get_businesses(
        self,
        start_date: str,
        end_date: str
    ) -> List[Dict[str, Any]]:

        params = {
            "created_at": (
                f"{start_date} "
                f"to {end_date}"
            ),

            # محاولة تقليل الصفحات
            "limit": 5000,
        }

        resource = (
            "businesses?"
            + urlencode(params)
        )

        return self.get_all(
            resource,
            concurrency=20
        )

    # ==========================================
    # Extract Business ID
    # ==========================================

    @staticmethod
    def _extract_business_id(
        shipment: Dict[str, Any]
    ) -> Optional[int]:
        """Extract the owning Business ID from known OPOST payload shapes."""

        def as_int(value: Any) -> Optional[int]:
            if value is None or isinstance(value, bool):
                return None
            if isinstance(value, dict):
                for key in ("id", "resource_id", "value"):
                    candidate = as_int(value.get(key))
                    if candidate is not None:
                        return candidate
                return None
            try:
                text = str(value).strip()
                if not text:
                    return None
                return int(float(text))
            except (TypeError, ValueError, OverflowError):
                return None

        # Direct/top-level relationships used by some API variants.
        for candidate in (
            shipment.get("business_id"),
            shipment.get("business"),
            shipment.get("sender_business_id"),
            shipment.get("merchant_id"),
            shipment.get("sender"),
            shipment.get("merchant"),
        ):
            value = as_int(candidate)
            if value is not None:
                return value

        for field in shipment.get("fields", []) or []:
            if not isinstance(field, dict):
                continue

            attribute = str(
                field.get("attribute")
                or field.get("related_name")
                or field.get("name")
                or ""
            ).strip().lower()

            if attribute not in {
                "business",
                "business.name",
                "business_id",
                "sender",
                "sender.business",
                "sender_business",
                "merchant",
                "merchant_id",
            }:
                continue

            for candidate in (
                field.get("resource_id"),
                field.get("related"),
                field.get("value"),
                field.get("value_id"),
            ):
                value = as_int(candidate)
                if value is not None:
                    return value

        return None

    # ==========================================
    # Shipments For One Business
    # ==========================================

    def get_business_shipments(
        self,
        business_id: Any,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Load one business shipments using the verified numeric Business filter.

        The old implementation used ``business.name`` even when a numeric ID was
        supplied. OPOST ignored that filter in this account and returned unrelated
        shipments. This method uses ``business=<id>`` and verifies every returned
        shipment before accepting the result.
        """
        business_text = str(business_id or "").strip()
        if not business_text:
            return []

        params: Dict[str, Any] = {"business": business_text, "limit": 5000}
        if start_date and end_date:
            params["created_at"] = f"{start_date} to {end_date}"
        resource = "shipments?" + urlencode(params)
        shipments = self.get_all(resource, concurrency=12)

        expected = int(float(business_text))
        foreign = []
        for shipment in shipments:
            if not isinstance(shipment, dict):
                continue
            found = self._extract_business_id(shipment)
            if found is None or found != expected:
                foreign.append(found)
                if len(foreign) >= 3:
                    break
        if foreign:
            raise RuntimeError(
                "OPOST تجاهل فلتر Business ID وأعاد شحنات لحسابات أخرى. "
                "تم رفض النتيجة لحماية الدقة."
            )
        return [item for item in shipments if isinstance(item, dict)]

    # ==========================================
    # Aggregated Period Shipment Statistics
    # ==========================================

    def get_shipment_statistics_grouped_by_business(
        self,
        start_date: str,
        end_date: str,
        business_ids: Optional[Iterable[int]] = None,
        concurrency: int = 18,
        progress_callback=None,
    ) -> Dict[str, Dict[str, Any]]:
        """Download each OPOST page once and aggregate it immediately.

        Version 2.3.2 removes two expensive barriers from the old pipeline:
        all missing pages are queued in one executor (a single slow page no
        longer blocks the next wave), and the disk cache stores compact page
        statistics rather than full shipment JSON payloads.
        """
        from business_service import shipment_status
        import hashlib

        allowed = {str(int(v)) for v in (business_ids or []) if v is not None}
        params = {"created_at": f"{start_date} to {end_date}", "limit": 5000}
        resource = "shipments?" + urlencode(params)
        page_cache_root = _PROJECT_DIR / "cache" / "shipment_page_stats"
        page_cache_key = hashlib.sha256(
            f"{start_date}|{end_date}|limit=5000|2.3.2-compact".encode("utf-8")
        ).hexdigest()
        page_cache_dir = page_cache_root / page_cache_key
        page_cache_dir.mkdir(parents=True, exist_ok=True)
        page_cache_ttl = 24 * 60 * 60

        def blank():
            return {"__aggregated__": True, "total": 0, "closed": 0,
                    "delivered": 0, "returned": 0, "cancelled": 0,
                    "unknown": 0, "status_counts": {}}

        result = {key: blank() for key in allowed}
        unmatched = 0

        def aggregate_records(records: Any) -> Dict[str, Any]:
            page_result: Dict[str, Dict[str, Any]] = {}
            page_unmatched = 0
            for shipment in list(records or []):
                if not isinstance(shipment, dict):
                    continue
                business_id = self._extract_business_id(shipment)
                if business_id is None:
                    page_unmatched += 1
                    continue
                key = str(business_id)
                if allowed and key not in allowed:
                    continue
                stats = page_result.setdefault(key, blank())
                status = str(shipment_status(shipment) or "unknown").strip().lower()
                stats["total"] += 1
                stats["status_counts"][status] = stats["status_counts"].get(status, 0) + 1
                if "closed" in status or "مغلق" in status or "إغلاق" in status:
                    stats["closed"] += 1
                elif "delivered" in status or "تسليم" in status or "توصيل" in status:
                    stats["delivered"] += 1
                elif "return" in status or "راجع" in status or "إرجاع" in status:
                    stats["returned"] += 1
                elif "cancel" in status or "ملغ" in status:
                    stats["cancelled"] += 1
                else:
                    stats["unknown"] += 1
            return {"businesses": page_result, "unmatched": page_unmatched}

        def merge_summary(summary: Dict[str, Any]) -> None:
            nonlocal unmatched
            unmatched += int(summary.get("unmatched", 0) or 0)
            for key, incoming in (summary.get("businesses", {}) or {}).items():
                target = result.setdefault(str(key), blank())
                for field in ("total", "closed", "delivered", "returned", "cancelled", "unknown"):
                    target[field] += int(incoming.get(field, 0) or 0)
                for status, count in (incoming.get("status_counts", {}) or {}).items():
                    target["status_counts"][status] = target["status_counts"].get(status, 0) + int(count or 0)

        def cache_path(page_number: int) -> Path:
            return page_cache_dir / f"page-{page_number}.json"

        def read_cached_page(page_number: int):
            path = cache_path(page_number)
            try:
                if not path.exists() or time.time() - path.stat().st_mtime > page_cache_ttl:
                    return None
                payload = json.loads(path.read_text(encoding="utf-8"))
                return payload if isinstance(payload, dict) and "businesses" in payload else None
            except (OSError, json.JSONDecodeError):
                return None

        def write_cached_page(page_number: int, summary: Dict[str, Any]) -> None:
            path = cache_path(page_number)
            temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                temporary.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, path)
            except OSError:
                pass
            finally:
                try: temporary.unlink(missing_ok=True)
                except OSError: pass

        print(f"\n📥 Streaming {resource}")
        first_summary = read_cached_page(1)
        if first_summary is None:
            first_page = self.get_page(resource, 1)
            pagination = first_page.get("pagination", {}) or {}
            last_page = int(pagination.get("last_page", 1) or 1)
            first_summary = aggregate_records(first_page.get("data", []) or [])
            write_cached_page(1, first_summary)
            try:
                (page_cache_dir / "meta.json").write_text(
                    json.dumps({"last_page": last_page, "saved_at": time.time()}), encoding="utf-8")
            except OSError: pass
        else:
            try:
                meta = json.loads((page_cache_dir / "meta.json").read_text(encoding="utf-8"))
                last_page = int(meta.get("last_page", 1) or 1)
            except (OSError, json.JSONDecodeError, ValueError):
                first_page = self.get_page(resource, 1)
                last_page = int((first_page.get("pagination", {}) or {}).get("last_page", 1) or 1)

        merge_summary(first_summary)
        completed = 1
        if progress_callback is not None: progress_callback(completed, last_page)

        missing_pages = []
        for page_number in range(2, last_page + 1):
            summary = read_cached_page(page_number)
            if summary is None:
                missing_pages.append(page_number)
            else:
                merge_summary(summary)
                completed += 1

        if progress_callback is not None and completed > 1:
            progress_callback(completed, last_page)

        if missing_pages:
            concurrency = max(1, min(int(concurrency or 18), 18, len(missing_pages)))
            cookies = requests.utils.dict_from_cookiejar(self.http.cookies)
            headers = dict(self.http.headers)
            thread_local = threading.local()

            def thread_session() -> requests.Session:
                session = getattr(thread_local, "session", None)
                if session is None:
                    session = requests.Session()
                    session.headers.update(headers)
                    session.cookies.update(cookies)
                    adapter = HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=0, pool_block=True)
                    session.mount("https://", adapter); session.mount("http://", adapter)
                    thread_local.session = session
                return session

            def request_page(page_number: int, read_timeout: int):
                url = f"{self.origin}/en/w/resources/{self._with_page(resource, page_number)}"
                with _GLOBAL_OPOST_REQUESTS:
                    response = thread_session().get(url, timeout=(4, read_timeout), allow_redirects=False)
                if response.status_code in (301, 302, 303, 307, 308, 401, 403):
                    raise RuntimeError(f"OPOST session expired: HTTP {response.status_code}")
                response.raise_for_status()
                payload = self._normalize_payload(response.json(), f"{resource} page={page_number}")
                summary = aggregate_records(payload.get("data", []) or [])
                write_cached_page(page_number, summary)
                return page_number, summary

            failed = []
            # Queue every page at once. Executor concurrency still protects OPOST,
            # but a slow page cannot stop later pages from starting.
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {executor.submit(request_page, page, 20): page for page in missing_pages}
                for future in as_completed(futures):
                    page_number = futures[future]
                    try:
                        _page, summary = future.result()
                        merge_summary(summary); completed += 1
                        if progress_callback is not None and (completed == last_page or completed % 4 == 0):
                            progress_callback(completed, last_page)
                    except Exception as error:
                        failed.append((page_number, error))

            if failed:
                # One slower retry per failed page, after all fast pages finish.
                final_failures = []
                with ThreadPoolExecutor(max_workers=min(4, len(failed))) as executor:
                    futures = {executor.submit(request_page, page, 55): (page, original)
                               for page, original in failed}
                    for future in as_completed(futures):
                        page_number, original = futures[future]
                        try:
                            _page, summary = future.result()
                            merge_summary(summary); completed += 1
                            if progress_callback is not None:
                                progress_callback(completed, last_page)
                        except Exception as error:
                            final_failures.append((page_number, error, original))
                if final_failures:
                    page_number, error, original = final_failures[0]
                    raise RuntimeError(
                        f"Page {page_number} failed: {error}. Completed pages were saved; retry resumes from the missing page."
                    ) from original

        if unmatched:
            raise RuntimeError(f"تعذر ربط {unmatched} شحنة بحساب Business. تم إيقاف التقرير لحماية الدقة.")
        print(f"✅ Aggregated shipment statistics for {len(result)} businesses")
        return result

    # ==========================================
    # Compatibility Method
    # ==========================================

    def get_shipments_grouped_by_business(
        self,
        start_date: str,
        end_date: str,
        business_ids: Optional[
            Iterable[int]
        ] = None,
        progress_callback=None,
    ) -> Dict[
        str,
        List[Dict[str, Any]]
    ]:

        """
        هذه الدالة باقية للتوافق مع main.py القديم.

        ملاحظة:
        هذه الطريقة تقرأ شحنات الفترة كلها؛
        الإصدار 2.1.2 يحتفظ بهذه الدالة للتوافق فقط
        للفترة ثم يجمّع الشحنات محليًا حسب Business ID.
        """

        allowed_ids: Optional[
            Set[int]
        ] = None

        if business_ids is not None:

            allowed_ids = {
                int(business_id)

                for business_id
                in business_ids

                if business_id is not None
            }

        params = {

            "created_at": (
                f"{start_date} "
                f"to {end_date}"
            ),

            "limit":
                1000,
        }

        resource = (
            "shipments?"
            + urlencode(params)
        )

        shipments = self.get_all(
            resource,
            concurrency=24,
            progress_callback=progress_callback,
        )

        grouped: Dict[
            str,
            List[Dict[str, Any]]
        ] = defaultdict(list)

        matched = 0

        for shipment in shipments:

            business_id = (
                self._extract_business_id(
                    shipment
                )
            )

            if business_id is None:
                continue

            if (
                allowed_ids is not None
                and business_id
                not in allowed_ids
            ):
                continue

            grouped[
                str(business_id)
            ].append(
                shipment
            )

            matched += 1

        print(
            f"✅ Matched {matched} shipments "
            f"to {len(grouped)} businesses"
        )

        return dict(
            grouped
        )

    # ==========================================
    # Fast Shipments For Selected Businesses
    # ==========================================

    def get_all_business_shipments_fast(
        self,
        businesses: List[Dict[str, Any]],
        start_date: str,
        end_date: str,
        concurrency: int = 8,
        progress_callback=None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Load only selected businesses and verify the server-side filter.

        The method never trusts ``business.name`` blindly. Every returned
        shipment must contain the exact requested Business ID. If OPOST
        ignores the filter, returns foreign shipments, or a request fails,
        a RuntimeError is raised so the caller can safely fall back to the
        full-period strategy instead of producing false zeros.
        """
        if self.page is None:
            raise RuntimeError("Browser is not started.")

        selected = []
        for business in businesses or []:
            if not isinstance(business, dict):
                continue
            business_id = business.get("id")
            name = str(business.get("display") or business.get("name") or "").strip()
            try:
                business_id = int(business_id)
            except (TypeError, ValueError):
                continue
            if name:
                selected.append({"id": business_id, "name": name})

        if not selected:
            return {}

        concurrency = max(1, min(int(concurrency or 8), 12))
        chunk_size = max(concurrency, concurrency * 2)
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        verified_positive = False
        completed = 0

        print("\n" + "=" * 60)
        print("TRYING VERIFIED SELECTED-BUSINESS SHIPMENT FILTER")
        print("Businesses:", len(selected))
        print("Concurrency:", concurrency)
        print("=" * 60)

        for offset in range(0, len(selected), chunk_size):
            chunk = selected[offset:offset + chunk_size]
            result = self.page.evaluate(
                """
                async (args) => {
                    const {businesses, startDate, endDate, concurrency, origin} = args;
                    const normalize = value => Array.isArray(value) ? (value[0] || {}) : (value || {});
                    const extractBusinessId = shipment => {
                        if (shipment && shipment.business_id != null) return Number(shipment.business_id);
                        if (shipment && shipment.business && shipment.business.id != null) return Number(shipment.business.id);
                        for (const field of ((shipment && shipment.fields) || [])) {
                            if (!field) continue;
                            const attr = field.attribute || field.related_name || '';
                            if (!['business.name','business','business_id','sender','merchant'].includes(attr)) continue;
                            const value = field.value;
                            const candidates = [
                                field.resource_id,
                                field.related && field.related.id,
                                value && typeof value === 'object' ? value.id : null,
                                value && typeof value === 'object' ? value.resource_id : null,
                            ];
                            for (const candidate of candidates) {
                                if (candidate !== undefined && candidate !== null && candidate !== '') {
                                    const numberValue = Number(candidate);
                                    if (Number.isFinite(numberValue)) return numberValue;
                                }
                            }
                        }
                        return null;
                    };
                    async function fetchJson(url) {
                        let lastError = null;
                        for (let attempt=1; attempt<=3; attempt++) {
                            const controller = new AbortController();
                            const timer = setTimeout(() => controller.abort(), 20000);
                            try {
                                const response = await fetch(url, {
                                    credentials:'include', signal:controller.signal,
                                    headers:{'Accept':'application/json, text/plain, */*','X-Requested-With':'XMLHttpRequest','App-Language':'en'}
                                });
                                clearTimeout(timer);
                                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                                return await response.json();
                            } catch (error) {
                                clearTimeout(timer); lastError = error;
                                if (attempt < 2) await new Promise(r => setTimeout(r, 400 * attempt));
                            }
                        }
                        throw lastError || new Error('Request failed');
                    }
                    async function loadBusiness(business) {
                        const params = new URLSearchParams();
                        params.set('business.name', business.name);
                        params.set('created_at', `${startDate} to ${endDate}`);
                        params.set('limit', '1000');
                        const base = `${origin}/en/w/resources/shipments?${params.toString()}`;
                        const first = normalize(await fetchJson(`${base}&page=1`));
                        const lastPage = Number((first.pagination || {}).last_page || 1);
                        let raw = Array.from(first.data || []);
                        for (let page=2; page<=lastPage; page++) {
                            const payload = normalize(await fetchJson(`${base}&page=${page}`));
                            raw.push(...Array.from(payload.data || []));
                        }
                        const matched = [];
                        let foreign = 0;
                        let unknown = 0;
                        for (const shipment of raw) {
                            const id = extractBusinessId(shipment);
                            if (id === business.id) matched.push(shipment);
                            else if (id == null) unknown++;
                            else foreign++;
                        }
                        return {id:String(business.id), name:business.name, rawCount:raw.length, matched, foreign, unknown, lastPage};
                    }
                    const output = new Array(businesses.length);
                    let next = 0;
                    async function worker() {
                        while (true) {
                            const i = next++;
                            if (i >= businesses.length) return;
                            try { output[i] = await loadBusiness(businesses[i]); }
                            catch (error) { output[i] = {id:String(businesses[i].id), name:businesses[i].name, error:String(error)}; }
                        }
                    }
                    await Promise.all(Array.from({length:Math.min(concurrency,businesses.length)}, () => worker()));
                    return output;
                }
                """,
                {"businesses": chunk, "startDate": start_date, "endDate": end_date, "concurrency": concurrency, "origin": self.origin},
            )

            for item in result or []:
                if not isinstance(item, dict):
                    raise RuntimeError("Invalid selected-business shipment response.")
                if item.get("error"):
                    raise RuntimeError(
                        f"Selected-business filter request failed for {item.get('name','')} ({item.get('id','')}): {item.get('error')}"
                    )
                raw_count = int(item.get("rawCount") or 0)
                foreign = int(item.get("foreign") or 0)
                unknown = int(item.get("unknown") or 0)
                matched = item.get("matched") or []
                if foreign > 0:
                    raise RuntimeError(
                        "OPOST ignored business.name filter: foreign Business IDs were returned."
                    )
                if raw_count > 0 and unknown > 0:
                    raise RuntimeError(
                        "OPOST returned shipments whose Business ID could not be verified."
                    )
                if raw_count > 0 and len(matched) == 0:
                    raise RuntimeError(
                        "OPOST business filter could not be verified for returned shipments."
                    )
                if matched:
                    verified_positive = True
                grouped[str(item.get("id") or "").strip()] = list(matched)

            completed += len(chunk)
            if progress_callback is not None:
                progress_callback(completed, len(selected))

        if not verified_positive:
            raise RuntimeError(
                "The selected-business filter returned no verifiable shipment sample; using full-period fallback."
            )

        print(f"✅ Verified selected-business filter for {len(selected)} businesses")
        print(f"✅ Matched {sum(len(v) for v in grouped.values())} shipments")
        return grouped

    # ==========================================
    # Close Browser
    # ==========================================

    def close(self):
        """Release HTTP and optional Playwright resources safely."""
        page = self.page
        context = self.context
        browser = self.browser
        playwright_instance = self.playwright

        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None

        try:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
        finally:
            if playwright_instance is not None:
                try:
                    playwright_instance.stop()
                except Exception:
                    pass
            try:
                self.http.close()
            except Exception:
                pass


# ==========================================
# OPOST Business Account-Manager Operations
# ==========================================

def _normalized_manager_text(value: Any) -> str:
    import re
    text = str(value or "").strip().casefold()
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", text)


def _field_key(field: Dict[str, Any]) -> str:
    return str(field.get("attribute") or field.get("related_name") or field.get("name") or "").strip()


def _field_display(field: Dict[str, Any]) -> str:
    """Return the human-readable relation label shown by OPOST/Nova.

    OPOST deployments use several keys for belongs-to fields.  The numeric
    relation id must never be exposed as the visible manager label.
    """
    if not isinstance(field, dict):
        return ""

    # Laravel Nova and OPOST variants that carry the visible relation name.
    direct_keys = (
        "displayedAs", "displayed_as", "belongsToDisplay",
        "belongs_to_display", "value_label", "display",
        "label_value", "resourceLabel", "resource_label",
    )
    nested_keys = (
        "displayedAs", "displayed_as", "display", "label",
        "name", "full_name", "title", "text", "value",
    )

    def clean(value: Any) -> str:
        text = str(value or "").strip()
        # A pure number is the relation ID, not the name shown in OPOST.
        return "" if (not text or text.isdigit()) else text

    for key in direct_keys:
        value = field.get(key)
        if isinstance(value, dict):
            for subkey in nested_keys:
                text = clean(value.get(subkey))
                if text:
                    return text
        elif isinstance(value, (list, tuple)):
            for entry in value:
                if isinstance(entry, dict):
                    for subkey in nested_keys:
                        text = clean(entry.get(subkey))
                        if text:
                            return text
                else:
                    text = clean(entry)
                    if text:
                        return text
        else:
            text = clean(value)
            if text:
                return text

    value = field.get("value")
    if isinstance(value, dict):
        for subkey in nested_keys:
            text = clean(value.get(subkey))
            if text:
                return text
    elif isinstance(value, (list, tuple)):
        for entry in value:
            if isinstance(entry, dict):
                for subkey in nested_keys:
                    text = clean(entry.get(subkey))
                    if text:
                        return text
            else:
                text = clean(entry)
                if text:
                    return text
    else:
        text = clean(value)
        if text:
            return text
    return ""


def _field_relation_id(field: Dict[str, Any]) -> Optional[str]:
    candidates = [
        field.get("resource_id"), field.get("value_id"), field.get("related"), field.get("value")
    ]
    for value in candidates:
        if isinstance(value, dict):
            for key in ("id", "resource_id", "value"):
                nested = value.get(key)
                if nested not in (None, "") and str(nested).strip().isdigit():
                    return str(nested).strip()
        elif value not in (None, "") and str(value).strip().isdigit():
            return str(value).strip()
    return None


def _payload_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict) and "data" in payload[0]:
            payload = payload[0]
        else:
            return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            return [data]
        if payload.get("id") is not None:
            return [payload]
    return []


def _client_authenticated_request(
    client: "OpostClient",
    method: str,
    url: str,
    *,
    data: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout: tuple[int, int] = (5, 25),
) -> requests.Response:
    """Send one authenticated modifying request and refresh the saved login once if needed."""
    from urllib.parse import unquote

    def perform() -> requests.Response:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{client.origin}/resources/businesses",
        }
        xsrf = client.http.cookies.get("XSRF-TOKEN")
        if xsrf:
            headers["X-XSRF-TOKEN"] = unquote(str(xsrf))
        return client.http.request(
            method.upper(), url, data=data, json=json_body, headers=headers,
            timeout=timeout, allow_redirects=False,
        )

    response = perform()
    if response.status_code in (301, 302, 303, 307, 308, 401, 403, 419):
        global _AUTH_OK_UNTIL
        with _AUTH_CACHE_LOCK:
            _AUTH_OK_UNTIL = 0.0
        client.login(force_check=True)
        client._load_http_cookies()
        response = perform()
    return response


def _manager_field_from_business(business: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for field in business.get("fields", []) or []:
        if not isinstance(field, dict):
            continue
        key = _field_key(field).casefold().replace("-", "_").replace(".", "_")
        label = str(field.get("label") or field.get("name") or "").casefold()
        if "account_manager" in key or "account manager" in label or "مدير الحساب" in label:
            return field
    return None


def _business_manager_display(business: Dict[str, Any]) -> str:
    field = _manager_field_from_business(business)
    return _field_display(field) if field else ""


def _business_manager_id(business: Dict[str, Any]) -> Optional[str]:
    field = _manager_field_from_business(business)
    return _field_relation_id(field) if field else None


def _business_visible_value(business: Dict[str, Any], aliases: Iterable[str]) -> str:
    """Read a visible business value from top-level data or Nova fields.

    List endpoints in the two OPOST frontends do not always return the same
    field shape. This helper accepts attribute/name/label aliases and avoids
    showing numeric relation IDs as labels.
    """
    wanted = {
        str(alias or "").strip().casefold().replace("-", "_").replace(".", "_").replace(" ", "_")
        for alias in aliases
        if str(alias or "").strip()
    }

    def norm(value: Any) -> str:
        return str(value or "").strip().casefold().replace("-", "_").replace(".", "_").replace(" ", "_")

    def visible(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("displayedAs", "displayed_as", "display", "label", "name", "title", "text", "value"):
                text = visible(value.get(key))
                if text:
                    return text
            return ""
        if isinstance(value, (list, tuple)):
            for item in value:
                text = visible(item)
                if text:
                    return text
            return ""
        text = str(value or "").strip()
        return "" if (not text or text.isdigit()) else text

    for key, value in business.items():
        if norm(key) in wanted:
            text = visible(value)
            if text:
                return text

    for field in business.get("fields", []) or []:
        if not isinstance(field, dict):
            continue
        candidates = (
            _field_key(field),
            field.get("attribute"),
            field.get("related_name"),
            field.get("name"),
            field.get("label"),
        )
        if any(norm(candidate) in wanted for candidate in candidates):
            text = _field_display(field)
            if text:
                return text
            text = visible(field.get("value"))
            if text:
                return text
    return ""


def _get_business_detail_for_update(client: "OpostClient", business_id: str) -> Dict[str, Any]:
    payload = client._request(f"businesses/{business_id}")
    items = _payload_items(payload)
    for item in items:
        if str(item.get("id") or "").strip() == str(business_id):
            return item
    if items:
        return items[0]
    raise RuntimeError(f"لم يتم العثور على الحساب {business_id} في OPOST.")


def _resolve_user_relation_id(client: "OpostClient", target_label: str) -> str:
    """Resolve the OPOST user relation once, using exact normalized display matching."""
    target_norm = _normalized_manager_text(target_label)
    resources = (
        "users?limit=5000",
        "users?search=" + quote(target_label) + "&limit=100",
    )
    candidates: List[Dict[str, Any]] = []
    for resource in resources:
        try:
            candidates.extend(client.get_all(resource, concurrency=8))
        except Exception:
            continue
    exact: List[str] = []
    contains: List[str] = []
    for item in candidates:
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            continue
        display = str(item.get("display") or item.get("name") or "").strip()
        if not display:
            for field in item.get("fields", []) or []:
                if isinstance(field, dict) and _field_key(field).casefold() in {"name", "full_name", "display"}:
                    display = _field_display(field)
                    if display:
                        break
        norm = _normalized_manager_text(display)
        if norm == target_norm:
            exact.append(item_id)
        elif target_norm in norm or norm in target_norm:
            contains.append(item_id)
    unique = list(dict.fromkeys(exact or contains))
    if unique:
        # OPOST may expose the same assignable Account Manager more than once
        # with different internal relation IDs (for example Subscriber Service).
        # The original UI accepts any of these duplicate choices, so select the
        # first exact match instead of blocking the whole bulk transfer.
        return unique[0]
    raise RuntimeError(f"تعذر العثور على المستخدم الهدف داخل OPOST: {target_label}")


def _update_business_manager_once(
    client: "OpostClient",
    business_id: str,
    target_user_id: str,
    manager_attribute: str,
) -> None:
    """Try the known Laravel-Nova update shapes used by OPOST, then verify separately."""
    resource_url = f"{client.origin}/en/w/resources/businesses/{business_id}"
    attr = manager_attribute or "account_manager"
    payload_variants = [
        {"_method": "PUT", attr: target_user_id},
        {"_method": "PUT", "account_manager": target_user_id},
        {"_method": "PUT", "account_manager_id": target_user_id},
    ]
    attempted = []
    for payload in payload_variants:
        signature = tuple(sorted(payload.items()))
        if signature in attempted:
            continue
        attempted.append(signature)
        response = _client_authenticated_request(client, "POST", resource_url, data=payload)
        if response.status_code in (200, 201, 202, 204):
            return
        # Some OPOST deployments expose a conventional REST PUT endpoint.
        response = _client_authenticated_request(client, "PUT", resource_url, data={k: v for k, v in payload.items() if k != "_method"})
        if response.status_code in (200, 201, 202, 204):
            return
    raise RuntimeError("رفض OPOST طلب تغيير مدير الحساب. لم يتم تعديل الحساب.")


def opost_find_transfer_candidates(
    client: "OpostClient",
    start_date: str,
    end_date: str,
    source_manager: str,
) -> List[Dict[str, str]]:
    source_norm = _normalized_manager_text(source_manager)
    # OPOST exposes the source manager as relation ID 15122. Filtering server-side
    # removes unnecessary pages while the exact display-name check below remains
    # the final safety guard.
    params = urlencode({
        "account_manager": "15122",
        "created_at": f"{start_date} to {end_date}",
        "limit": 5000,
    })
    businesses = client.get_all(f"businesses?{params}", concurrency=12)
    result: List[Dict[str, str]] = []
    for business in businesses:
        current = _business_manager_display(business)
        if _normalized_manager_text(current) != source_norm:
            continue
        result.append({
            "id": str(business.get("id") or ""),
            "name": str(business.get("display") or business.get("name") or "").strip(),
            "created_at": str(next((f.get("value") for f in business.get("fields", []) or [] if isinstance(f, dict) and _field_key(f).casefold() == "created_at"), "") or ""),
            "office": str(next((_field_display(f) for f in business.get("fields", []) or [] if isinstance(f, dict) and _field_key(f).casefold() == "office"), "") or ""),
            "account_manager": current,
        })
    result.sort(key=lambda item: (item.get("created_at", ""), item.get("id", "")))
    return result


def opost_bulk_transfer_account_manager(
    business_ids: Iterable[str],
    source_manager: str,
    target_manager: str,
    max_workers: int = 6,
) -> Dict[str, Any]:
    """Change the account manager in OPOST itself, verifying every successful write."""
    ids = list(dict.fromkeys(str(x).strip() for x in business_ids if str(x).strip()))
    if not ids:
        return {"success": [], "failed": []}

    bootstrap = OpostClient()
    bootstrap.start()
    bootstrap.login()
    target_user_id = _resolve_user_relation_id(bootstrap, target_manager)
    bootstrap.close()

    source_norm = _normalized_manager_text(source_manager)
    target_norm = _normalized_manager_text(target_manager)

    def transfer_one(business_id: str) -> Dict[str, str]:
        client = OpostClient()
        try:
            client.start()
            client.login()
            before = _get_business_detail_for_update(client, business_id)
            current = _business_manager_display(before)
            if _normalized_manager_text(current) == target_norm:
                return {"id": business_id, "status": "success", "message": "الحساب محول مسبقًا."}
            if _normalized_manager_text(current) != source_norm:
                return {"id": business_id, "status": "failed", "message": f"مدير الحساب الحالي مختلف: {current or 'غير معروف'}"}
            field = _manager_field_from_business(before)
            attribute = _field_key(field) if field else "account_manager"
            _update_business_manager_once(client, business_id, target_user_id, attribute)
            # Verify from OPOST after the write. This prevents false success messages.
            after = _get_business_detail_for_update(client, business_id)
            updated = _business_manager_display(after)
            updated_id = _business_manager_id(after)
            if _normalized_manager_text(updated) == target_norm or str(updated_id or "") == str(target_user_id):
                return {"id": business_id, "status": "success", "message": "تم التحويل بنجاح."}
            return {"id": business_id, "status": "failed", "message": "أرسل OPOST الطلب لكن التحقق لم يؤكد التغيير."}
        except Exception as exc:
            return {"id": business_id, "status": "failed", "message": str(exc)}
        finally:
            client.close()

    results: List[Dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), 8))) as executor:
        futures = [executor.submit(transfer_one, business_id) for business_id in ids]
        for future in as_completed(futures):
            results.append(future.result())
    return {
        "success": [r for r in results if r["status"] == "success"],
        "failed": [r for r in results if r["status"] != "success"],
    }

# Bind helpers as public methods without changing the stable class body above.
OpostClient.find_transfer_candidates = lambda self, start_date, end_date, source_manager: opost_find_transfer_candidates(self, start_date, end_date, source_manager)

# ===== 2.3.9: dynamic account-manager transfer =====
def opost_list_account_managers(client: "OpostClient") -> List[Dict[str, str]]:
    """Return the exact assignable Account Manager choices exposed by OPOST.

    The original OPOST Account Manager filter is a searchable relation control.
    Reading arbitrary users or business rows mixes ordinary users with managers,
    so this function first queries the relation/filter option endpoints that feed
    that control.  Only if OPOST changes those endpoints do we fall back to
    managers currently attached to businesses.
    """

    def clean_label(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("label", "name", "display", "displayedAs", "displayed_as", "text", "title", "value"):
                text = clean_label(value.get(key))
                if text:
                    return text
            return ""
        text = str(value or "").strip()
        return "" if not text or text.isdigit() else text

    def add_choice(output: List[Dict[str, str]], seen_ids: Set[str], seen_names: Set[str], item_id: Any, name: Any) -> None:
        manager_id = str(item_id or "").strip()
        manager_name = clean_label(name)
        name_key = _normalized_manager_text(manager_name)
        if not manager_id or not manager_name or manager_id in seen_ids or name_key in seen_names:
            return
        seen_ids.add(manager_id)
        seen_names.add(name_key)
        output.append({"id": manager_id, "name": manager_name})

    def parse_options(payload: Any) -> List[Dict[str, str]]:
        output: List[Dict[str, str]] = []
        seen_ids: Set[str] = set()
        seen_names: Set[str] = set()

        def walk(node: Any) -> None:
            if isinstance(node, list):
                for entry in node:
                    walk(entry)
                return
            if not isinstance(node, dict):
                return

            # Common Nova / Vue relation option shapes.
            candidate_id = (
                node.get("id") or node.get("value") or node.get("resourceId") or
                node.get("resource_id") or node.get("key")
            )
            candidate_name = (
                node.get("display") or node.get("displayedAs") or node.get("displayed_as") or
                node.get("label") or node.get("name") or node.get("text") or node.get("title")
            )
            if isinstance(candidate_id, (str, int)) and candidate_name is not None:
                add_choice(output, seen_ids, seen_names, candidate_id, candidate_name)

            for key, value in node.items():
                if key in {"fields"}:
                    continue
                if isinstance(value, (dict, list)):
                    walk(value)

        walk(payload)
        output.sort(key=lambda row: row["name"].casefold())
        return output

    SERVICE_MANAGER_NAME = "خدمة المشتركين - 0568823212"
    SERVICE_MANAGER_ALIASES = (
        SERVICE_MANAGER_NAME,
        "خدمة المشتركين- 0568823212",
        "خدمة المشتركين -0568823212",
        "خدمة المشتركين 0568823212",
    )

    def ensure_priority_manager(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
        normalized = {_normalized_manager_text(row.get("name", "")) for row in items}
        if not any(_normalized_manager_text(alias) in normalized for alias in SERVICE_MANAGER_ALIASES):
            # The exact relation ID is resolved once at transfer time. This keeps
            # the required OPOST manager selectable even when its virtualized
            # dropdown row was not mounted while the list was scraped.
            items.insert(0, {"id": "resolve:customer-service", "name": SERVICE_MANAGER_NAME})
        else:
            items.sort(key=lambda row: (0 if "خدمة المشتركين" in row.get("name", "") else 1, row.get("name", "").casefold()))
        return items

    errors: List[str] = []

    def load_from_original_dropdown() -> List[Dict[str, str]]:
        """Read the exact Account Manager dropdown used by the original OPOST UI."""
        captured: List[Any] = []
        page = None
        remember_response = None
        try:
            client._launch_login_browser()
            page = client.page

            def _remember_response(response):
                try:
                    url = response.url.lower()
                    ctype = (response.headers.get("content-type") or "").lower()
                    if "json" not in ctype:
                        return
                    if not any(token in url for token in ("account", "manager", "relatable", "user", "filter")):
                        return
                    payload = response.json()
                    if payload is not None:
                        captured.append(payload)
                except Exception:
                    return

            remember_response = _remember_response
            page.on("response", remember_response)
            page.goto(
                client.origin.rstrip("/") + "/en/logistics/resources/businesses?page=1",
                wait_until="domcontentloaded", timeout=35000,
            )
            page.wait_for_timeout(1200)

            labels = page.get_by_text("Account Manager", exact=True)
            opened = False
            for index in range(labels.count() - 1, -1, -1):
                try:
                    labels.nth(index).click(timeout=2500)
                    opened = True
                    break
                except Exception:
                    continue
            if not opened:
                candidates = page.locator(
                    'button:has-text("Account Manager"), [role="button"]:has-text("Account Manager"), th:has-text("Account Manager")'
                )
                for index in range(candidates.count() - 1, -1, -1):
                    try:
                        candidates.nth(index).click(timeout=2500)
                        opened = True
                        break
                    except Exception:
                        continue
            if not opened:
                raise RuntimeError("Account Manager filter was not found in OPOST")

            page.wait_for_timeout(700)
            search = page.locator(
                'input[placeholder*="Select User" i], input[placeholder*="Search" i], [role="combobox"] input'
            ).last
            if search.count():
                try:
                    search.click(timeout=2000)
                    search.fill("")
                    search.press("ArrowDown")
                except Exception:
                    pass
            page.wait_for_timeout(1600)

            for _ in range(18):
                page.evaluate("""() => {
                    const candidates = [...document.querySelectorAll('[role=\"listbox\"], .v-list, .multiselect__content-wrapper, .dropdown-menu, [class*=\"menu\"]')];
                    for (const el of candidates) {
                        if (el.scrollHeight > el.clientHeight + 8) el.scrollTop = el.scrollHeight;
                    }
                }""")
                page.wait_for_timeout(180)

            for payload in captured:
                choices = parse_options(payload)
                if len(choices) >= 3:
                    return choices

            raw = page.evaluate("""() => {
                const selectors = ['[role=\"option\"]', '.v-list-item', '.multiselect__option', 'li'];
                const out = [];
                const seen = new Set();
                for (const selector of selectors) {
                    for (const el of document.querySelectorAll(selector)) {
                        const name = (el.innerText || el.textContent || '').trim();
                        if (!name || name.length > 180 || seen.has(name)) continue;
                        const id = el.getAttribute('data-value') || el.getAttribute('data-id') ||
                                   el.getAttribute('value') || '';
                        if (id) { out.push({id, name}); seen.add(name); }
                    }
                }
                return out;
            }""")
            output: List[Dict[str, str]] = []
            seen_ids: Set[str] = set()
            seen_names: Set[str] = set()
            for row in raw or []:
                add_choice(output, seen_ids, seen_names, row.get("id"), row.get("name"))
            if len(output) >= 3:
                output.sort(key=lambda row: row["name"].casefold())
                return output
            raise RuntimeError("OPOST dropdown opened but returned no assignable managers")
        finally:
            if page is not None and remember_response is not None:
                try:
                    page.remove_listener("response", remember_response)
                except Exception:
                    pass

    try:
        exact_choices = load_from_original_dropdown()
        if len(exact_choices) >= 3:
            return ensure_priority_manager(exact_choices)
    except Exception as exc:
        errors.append(f"original dropdown: {exc}")

    base = client.origin.rstrip("/")
    # Endpoints used by Nova-like searchable relation/filter controls across the
    # two OPOST frontends.  The first successful non-empty response is the same
    # list shown in OPOST's Account Manager dropdown.
    candidates = [
        "/en/w/resources/businesses/relatable/users?field=account_manager&search=&first=false&withTrashed=false",
        "/en/w/resources/businesses/relatable/account-manager?field=account_manager&search=&first=false&withTrashed=false",
        "/en/w/resources/businesses/filters/account_manager/options",
        "/en/w/resources/businesses/filters?filter=account_manager",
        "/nova-api/businesses/relatable/users?field=account_manager&search=&first=false&withTrashed=false",
        "/nova-api/businesses/relatable/account-manager?field=account_manager&search=&first=false&withTrashed=false",
    ]
    for path in candidates:
        try:
            response = client.http.get(base + path, timeout=(4, 12), allow_redirects=False)
            if response.status_code in (301, 302, 303, 307, 308, 401, 403, 419):
                client.login(force_check=True)
                client._load_http_cookies()
                response = client.http.get(base + path, timeout=(4, 12), allow_redirects=False)
            if not response.ok or "json" not in response.headers.get("content-type", "").lower():
                errors.append(f"{path}: HTTP {response.status_code}")
                continue
            choices = parse_options(response.json())
            # A valid manager relation normally contains more than a couple of choices.
            if len(choices) >= 3:
                return ensure_priority_manager(choices)
        except Exception as exc:
            errors.append(f"{path}: {exc}")

    # Fallback: collect only values from the actual Account Manager relation in
    # business rows.  Never use the general Users resource because it contains
    # non-manager accounts and was the source of the mixed list.
    output: List[Dict[str, str]] = []
    seen_ids: Set[str] = set()
    seen_names: Set[str] = set()
    try:
        # Several pages increase coverage while remaining much cheaper than the
        # complete 4,700-business archive.
        for page_number in range(1, 9):
            page = client.get_page("businesses?limit=100", page_number)
            rows = page.get("data", []) or []
            for business in rows:
                if not isinstance(business, dict):
                    continue
                add_choice(
                    output, seen_ids, seen_names,
                    _business_manager_id(business), _business_manager_display(business),
                )
            pagination = page.get("pagination", {}) or {}
            if page_number >= int(pagination.get("last_page", page_number) or page_number):
                break
        if output:
            output.sort(key=lambda row: row["name"].casefold())
            return ensure_priority_manager(output)
    except Exception as exc:
        errors.append(f"business relation fallback: {exc}")

    raise RuntimeError("تعذر تحميل قائمة مدراء الحساب من OPOST. " + " | ".join(errors[-4:]))

def opost_find_manager_candidates(
    client: "OpostClient",
    start_date: str,
    end_date: str,
    source_manager_id: str,
    source_manager_label: str = "",
) -> List[Dict[str, str]]:
    """Return businesses assigned to one manager inside the selected period.

    OPOST has more than one frontend/API encoding for the same filters.  This
    implementation tries the precise server-side filter first, then falls back
    to one-filter-at-a-time requests and applies the remaining condition
    locally.  An empty result is a valid result and must not be treated as an
    error.
    """
    from datetime import datetime as _dt

    source_id = str(source_manager_id or "").strip()
    source_label = str(source_manager_label or "").strip()
    source_norm = _normalized_manager_text(source_label)
    if not source_id:
        return []

    def parse_day(value: Any):
        text = str(value or "").strip()
        if not text:
            return None
        text = text.replace("T", " ").split(".", 1)[0].replace("Z", "").strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
            try:
                return _dt.strptime(text[:19] if "%H" in fmt else text[:10], fmt).date()
            except ValueError:
                continue
        # OPOST sometimes prefixes/suffixes the visible value; keep the first ISO date.
        import re
        match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if match:
            try:
                return _dt.strptime(match.group(1), "%Y-%m-%d").date()
            except ValueError:
                pass
        return None

    start_day = parse_day(start_date)
    end_day = parse_day(end_date)

    def date_matches(business: Dict[str, Any]) -> bool:
        if start_day is None or end_day is None:
            return True
        raw = _business_visible_value(
            business, ("created_at", "created at", "تاريخ الإنشاء", "تاريخ الانشاء")
        ) or str(business.get("created_at") or "").strip()
        day = parse_day(raw)
        return bool(day and start_day <= day <= end_day)

    def manager_matches(business: Dict[str, Any]) -> bool:
        current_id = str(_business_manager_id(business) or "").strip()
        current_display = _business_manager_display(business)
        return bool(
            (current_id and current_id == source_id)
            or (source_norm and _normalized_manager_text(current_display) == source_norm)
        )

    date_value = f"{start_date} to {end_date}"
    attempts = [
        {"account_manager": source_id, "created_at": date_value, "limit": 5000},
        {"account_manager": source_id, "limit": 5000},
        {"created_at": date_value, "limit": 5000},
    ]
    businesses: List[Dict[str, Any]] = []
    errors: List[str] = []
    used_params: Dict[str, Any] = {}

    for params in attempts:
        try:
            loaded = client.get_all("businesses?" + urlencode(params), concurrency=10)
            if loaded:
                businesses = [item for item in loaded if isinstance(item, dict)]
                used_params = params
                break
            # A precise filtered query returning zero rows means there are no accounts.
            if "account_manager" in params and "created_at" in params:
                return []
        except Exception as exc:
            errors.append(str(exc))
            continue

    if not businesses:
        if errors:
            raise RuntimeError("تعذر جلب حسابات OPOST: " + " | ".join(errors[-2:]))
        return []

    result: List[Dict[str, str]] = []
    server_filtered_manager = "account_manager" in used_params
    server_filtered_date = "created_at" in used_params

    for business in businesses:
        if not server_filtered_manager and not manager_matches(business):
            continue
        if not server_filtered_date and not date_matches(business):
            continue
        # Even when OPOST accepted the filters, verify relation/date locally when
        # the response exposes enough data. This prevents false positives while
        # still accepting list payloads that omit the visible manager relation.
        if server_filtered_manager:
            current_id = str(_business_manager_id(business) or "").strip()
            current_display = _business_manager_display(business)
            if current_id and current_id != source_id:
                continue
            if not current_id and current_display and source_norm and _normalized_manager_text(current_display) != source_norm:
                continue
        if server_filtered_date and not date_matches(business):
            continue

        current_id = str(_business_manager_id(business) or "").strip()
        current_display = _business_manager_display(business)
        created_at = _business_visible_value(
            business, ("created_at", "created at", "تاريخ الإنشاء", "تاريخ الانشاء")
        ) or str(business.get("created_at") or "").strip()
        office = _business_visible_value(
            business, ("office", "office_id", "office_name", "المكتب")
        )
        result.append({
            "id": str(business.get("id") or ""),
            "name": str(business.get("display") or business.get("name") or "").strip(),
            "created_at": created_at,
            "office": office,
            "account_manager": current_display or source_label,
            "account_manager_id": current_id or source_id,
        })

    result.sort(key=lambda item: (item.get("created_at", ""), item.get("id", "")))
    return result

def _ui_change_account_manager_batch_worker(
    business_ids: Iterable[str],
    target_manager_id: str,
    target_manager_label: str,
) -> Dict[str, Any]:
    """Change managers through the exact original OPOST edit workflow.

    OPOST exposes duplicate relation choices for some managers (notably
    Subscriber Service).  This implementation searches the business by its
    real name, expands the matching row, opens Edit, and retries each duplicate
    visible manager option until OPOST accepts the save.
    """
    ids = list(dict.fromkeys(str(x).strip() for x in business_ids if str(x).strip()))
    output: List[Dict[str, str]] = []
    if not ids:
        return {"success": [], "failed": []}

    target_manager_id = str(target_manager_id or "").strip()
    target_manager_label = str(target_manager_label or "").strip()
    if not target_manager_label:
        raise RuntimeError("اسم المدير الجديد غير متوفر.")

    client = OpostClient()
    try:
        client.start()
        client.login()
        client._launch_login_browser()
        page = client.page
        page.set_default_timeout(9000)
        base = client.origin.rstrip("/")

        # Transfer pages do not need images, video or web fonts. Blocking only
        # these heavy resources keeps all HTML, CSS and JavaScript behaviour
        # intact while making each OPOST edit page noticeably faster.
        try:
            def _lightweight_transfer_route(route):
                resource_type = route.request.resource_type
                if resource_type in {"image", "media", "font"}:
                    route.abort()
                else:
                    route.continue_()
            page.route("**/*", _lightweight_transfer_route)
        except Exception:
            pass

        def click_text_in(scope, texts: Iterable[str], timeout: int = 5000) -> bool:
            for text in texts:
                for exact in (True, False):
                    try:
                        loc = scope.get_by_text(text, exact=exact)
                        for i in range(loc.count()):
                            item = loc.nth(i)
                            if item.is_visible():
                                item.click(timeout=timeout)
                                return True
                    except Exception:
                        continue
            return False

        def find_edit_scope():
            for selector in ('[role="dialog"]', '.v-dialog:visible', '.modal:visible', '.drawer:visible', 'form:visible'):
                try:
                    loc = page.locator(selector)
                    for i in range(loc.count() - 1, -1, -1):
                        item = loc.nth(i)
                        if item.is_visible():
                            return item
                except Exception:
                    continue
            return page

        def find_manager_input(scope):
            for caption in ("Account Manager", "مدير الحساب"):
                try:
                    labels = scope.get_by_text(caption, exact=True)
                    for i in range(labels.count()):
                        label = labels.nth(i)
                        if not label.is_visible():
                            continue
                        for candidate in (
                            label.locator("xpath=following::input[1]"),
                            label.locator("xpath=ancestor::*[self::div or self::label][1]//input"),
                            label.locator("xpath=following::*[@role='combobox'][1]"),
                        ):
                            if candidate.count() and candidate.first.is_visible():
                                return candidate.first
                except Exception:
                    continue
            for selector in (
                'input[placeholder*="Select User" i]',
                'input[role="combobox"]',
                '[role="combobox"] input',
            ):
                try:
                    loc = scope.locator(selector)
                    for i in range(loc.count()):
                        item = loc.nth(i)
                        if not item.is_visible():
                            continue
                        nearby = ""
                        try:
                            nearby = item.locator("xpath=ancestor::div[1]").inner_text(timeout=700)
                        except Exception:
                            pass
                        if "manager" in nearby.casefold() or "مدير" in nearby or "Select User" in (item.get_attribute("placeholder") or ""):
                            return item
                except Exception:
                    continue
            return None

        def select_manager(scope, duplicate_index: int = 0) -> None:
            """Choose a real Account Manager option from the visible OPOST dropdown.

            OPOST uses a virtual/searchable relation widget. Typing text into the
            field does not assign the relation; a visible dropdown row must be
            clicked. This implementation deliberately follows the same interaction
            a user performs and avoids forcing hidden values that can trigger 422.
            """
            import re as _re

            field = find_manager_input(scope)
            if field is None:
                raise RuntimeError("لم يتم العثور على خانة مدير الحساب داخل نموذج التعديل.")

            def norm(value: str) -> str:
                value = (value or "").replace("ـ", " ")
                value = _re.sub(r"[\u200e\u200f\u202a-\u202e]", "", value)
                value = value.replace("–", "-").replace("—", "-")
                value = _re.sub(r"[^0-9A-Za-z\u0600-\u06FF]+", " ", value)
                return " ".join(value.casefold().split())

            wanted_phone = "".join(_re.findall(r"\d+", target_manager_label))
            wanted_norm = norm(target_manager_label)
            wanted_tokens = [t for t in wanted_norm.split() if len(t) >= 3 and not t.isdigit()]

            # The OPOST widget finds this option reliably when searching by the
            # Arabic name. Searching only by phone can leave the text uncommitted.
            search_terms = []
            if "خدمة" in wanted_norm and "مشترك" in wanted_norm:
                search_terms.extend(["خدمة المشتركين", "خدمة المش", wanted_phone])
            else:
                search_terms.extend([target_manager_label, wanted_phone])
            search_terms = [x for x in dict.fromkeys(x for x in search_terms if x)]

            last_visible = []
            for search_term in search_terms:
                field.click(timeout=5000)
                try:
                    field.press("Control+A")
                    field.fill(search_term)
                except Exception:
                    field.press("Control+A")
                    field.type(search_term, delay=12)
                # Most OPOST dropdowns render the matching rows immediately. Try
                # the small known dropdown containers first and fall back to the
                # older broad DOM scan only when the widget exposes no usable row.
                page.wait_for_timeout(220)
                fast_rows = []
                fast_selectors = (
                    '[role="listbox"] [role="option"]:visible',
                    '[role="listbox"] li:visible',
                    '.vs__dropdown-menu li:visible',
                    '.multiselect__content-wrapper li:visible',
                    '.select2-results__option:visible',
                    '.dropdown-menu:visible li:visible',
                )
                for selector in fast_selectors:
                    try:
                        rows = page.locator(selector)
                        for row_index in range(min(rows.count(), 40)):
                            row = rows.nth(row_index)
                            text = (row.inner_text(timeout=250) or '').strip()
                            if not text:
                                continue
                            ntext = norm(text)
                            digits = ''.join(_re.findall(r"\d+", text))
                            phone_ok = bool(wanted_phone and wanted_phone in digits)
                            token_ok = bool(wanted_tokens) and all(t in ntext for t in wanted_tokens[:2])
                            support_ok = 'خدمة' in ntext and 'مشترك' in ntext
                            if phone_ok or token_ok or support_ok:
                                fast_rows.append(row)
                        if fast_rows:
                            break
                    except Exception:
                        continue
                if fast_rows:
                    chosen = fast_rows[min(max(0, duplicate_index), len(fast_rows) - 1)]
                    chosen.click(timeout=2500, force=True)
                    page.wait_for_timeout(120)
                    try:
                        field.press('Tab')
                    except Exception:
                        pass
                    return

                # Inspect all visible text-bearing nodes only as a compatibility
                # fallback for the legacy virtual dropdown.
                visible = []
                try:
                    nodes = page.locator("body *:visible")
                    count = min(nodes.count(), 1200)
                    for i in range(count):
                        item = nodes.nth(i)
                        try:
                            text = (item.inner_text(timeout=45) or "").strip()
                        except Exception:
                            continue
                        if not text or len(text) > 180 or "\n" in text:
                            continue
                        ntext = norm(text)
                        digits = "".join(_re.findall(r"\d+", text))
                        phone_ok = bool(wanted_phone and wanted_phone in digits)
                        token_ok = bool(wanted_tokens) and all(t in ntext for t in wanted_tokens[:2])
                        support_ok = "خدمة" in ntext and "مشترك" in ntext
                        if phone_ok or token_ok or support_ok:
                            visible.append((item, text))
                    last_visible = [text for _, text in visible[:8]]
                except Exception:
                    visible = []

                # Prefer the smallest clickable row containing the exact option.
                clickable = []
                seen = set()
                for item, text in visible:
                    try:
                        candidate = item
                        tag = (candidate.evaluate("el => el.tagName") or "").lower()
                        if tag not in {"li", "div", "span", "a", "button"}:
                            continue
                        box = candidate.bounding_box()
                        if not box or box.get("height", 0) > 90 or box.get("width", 0) < 80:
                            continue
                        key = (round(box["x"]), round(box["y"]), text)
                        if key in seen:
                            continue
                        seen.add(key)
                        clickable.append(candidate)
                    except Exception:
                        continue

                if clickable:
                    chosen = clickable[min(max(0, duplicate_index), len(clickable)-1)]
                    try:
                        chosen.scroll_into_view_if_needed(timeout=1500)
                    except Exception:
                        pass
                    chosen.click(timeout=5000, force=True)
                    page.wait_for_timeout(160)
                else:
                    # Keyboard fallback for a virtualized dropdown.
                    field.press("ArrowDown")
                    for _ in range(max(0, duplicate_index)):
                        field.press("ArrowDown")
                    field.press("Enter")
                    page.wait_for_timeout(160)

                # A committed option replaces the search text with the selected
                # display value or closes the list. Do not require hidden inputs.
                try:
                    current_value = (field.input_value(timeout=800) or "").strip()
                except Exception:
                    current_value = ""
                current_norm = norm(current_value)
                current_digits = "".join(_re.findall(r"\d+", current_value))
                list_open = False
                try:
                    list_open = page.locator('[role="listbox"]:visible, .vs__dropdown-menu:visible, .multiselect__content-wrapper:visible, .select2-results:visible').count() > 0
                except Exception:
                    pass
                committed = (
                    (wanted_phone and wanted_phone in current_digits)
                    or ("خدمة" in current_norm and "مشترك" in current_norm)
                    or (current_value and norm(search_term) != current_norm and not list_open)
                )
                if committed:
                    try:
                        field.press("Tab")
                    except Exception:
                        pass
                    page.wait_for_timeout(80)
                    return

                # Clear and try the next search term / duplicate.
                try:
                    field.press("Escape")
                except Exception:
                    pass

            details = f" الخيارات الظاهرة: {', '.join(last_visible)}" if last_visible else ""
            raise RuntimeError(f"لم يتم تثبيت المدير كخيار فعلي داخل OPOST: {target_manager_label}.{details}")

        def _short_name_candidates(business_name: str, business_id: str) -> List[str]:
            """Generate meaningful, valid and deterministic 3–5 letter codes.

            The first three letters are derived from the real business name.
            Two extra letters are derived from the unique Business ID, keeping
            the result readable while making collisions extremely unlikely.
            More candidates are returned so OPOST can reject an existing code
            and the next one can be tried without asking the user.
            """
            import hashlib as _hashlib
            import re as _re

            arabic_map = {
                'ا':'A','أ':'A','إ':'A','آ':'A','ب':'B','ت':'T','ث':'T','ج':'J','ح':'H','خ':'K',
                'د':'D','ذ':'D','ر':'R','ز':'Z','س':'S','ش':'S','ص':'S','ض':'D','ط':'T','ظ':'Z',
                'ع':'A','غ':'G','ف':'F','ق':'K','ك':'K','ل':'L','م':'M','ن':'N','ه':'H','ة':'H',
                'و':'W','ؤ':'W','ي':'Y','ى':'Y','ئ':'Y','ء':'A'
            }
            raw = str(business_name or '').strip()
            words = [w for w in _re.split(r'[^A-Za-z\u0600-\u06FF]+', raw) if w]
            transliterated = []
            for word in words:
                chars = []
                for ch in word:
                    if 'A' <= ch.upper() <= 'Z':
                        chars.append(ch.upper())
                    elif ch in arabic_map:
                        chars.append(arabic_map[ch])
                if chars:
                    transliterated.append(''.join(chars))

            # Prefer initials for multi-word names; otherwise keep consonant-like
            # letters from the actual name. Example: شرق -> SRK.
            if len(transliterated) >= 3:
                root = ''.join(w[0] for w in transliterated[:3])
            elif len(transliterated) == 2:
                root = transliterated[0][0] + transliterated[1][0]
                root += (transliterated[0][1:2] or transliterated[1][1:2] or 'X')
            elif transliterated:
                root = transliterated[0]
            else:
                root = 'BIZ'
            root = _re.sub(r'[^A-Z]', '', root.upper())
            root = (root + 'BIZ')[:3]

            results: List[str] = []
            for salt in range(24):
                digest = _hashlib.sha1(f'{business_id}|{raw}|{salt}'.encode('utf-8')).digest()
                suffix = chr(65 + digest[0] % 26) + chr(65 + digest[1] % 26)
                candidate = (root + suffix)[:5]
                if 3 <= len(candidate) <= 5 and candidate not in results:
                    results.append(candidate)
            return results

        def _find_short_name_input(scope):
            selectors = (
                'input[name="short_name"]', 'input[name$="[short_name]"]',
                'input[id*="short_name" i]', 'input[name*="short-name" i]'
            )
            for selector in selectors:
                try:
                    loc = scope.locator(selector)
                    for i in range(loc.count()):
                        item = loc.nth(i)
                        if item.is_visible():
                            return item
                except Exception:
                    continue
            for caption in ('Short Name', 'الاسم المختصر', 'اسم مختصر'):
                try:
                    label = scope.get_by_text(caption, exact=False).first
                    if label.count() and label.is_visible():
                        candidate = label.locator('xpath=following::input[1]')
                        if candidate.count() and candidate.first.is_visible():
                            return candidate.first
                except Exception:
                    continue
            return None

        def _set_generated_short_name(scope, business_id: str, business_name: str, candidate_index: int = 0) -> str:
            """Set a generated short name and force the frontend model to receive it.

            OPOST's old edit form uses a JS-controlled input. ``fill()`` can update
            what is visible without updating the internal model, so use the native
            value setter, dispatch the complete event sequence, then verify the
            displayed value before saving.
            """
            field = _find_short_name_input(scope)
            if field is None:
                raise RuntimeError('تعذر العثور على خانة الاسم المختصر داخل نموذج OPOST.')
            candidates = _short_name_candidates(business_name, business_id)
            if not candidates:
                raise RuntimeError('تعذر إنشاء اسم مختصر صالح للحساب.')
            candidate = candidates[min(max(0, candidate_index), len(candidates) - 1)]
            field.scroll_into_view_if_needed(timeout=2500)
            field.click(timeout=2500)
            field.evaluate("""(el, value) => {
                const proto = Object.getPrototypeOf(el);
                const descriptor = Object.getOwnPropertyDescriptor(proto, 'value')
                    || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                if (descriptor && descriptor.set) descriptor.set.call(el, value);
                else el.value = value;
                el.dataset.optimusShortNameChanged = '1';
                for (const type of ['input', 'change', 'blur']) {
                    el.dispatchEvent(new Event(type, {bubbles:true, composed:true}));
                }
            }""", candidate)
            # Keyboard interaction is an additional fallback for Vue/React wrappers.
            try:
                field.press('Control+A')
                field.type(candidate, delay=8)
                field.press('Tab')
            except Exception:
                pass
            page.wait_for_timeout(80)
            try:
                actual = (field.input_value(timeout=800) or '').strip().upper()
            except Exception:
                actual = ''
            if actual != candidate:
                raise RuntimeError(f'تعذر تثبيت الاسم المختصر الجديد داخل OPOST: {candidate}.')
            return candidate

        def _read_business_name_from_form(scope, business_id: str) -> str:
            """Read the name from the already-open edit form without another API call."""
            selectors = (
                'input[name="name"]', 'input[name$="[name]"]',
                'input[id$="name" i]', 'input[id*="business_name" i]'
            )
            for selector in selectors:
                try:
                    loc = scope.locator(selector)
                    for i in range(loc.count()):
                        item = loc.nth(i)
                        if not item.is_visible():
                            continue
                        value = (item.input_value(timeout=500) or '').strip()
                        if value:
                            return value
                except Exception:
                    continue
            for caption in ('Name', 'الاسم'):
                try:
                    label = scope.get_by_text(caption, exact=True).first
                    if label.count() and label.is_visible():
                        inp = label.locator('xpath=following::input[1]').first
                        if inp.count() and inp.is_visible():
                            value = (inp.input_value(timeout=500) or '').strip()
                            if value:
                                return value
                except Exception:
                    continue
            return f'BUSINESS {business_id}'

        def _repair_visible_duplicate_short_name(scope, business_id: str, business_name: str) -> Optional[str]:
            """Fix the legacy duplicate immediately when OPOST already shows it."""
            try:
                text = scope.inner_text(timeout=1200).casefold()
            except Exception:
                text = ''
            if 'short name has already been taken' in text or 'الاسم المختصر' in text and ('مستخدم' in text or 'مكرر' in text):
                return _set_generated_short_name(scope, business_id, business_name, 0)
            return None

        def _disable_unchanged_unique_fields(scope) -> None:
            """Exclude unchanged unique fields from the update request.

            The legacy OPOST edit form sometimes validates ``short_name`` as if
            a new business were being created. Sending the unchanged value then
            produces the false error "The short name has already been taken".
            A normal partial update should omit fields that were not edited, so
            disable only the short-name control immediately before submitting.
            """
            script = r"""
            root => {
              const norm = value => String(value || '')
                .replace(/[\u200e\u200f\u202a-\u202e]/g, '')
                .trim().toLowerCase();
              const labels = [...root.querySelectorAll('label, .form-label, .v-label, span, div')];
              const targets = [];
              for (const label of labels) {
                const text = norm(label.textContent);
                if (!(text === 'short name' || text.includes('short name') || text.includes('اسم مختصر'))) continue;
                let input = null;
                if (label.htmlFor) input = document.getElementById(label.htmlFor);
                if (!input) {
                  const box = label.closest('label, .form-group, .v-input, .field, div');
                  input = box && box.querySelector('input, textarea');
                }
                if (!input) {
                  let node = label.nextElementSibling;
                  while (node && !input) {
                    input = node.matches?.('input,textarea') ? node : node.querySelector?.('input,textarea');
                    node = node.nextElementSibling;
                  }
                }
                if (input) targets.push(input);
              }
              for (const input of document.querySelectorAll(
                'input[name="short_name"], input[name$="[short_name]"], input[id*="short_name" i], input[name*="short-name" i]'
              )) targets.push(input);
              const unique = [...new Set(targets)];
              for (const input of unique) {
                if (input.dataset.optimusShortNameChanged === '1') continue;
                input.dataset.optimusWasDisabled = input.disabled ? '1' : '0';
                input.disabled = true;
              }
              return unique.length;
            }
            """
            try:
                scope.evaluate(script)
            except Exception:
                try:
                    page.evaluate(script, page.locator('form:visible').last.element_handle())
                except Exception:
                    pass

        def save_and_wait(scope, business_id: str) -> None:
            error_messages: List[str] = []
            responses: List[tuple[int, str, str]] = []

            def remember(response):
                try:
                    method = response.request.method.upper()
                    url = response.url.lower()
                    if method in {"POST", "PUT", "PATCH"} and "business" in url:
                        body = ""
                        if response.status >= 400:
                            try:
                                body = response.text()[:1200]
                            except Exception:
                                body = ""
                        responses.append((response.status, response.url, body))
                except Exception:
                    pass

            page.on("response", remember)
            try:
                # OPOST's legacy form has a broken uniqueness check for the
                # unchanged short name. Exclude that untouched field so the
                # request contains only the actual manager change.
                _disable_unchanged_unique_fields(scope)
                if not click_text_in(scope, ("حفظ", "تحديث", "Save", "Update"), timeout=5500):
                    submit = scope.locator('button[type="submit"], input[type="submit"]').last
                    if not submit.count() or not submit.is_visible():
                        raise RuntimeError("لم يتم العثور على زر حفظ التعديل.")
                    submit.click(timeout=5000)

                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    page.wait_for_timeout(100)
                    # A success toast or the edit form closing is enough.
                    success_seen = False
                    for selector in ('[role="alert"]', '.toast', '.notification', '.alert', '.v-snack'):
                        try:
                            loc = page.locator(selector)
                            for i in range(loc.count()):
                                item = loc.nth(i)
                                if not item.is_visible():
                                    continue
                                text = item.inner_text(timeout=500).strip()
                                low = text.casefold()
                                if text and any(k in low for k in ("success", "updated", "saved", "نجاح", "تم التحديث", "تم الحفظ")):
                                    success_seen = True
                                if text and any(k in low for k in ("error", "failed", "رفض", "فشل", "غير مسموح")):
                                    error_messages.append(text)
                        except Exception:
                            continue
                    if error_messages or success_seen or responses:
                        break

                for status, _url, body in responses:
                    if status >= 400:
                        detail = ""
                        if body:
                            try:
                                import json as _json
                                parsed = _json.loads(body)
                                errors = parsed.get("errors") if isinstance(parsed, dict) else None
                                message = parsed.get("message") if isinstance(parsed, dict) else None
                                if isinstance(errors, dict):
                                    detail = " ".join(str(v[0] if isinstance(v, list) and v else v) for v in errors.values())
                                elif message:
                                    detail = str(message)
                            except Exception:
                                detail = body[:350]
                        if "short name has already been taken" in detail.casefold():
                            detail = "رفض OPOST حقل الاسم المختصر رغم أنه لم يتغير. تم استبعاده من الطلب، لكن النموذج القديم أعاد إرساله."
                        error_messages.append(f"رفض OPOST طلب الحفظ (HTTP {status})" + (f": {detail}" if detail else "."))
                if error_messages:
                    raise RuntimeError(" ".join(dict.fromkeys(error_messages)))
            finally:
                try:
                    page.remove_listener("response", remember)
                except Exception:
                    pass

        def business_name_for_search(business_id: str) -> str:
            try:
                detail = _get_business_detail_for_update(client, business_id)
                return str(detail.get("display") or detail.get("name") or "").strip()
            except Exception:
                return ""

        # Once the first direct edit URL succeeds, reuse exactly that route for
        # the rest of this worker. Older versions retried up to four routes for
        # every account, which could multiply navigation time across large batches.
        preferred_edit_route: Optional[str] = None

        def open_business_edit(business_id: str) -> Any:
            nonlocal preferred_edit_route
            """Open the edit form directly and wait only for the required fields.

            The previous implementation slept for a fixed time, checked the form
            too early, then performed an expensive table search. On OPOST's
            dynamically rendered edit page this caused both false "not found"
            failures and a large delay. The Business ID already gives us the exact
            edit URL, so use it as the primary and normal path.
            """
            route_templates = (
                "/resources/businesses/{id}/edit",
                "/logistics/resources/businesses/{id}/edit",
                "/en/logistics/resources/businesses/{id}/edit",
                "/en/w/resources/businesses/{id}/edit",
            )
            if preferred_edit_route:
                ordered_templates = (preferred_edit_route,) + tuple(
                    item for item in route_templates if item != preferred_edit_route
                )
            else:
                ordered_templates = route_templates
            last_error = None
            for route_template in ordered_templates:
                route = route_template.format(id=business_id)
                try:
                    # 'commit' returns as soon as the server accepts navigation;
                    # then we wait for the one field the transfer actually needs.
                    page.goto(f"{base}{route}", wait_until="commit", timeout=15000)
                    deadline = time.monotonic() + 10.0
                    while time.monotonic() < deadline:
                        # A redirect to login means this route cannot be used with
                        # the current session. Do not waste the full timeout.
                        current = (page.url or "").casefold()
                        if "/login" in current or "/auth" in current:
                            raise RuntimeError("انتهت جلسة OPOST أثناء فتح الحساب.")
                        scope = find_edit_scope()
                        if find_manager_input(scope) is not None and _find_short_name_input(scope) is not None:
                            preferred_edit_route = route_template
                            return scope
                        page.wait_for_timeout(80)
                    last_error = RuntimeError(f"لم يكتمل تحميل نموذج تعديل الحساب #{business_id}.")
                except Exception as exc:
                    last_error = exc
                    continue

            # Compatibility fallback for installations where the edit route is
            # changed. It runs only after every direct route genuinely failed.
            business_name = business_name_for_search(business_id)
            query_value = business_name or business_id
            for route in ("/logistics/resources/businesses", "/en/logistics/resources/businesses"):
                try:
                    search_url = f"{base}{route}?name={quote(query_value)}&page=1"
                    page.goto(search_url, wait_until="commit", timeout=15000)
                    row = page.locator("tr", has_text=business_id).first
                    try:
                        row.wait_for(state="visible", timeout=5000)
                    except Exception:
                        row = page.locator("[role='row']", has_text=business_id).first
                        row.wait_for(state="visible", timeout=2500)

                    # Expand the matching row and open Edit.
                    controls = row.locator('button, [role="button"], a')
                    clicked = False
                    for i in range(controls.count() - 1, -1, -1):
                        control = controls.nth(i)
                        if control.is_visible():
                            control.click(timeout=2000)
                            clicked = True
                            break
                    if not clicked:
                        row.click(timeout=2000)
                    if not click_text_in(page, ("تعديل", "Edit"), timeout=3000):
                        raise RuntimeError("لم يظهر زر تعديل الحساب بعد فتح صف الحساب.")

                    deadline = time.monotonic() + 7.0
                    while time.monotonic() < deadline:
                        scope = find_edit_scope()
                        if find_manager_input(scope) is not None and _find_short_name_input(scope) is not None:
                            return scope
                        page.wait_for_timeout(80)
                except Exception as exc:
                    last_error = exc
                    continue

            if last_error:
                raise RuntimeError(f"تعذر فتح نموذج الحساب #{business_id} داخل OPOST: {last_error}")
            raise RuntimeError(f"تعذر فتح نموذج الحساب #{business_id} داخل OPOST.")

        def transfer_one(business_id: str) -> None:
            """Update one business with the fewest possible page operations.

            The old OPOST database contains duplicate short names. The new form
            validates uniqueness on every save, even when only the manager changes.
            Therefore we proactively assign a meaningful unique 3–5 letter code in
            the same save as the manager change. This avoids the guaranteed first
            422 response and removes an entire reopen/retry cycle per account.
            """
            scope = open_business_edit(business_id)
            business_name = _read_business_name_from_form(scope, business_id)

            # Select the manager once. Duplicate manager records are retried only
            # if OPOST rejects the save for a reason unrelated to short_name.
            manager_variant = 0
            select_manager(scope, duplicate_index=manager_variant)

            errors: List[str] = []
            candidates = _short_name_candidates(business_name, business_id)
            # Six deterministic candidates are ample while keeping worst-case time low.
            for candidate_index in range(min(6, len(candidates))):
                try:
                    _set_generated_short_name(scope, business_id, business_name, candidate_index)
                    save_and_wait(scope, business_id)
                    return
                except Exception as exc:
                    message = str(exc)
                    errors.append(message)
                    low = message.casefold()
                    duplicate_short = ('short name' in low and 'taken' in low) or ('الاسم المختصر' in message)
                    if duplicate_short:
                        # The form remains open after 422; change only the short name
                        # and submit again—no navigation, no login, no manager reload.
                        continue
                    # If the manager relation itself was rejected, try another
                    # duplicate manager option once, still on the same form.
                    if manager_variant < 2 and any(k in low for k in ('manager', 'مدير', '422')):
                        manager_variant += 1
                        try:
                            select_manager(scope, duplicate_index=manager_variant)
                            continue
                        except Exception as manager_exc:
                            errors.append(str(manager_exc))
                    break
            raise RuntimeError(errors[-1] if errors else 'تعذر تعديل مدير الحساب داخل OPOST.')

        for business_id in ids:
            try:
                transfer_one(business_id)
                output.append({"id": business_id, "status": "success", "message": "تم التحويل بنجاح داخل OPOST."})
            except Exception as exc:
                output.append({"id": business_id, "status": "failed", "message": str(exc)})
    finally:
        client.close()

    return {
        "success": [row for row in output if row["status"] == "success"],
        "failed": [row for row in output if row["status"] != "success"],
    }

def _ui_change_account_manager_batch(
    business_ids: Iterable[str],
    target_manager_id: str,
    target_manager_label: str,
    max_workers: int = 6,
) -> Dict[str, Any]:
    """Run the proven UI transfer flow in a small number of parallel sessions.

    Each worker keeps one authenticated browser and processes its own chunk
    sequentially. This preserves the exact successful per-account workflow while
    avoiding the previous one-account-at-a-time bottleneck for large selections.
    """
    ids = list(dict.fromkeys(str(x).strip() for x in business_ids if str(x).strip()))
    if not ids:
        return {"success": [], "failed": []}

    # Six sessions is a practical upper bound: it substantially reduces batch
    # duration without opening hundreds of browsers or overwhelming OPOST.
    worker_count = max(1, min(int(max_workers or 1), 6, len(ids)))
    if worker_count == 1:
        return _ui_change_account_manager_batch_worker(ids, target_manager_id, target_manager_label)

    chunks = [ids[i::worker_count] for i in range(worker_count)]
    success: List[Dict[str, str]] = []
    failed: List[Dict[str, str]] = []

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="opost-transfer") as pool:
        futures = {
            pool.submit(
                _ui_change_account_manager_batch_worker,
                chunk,
                target_manager_id,
                target_manager_label,
            ): chunk
            for chunk in chunks if chunk
        }
        for future in as_completed(futures):
            chunk = futures[future]
            try:
                result = future.result()
                success.extend(result.get("success") or [])
                failed.extend(result.get("failed") or [])
            except Exception as exc:
                failed.extend({
                    "id": business_id,
                    "status": "failed",
                    "message": f"تعذر تشغيل مسار التحويل: {exc}",
                } for business_id in chunk)

    order = {business_id: index for index, business_id in enumerate(ids)}
    success.sort(key=lambda row: order.get(str(row.get("id") or ""), len(order)))
    failed.sort(key=lambda row: order.get(str(row.get("id") or ""), len(order)))
    return {"success": success, "failed": failed}


def opost_bulk_change_account_manager(
    business_ids: Iterable[str],
    source_manager_id: str,
    source_manager_label: str,
    target_manager_id: str,
    target_manager_label: str,
    max_workers: int = 6,
) -> Dict[str, Any]:
    """Change selected businesses inside OPOST using its original edit UI.

    OPOST currently rejects the previous direct update request even for a valid
    account. The browser flow below mirrors the confirmed working manual steps
    and reuses one authenticated page for the complete batch.
    """
    ids = list(dict.fromkeys(str(x).strip() for x in business_ids if str(x).strip()))
    if not ids:
        return {"success": [], "failed": []}
    if not str(source_manager_id or "").strip() or not str(target_manager_id or "").strip():
        raise RuntimeError("يجب اختيار المدير الحالي والمدير الجديد من قائمة OPOST.")
    if str(source_manager_id).strip() == str(target_manager_id).strip():
        raise RuntimeError("المدير الحالي والمدير الجديد متطابقان.")
    return _ui_change_account_manager_batch(
        ids,
        str(target_manager_id or "").strip(),
        str(target_manager_label or "").strip(),
        max_workers=max_workers,
    )

