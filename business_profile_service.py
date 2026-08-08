from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
import threading
from urllib.parse import urlencode


from business_service import (
    build_row, field_value, get_account_fields, parse_datetime, shipment_statistics,
    normalize_text, status_matches, CLOSED_STATUSES, DELIVERED_STATUSES,
    RETURNED_STATUSES, CANCELLED_STATUSES,
)
from opost_client import OpostClient

PROJECT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_DIR / "cache" / "profiles"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
DIRECTORY_CACHE = CACHE_DIR / "business_directory.json"
DIRECTORY_TTL = 24 * 60 * 60
PROFILE_CACHE_VERSION = "3.0.7-profile-fee-city-fast-v1"
PROFILE_CACHE_TTL = 10 * 60
_DIRECTORY_LOCK = threading.RLock()
_DIRECTORY_MEMORY: Optional[List[Dict[str, Any]]] = None
_DIRECTORY_NAME_INDEX: Optional[Dict[str, List[Dict[str, Any]]]] = None
CITY_CACHE = CACHE_DIR / "city_directory.json"
_CITY_LOOKUP_MEMORY: Optional[Dict[str, str]] = None
_CITY_LOCK = threading.RLock()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _payload_records(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict) and ("data" in payload[0] or "id" in payload[0]):
            payload = payload[0]
        else:
            return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    if payload.get("id") is not None:
        return [payload]
    return []


def _unique_businesses(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result = []
    for item in items:
        business_id = _clean(item.get("id"))
        if not business_id or business_id in seen:
            continue
        seen.add(business_id)
        result.append(item)
    return result


def _load_directory(client: OpostClient) -> List[Dict[str, Any]]:
    global _DIRECTORY_MEMORY, _DIRECTORY_NAME_INDEX
    with _DIRECTORY_LOCK:
        if _DIRECTORY_MEMORY is not None:
            return _DIRECTORY_MEMORY
        if DIRECTORY_CACHE.exists():
            try:
                payload = json.loads(DIRECTORY_CACHE.read_text(encoding="utf-8"))
                if isinstance(payload, list) and payload:
                    _DIRECTORY_MEMORY = [item for item in payload if isinstance(item, dict)]
                    return _DIRECTORY_MEMORY
            except (OSError, json.JSONDecodeError):
                pass

    # Refresh only when no usable local directory exists. A stale directory is
    # still better for instant search and identity matching; reports refresh it.
    businesses = client.get_all("businesses?limit=5000", concurrency=32)
    businesses = _unique_businesses([item for item in businesses if isinstance(item, dict)])
    with _DIRECTORY_LOCK:
        _DIRECTORY_MEMORY = businesses
        _DIRECTORY_NAME_INDEX = None
        try:
            temp = DIRECTORY_CACHE.with_suffix(f".{int(time.time()*1000)}.tmp")
            temp.write_text(json.dumps(businesses, ensure_ascii=False, default=str), encoding="utf-8")
            temp.replace(DIRECTORY_CACHE)
        except OSError:
            pass
    return businesses


def get_business_name_index(client: OpostClient) -> Dict[str, List[Dict[str, Any]]]:
    global _DIRECTORY_NAME_INDEX
    with _DIRECTORY_LOCK:
        if _DIRECTORY_NAME_INDEX is not None:
            return _DIRECTORY_NAME_INDEX
    directory = _load_directory(client)
    index: Dict[str, List[Dict[str, Any]]] = {}
    for item in directory:
        name = _clean(item.get("display") or item.get("name") or field_value(item, "name"))
        if not name:
            continue
        normalized = _normalize_search_text(name)
        index.setdefault(normalized, []).append(item)
    with _DIRECTORY_LOCK:
        _DIRECTORY_NAME_INDEX = index
    return index


def _normalize_search_text(value: str) -> str:
    import re
    text = _clean(value).casefold()
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"[^\w\u0600-\u06ff]+", "", text)


def search_businesses(query: str, limit: int = 20) -> List[Dict[str, Any]]:
    query = _clean(query)
    if not query:
        return []

    # Local/in-memory path first. Once the directory has been warmed this avoids
    # both OPOST login checks and reparsing the large JSON file on every search.
    client = OpostClient()
    try:
        client.start()
        if DIRECTORY_CACHE.exists() or _DIRECTORY_MEMORY is not None:
            directory = _load_directory(client)
            return _summaries(_match_businesses(directory, query)[:limit])

        client.login()
        if query.isdigit():
            try:
                exact = _payload_records(client.get_page(f"businesses/{query}", 1))
                exact = [b for b in exact if _clean(b.get("id")) == query]
                if exact:
                    return _summaries(exact[:limit])
            except Exception:
                pass
        directory = _load_directory(client)
        return _summaries(_match_businesses(directory, query)[:limit])
    finally:
        client.close()


def _match_businesses(items: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    needle = query.casefold()
    exact_id = query if query.isdigit() else None
    matches = []
    for business in _unique_businesses(items):
        business_id = _clean(business.get("id"))
        name = _clean(business.get("display") or business.get("name") or field_value(business, "name"))
        phone = _clean(field_value(business, "phone"))
        if exact_id and business_id == exact_id:
            matches.insert(0, business)
        elif needle in name.casefold() or needle in business_id.casefold() or (phone and needle in phone.casefold()):
            matches.append(business)
    return matches


def _summaries(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "id": item.get("id", ""),
            "name": _clean(item.get("display") or item.get("name") or field_value(item, "name")),
            "phone": _clean(field_value(item, "phone")),
            "created_at": _clean(field_value(item, "created_at")),
            "office": _clean(field_value(item, "office")),
        }
        for item in items
    ]


def _get_business_by_id(client: OpostClient, business_id: str) -> Dict[str, Any]:
    business_id = _clean(business_id)
    attempts: List[Dict[str, Any]] = []
    for resource in (
        f"businesses/{business_id}",
        "businesses?" + urlencode({"id": business_id, "limit": 1000}),
    ):
        try:
            if "/" in resource and "?" not in resource:
                attempts.extend(_payload_records(client.get_page(resource, 1)))
            else:
                attempts.extend(client.get_all(resource, concurrency=4))
        except Exception:
            continue
        for business in attempts:
            if _clean(business.get("id")) == business_id:
                return business
    for business in _load_directory(client):
        if _clean(business.get("id")) == business_id:
            return business
    raise RuntimeError("لم يتم العثور على الحساب المطلوب في OPOST.")


def _automatic_profile_period(business: Dict[str, Any]) -> tuple[str, str]:
    """Return the complete account history period used by the profile page.

    The profile is a CV for the business, so its shipment totals must match the
    cumulative figures visible in OPOST.  The 40-day incubation window remains
    part of monthly report classification only; using it here caused old
    businesses to incorrectly appear with zero shipments.
    """
    today = datetime.now().date()
    created = parse_datetime(field_value(business, "created_at"))
    start = created.date() if created is not None else today.replace(year=max(2000, today.year - 10), month=1, day=1)
    if start > today:
        start = today
    return start.isoformat(), today.isoformat()


def _field_identity(field: Dict[str, Any]) -> str:
    return _clean(field.get("attribute") or field.get("name") or field.get("related_name")).casefold()


def _human_field_value(field: Dict[str, Any]) -> str:
    """Prefer the human-readable relation label over numeric foreign keys."""
    def pick(value: Any) -> str:
        if value in (None, ""):
            return ""
        if isinstance(value, (str, int, float)):
            return _clean(value)
        if isinstance(value, list):
            values = [pick(item) for item in value]
            return ", ".join(item for item in values if item)
        if isinstance(value, dict):
            for key in ("display", "value_label", "label", "name", "title", "related_name"):
                text = pick(value.get(key))
                if text:
                    return text
            nested = pick(value.get("value"))
            if nested and not nested.isdigit():
                return nested
        return ""

    for source in (field.get("value_label"), field.get("display"), field.get("related"), field.get("value")):
        text = pick(source)
        if text and not (text.isdigit() and _field_identity(field).endswith("business_line")):
            return text
    return ""


def _curated_extra_fields(business: Dict[str, Any]) -> List[Dict[str, str]]:
    # Keep the profile useful and readable; hide internal resource payloads and raw JSON.
    excluded_fragments = {
        "logo", "users", "shipments_score", "shipment score",
        "shipments rate within 3 days", "business shipment stats",
        "shipment_stats", "business_shipment", "account_manager",
        "created_at", "phone", "mobile_intro", "id", "name", "office",
    }
    preferred = {
        "email", "short_name", "business_line", "price_plan", "address",
        "city", "region", "mobile", "website", "tax_number", "company_name",
    }
    labels = {
        "email": "Email", "short_name": "Short Name", "business_line": "Business Line",
        "price_plan": "Price Plan", "address": "Address", "city": "City",
        "region": "Region", "mobile": "Mobile", "website": "Website",
        "tax_number": "Tax Number", "company_name": "Company Name",
    }
    result: List[Dict[str, str]] = []
    for field in get_account_fields(business):
        identity = _field_identity(field)
        if not identity or any(part in identity for part in excluded_fragments):
            continue
        short = identity.split(".")[-1]
        if short not in preferred and identity not in preferred:
            continue
        text = _human_field_value(field)
        if not text:
            continue
        result.append({
            "label": labels.get(short, _clean(field.get("label") or short.replace("_", " ").title())),
            "translation_key": f"extra_{short}",
            "value": text,
        })
    return result




def _profile_closed_breakdown(shipment_source: Any) -> Dict[str, Any]:
    """Business-profile-only shipment breakdown.

    OPOST's operational closed bucket is composed of three business outcomes:
    COD (successfully delivered/collected), Delivered (closed return), and R
    (replacement/exchange).  We keep the report fetch untouched and classify
    only the already-fetched status counts used by the profile/PDF.
    """
    stats = shipment_statistics(shipment_source)
    status_counts = dict(stats.get("status_counts", {}) or {})

    cod = 0
    delivered_closed = 0
    exchange_r = 0
    returned = 0
    cancelled = 0
    other = 0

    exchange_aliases = {
        "r", "exchange", "replacement", "replace", "replaced",
        "تبديل", "استبدال", "بدل",
    }
    cod_aliases = {
        "cod", "c.o.d", "c o d", "cash on delivery", "cash_on_delivery", "cash delivery",
        "تحصيل", "نقدي عند الاستلام",
    }
    draft_aliases = {"draft", "مسودة"}
    submitted_aliases = {"submitted", "submit", "جاهز للاستلام", "بانتظار الاستلام"}
    exchange_norm = {normalize_text(x) for x in exchange_aliases}
    cod_norm = {normalize_text(x) for x in cod_aliases}
    draft_norm = {normalize_text(x) for x in draft_aliases}
    submitted_norm = {normalize_text(x) for x in submitted_aliases}
    draft = 0
    submitted = 0

    for raw_status, raw_count in status_counts.items():
        try:
            count = int(raw_count or 0)
        except (TypeError, ValueError):
            count = 0
        status = normalize_text(raw_status)
        if not count:
            continue

        # Exact R must be checked before the broader text groups.
        if status in exchange_norm:
            exchange_r += count
        elif status in cod_norm or status_matches(status, CLOSED_STATUSES):
            # Existing generic "closed" records in OPOST are the COD/success bucket.
            cod += count
        elif status_matches(status, DELIVERED_STATUSES):
            # In this business workflow OPOST Delivered means a closed returned parcel.
            delivered_closed += count
        elif status_matches(status, RETURNED_STATUSES):
            returned += count
        elif status_matches(status, CANCELLED_STATUSES):
            cancelled += count
        elif status in draft_norm or status.startswith("draft ") or status.endswith(" draft"):
            draft += count
        elif status in submitted_norm or status.startswith("submitted ") or status.endswith(" submitted"):
            submitted += count
        else:
            other += count

    total = int(stats.get("total", 0) or 0)
    # Aggregated payloads may not expose status_counts in older caches. Fall back
    # to the existing buckets without changing the fetch path.
    if total and not status_counts:
        cod = int(stats.get("closed", 0) or 0)
        delivered_closed = int(stats.get("delivered", 0) or 0)
        returned = int(stats.get("returned", 0) or 0)
        cancelled = int(stats.get("cancelled", 0) or 0)
        other = max(total - cod - delivered_closed - returned - cancelled, 0)

    closed_total = cod + delivered_closed + exchange_r
    # Keep the displayed total authoritative from OPOST. Any unclassified records
    # stay visible as Other instead of disappearing from the arithmetic.
    classified = closed_total + returned + cancelled + draft + submitted + other
    if total > classified:
        other += total - classified

    def rate(value: int) -> float:
        return round((value / total) * 100, 2) if total else 0.0

    return {
        "total": total,
        "cod": cod,
        "delivered_closed": delivered_closed,
        "exchange_r": exchange_r,
        "closed_total": closed_total,
        "closed_rate": rate(closed_total),
        "cod_rate": rate(cod),
        "returned": returned,
        "cancelled": cancelled,
        "draft": draft,
        "submitted": submitted,
        "in_progress": draft + submitted,
        "other": other,
        "status_counts": status_counts,
    }


def _display_value(value: Any) -> str:
    """Extract a human label from OPOST relation payloads without exposing IDs."""
    if value in (None, ""):
        return ""
    if isinstance(value, (str, int, float, bool)):
        return _clean(value)
    if isinstance(value, list):
        for entry in value:
            text = _display_value(entry)
            if text and not text.isdigit():
                return text
        return ""
    if isinstance(value, dict):
        for key in ("display", "value_label", "label", "name", "title", "text", "city_name"):
            text = _display_value(value.get(key))
            if text and not text.isdigit():
                return text
        fields = value.get("fields")
        if isinstance(fields, list):
            for field in fields:
                if isinstance(field, dict):
                    text = _human_field_value(field)
                    if text and not text.isdigit():
                        return text
        for key in ("related", "value", "data", "city", "office", "business"):
            text = _display_value(value.get(key))
            if text and not text.isdigit():
                return text
    return ""


def _field_visible(item: Dict[str, Any], aliases: set[str]) -> str:
    wanted = {normalize_text(a).replace(" ", "_") for a in aliases}
    for key, value in item.items():
        if normalize_text(key).replace(" ", "_") in wanted and value not in (None, ""):
            text = _display_value(value)
            if text:
                return text
            if not isinstance(value, (dict, list)):
                return _clean(value)
    for field in item.get("fields", []) or []:
        if not isinstance(field, dict):
            continue
        identity = normalize_text(field.get("attribute") or field.get("name") or field.get("related_name") or field.get("label")).replace(" ", "_")
        if identity not in wanted:
            continue
        text = _human_field_value(field) or _display_value(field.get("related")) or _display_value(field.get("value"))
        if text:
            return text
        value = field.get("value")
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return _clean(value)
    return ""

def _raw_field_value(item: Dict[str, Any], aliases: set[str]) -> Any:
    wanted = {normalize_text(a).replace(" ", "_") for a in aliases}
    for key, value in item.items():
        if normalize_text(key).replace(" ", "_") in wanted and value not in (None, ""):
            return value
    for field in item.get("fields", []) or []:
        if not isinstance(field, dict):
            continue
        identity = normalize_text(field.get("attribute") or field.get("name") or field.get("related_name") or field.get("label")).replace(" ", "_")
        if identity not in wanted:
            continue
        for key in ("related", "value", "resource_id", "resourceId"):
            value = field.get(key)
            if value not in (None, ""):
                return value
    return None


def _numeric_relation_id(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("resource_id", "resourceId", "id", "value", "key"):
            raw = value.get(key)
            text = _clean(raw)
            if text.isdigit():
                return text
        for key in ("related", "data", "city"):
            found = _numeric_relation_id(value.get(key))
            if found:
                return found
        return ""
    if isinstance(value, list):
        for entry in value:
            found = _numeric_relation_id(entry)
            if found:
                return found
        return ""
    text = _clean(value)
    return text if text.isdigit() else ""


def _city_lookup(client: OpostClient) -> Dict[str, str]:
    """Cached OPOST city directory used only as a last-resort fallback.

    Fee payloads normally include `city.name`; using that directly is both more
    accurate and much faster. The full city directory is therefore loaded only
    when OPOST truly returns a numeric relation without its label.
    """
    global _CITY_LOOKUP_MEMORY
    with _CITY_LOCK:
        if _CITY_LOOKUP_MEMORY is not None:
            return _CITY_LOOKUP_MEMORY
        try:
            if CITY_CACHE.exists():
                payload = json.loads(CITY_CACHE.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and payload:
                    _CITY_LOOKUP_MEMORY = {str(k): _clean(v) for k, v in payload.items() if _clean(v)}
                    return _CITY_LOOKUP_MEMORY
        except (OSError, json.JSONDecodeError):
            pass

    lookup: Dict[str, str] = {}
    try:
        cities = client.get_all("cities?limit=5000", concurrency=16)
        for city in cities:
            if not isinstance(city, dict):
                continue
            city_id = _clean(city.get("id"))
            name = _clean(city.get("display") or city.get("name") or field_value(city, "name") or field_value(city, "city_name"))
            if city_id and name:
                lookup[city_id] = name
    except Exception:
        lookup = {}
    with _CITY_LOCK:
        _CITY_LOOKUP_MEMORY = lookup
        if lookup:
            try:
                temp = CITY_CACHE.with_suffix(f".{int(time.time()*1000)}.tmp")
                temp.write_text(json.dumps(lookup, ensure_ascii=False), encoding="utf-8")
                temp.replace(CITY_CACHE)
            except OSError:
                pass
    return lookup


def _load_business_fees(client: OpostClient, business_id: str) -> List[Dict[str, str]]:
    """Load OPOST Business Fees once for the selected profile.

    This does not alter the existing account/shipment fetch. The old OPOST
    resource exposes Business Fees as its own resource, filtered by business.
    Results are stored inside the existing profile cache, so repeated PDF opens
    add no extra request for ten minutes.
    """
    from urllib.parse import urlencode

    resources = [
        "business-fees?" + urlencode({"business": business_id, "limit": 1000}),
        "business-fees?" + urlencode({"business_id": business_id, "limit": 1000}),
    ]
    records: List[Dict[str, Any]] = []
    for resource in resources:
        try:
            loaded = client.get_all(resource, concurrency=4)
        except Exception:
            continue
        candidates = [item for item in loaded if isinstance(item, dict)]
        if candidates:
            records = candidates
            break

    parsed: List[Dict[str, Any]] = []
    unresolved_city_ids: set[str] = set()
    for item in records:
        business_value = _field_visible(item, {"business", "business_name"})
        relation_raw = _raw_field_value(item, {"business_id", "business id", "business"})
        relation_id = _numeric_relation_id(relation_raw)
        if relation_id and relation_id != str(business_id):
            continue

        # OPOST business-fee records commonly expose the readable label in a
        # separate companion field such as `city.name` / related_name=`city.name`.
        # Read that first so we do NOT need to download the whole Cities resource
        # for every profile open. This fixes blank city names and keeps the page fast.
        city_label_aliases = {
            "city.name", "city_name", "city name", "city.display",
            "city_label", "city label", "المدينة", "المنطقة",
        }
        city_relation_aliases = {"city", "city_id", "city.id", "cities"}
        city = _field_visible(item, city_label_aliases)
        if not city:
            city = _field_visible(item, city_relation_aliases)
        city_raw = _raw_field_value(item, city_relation_aliases)
        city_id = _numeric_relation_id(city_raw)
        # A numeric relationship value is not useful to the user; resolve it from
        # OPOST Cities only when the fee response did not already include a label.
        if city.isdigit():
            city_id = city_id or city
            city = ""
        if not city and city_id:
            unresolved_city_ids.add(city_id)

        rest = _field_visible(item, {"rest_of_cities", "rest of cities", "rest_cities", "باقي المدن"})
        fee = _field_visible(item, {"fees", "fee"})
        cancel_fee = _field_visible(item, {"cancel_fees", "cancel fees", "cancel_fee"})
        parsed.append({
            "id": _clean(item.get("id")),
            "city": city, "city_id": city_id, "rest": rest,
            "fee": fee, "cancel_fee": cancel_fee, "business": business_value,
        })

    lookup = _city_lookup(client) if unresolved_city_ids else {}
    result: List[Dict[str, str]] = []
    seen = set()
    for row in parsed:
        rest = _clean(row.get("rest"))
        rest_yes = normalize_text(rest) in {"yes", "1", "true", "نعم"}
        city = _clean(row.get("city")) or lookup.get(_clean(row.get("city_id")), "")
        if rest_yes and not city:
            city = "باقي المدن"
        city = city or "—"
        signature = (city, rest_yes, row.get("fee"), row.get("cancel_fee"), row.get("id"))
        if signature in seen:
            continue
        seen.add(signature)
        result.append({
            "id": _clean(row.get("id")),
            "city": city,
            "rest_of_cities": "نعم" if rest_yes else ("لا" if rest else "—"),
            "fee": _clean(row.get("fee")) or "—",
            "cancel_fee": _clean(row.get("cancel_fee")) or "—",
            "business": _clean(row.get("business")),
        })
    return result

def _latest_profile_path(business_id: str) -> Path:
    return CACHE_DIR / f"business_{_clean(business_id)}_{PROFILE_CACHE_VERSION}.json"

def _load_fresh_profile(business_id: str, ttl: int = PROFILE_CACHE_TTL) -> Optional[Dict[str, Any]]:
    path = _latest_profile_path(business_id)
    try:
        if path.exists() and time.time() - path.stat().st_mtime <= ttl:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        pass
    return None

def build_business_profile(business_id: str) -> Dict[str, Any]:
    cached = _load_fresh_profile(business_id)
    if cached is not None:
        return cached
    client = OpostClient()
    try:
        client.start()
        client.login()
        business = _get_business_by_id(client, business_id)
        start_date, end_date = _automatic_profile_period(business)
        shipment_source_name = "verified business filter"
        try:
            shipments = client.get_business_shipments(
                business_id=business_id,
                start_date=start_date,
                end_date=end_date,
            )
            shipment_source: Any = shipments
        except Exception:
            # Accuracy-first fallback: aggregate the complete account history once,
            # but retain only the exact requested Business ID.
            stats_map = client.get_shipment_statistics_grouped_by_business(
                start_date=start_date,
                end_date=end_date,
                business_ids=[int(business_id)],
                concurrency=18,
            )
            shipment_source = stats_map.get(str(business_id), {})
            shipment_source_name = "verified full-period aggregation"

        row = build_row(
            business,
            shipment_source,
            reference_date=datetime.strptime(end_date, "%Y-%m-%d"),
        )
        breakdown = _profile_closed_breakdown(shipment_source)
        total = int(row.get("Shipments") or breakdown.get("total") or 0)
        row["Shipments"] = total
        row["COD"] = int(breakdown.get("cod", 0) or 0)
        row["Delivered"] = int(breakdown.get("delivered_closed", 0) or 0)
        row["R"] = int(breakdown.get("exchange_r", 0) or 0)
        row["Closed"] = int(breakdown.get("closed_total", 0) or 0)
        row["Closed %"] = float(breakdown.get("closed_rate", 0.0) or 0.0)
        row["Successful Delivery %"] = float(breakdown.get("cod_rate", 0.0) or 0.0)
        row["Returned"] = int(breakdown.get("returned", row.get("Returned", 0)) or 0)
        row["Cancelled"] = int(breakdown.get("cancelled", row.get("Cancelled", 0)) or 0)
        row["Draft"] = int(breakdown.get("draft", 0) or 0)
        row["Submitted"] = int(breakdown.get("submitted", 0) or 0)
        row["In Progress"] = int(breakdown.get("in_progress", 0) or 0)
        row["Other Status"] = int(breakdown.get("other", 0) or 0)
        row["In Progress / Other"] = row["In Progress"] + row["Other Status"]
        fees = _load_business_fees(client, business_id)
        profile = {
            "business": business,
            "row": row,
            "fields": _curated_extra_fields(business),
            "fees": fees,
            "shipment_breakdown": breakdown,
            "period": {
                "start_date": start_date,
                "end_date": end_date,
                "scope": "full_account_history",
            },
            "shipment_source": shipment_source_name,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            _latest_profile_path(business_id).write_text(
                json.dumps(profile, ensure_ascii=False, default=str), encoding="utf-8"
            )
        except OSError:
            pass
        return profile
    finally:
        client.close()


def save_profile_cache(profile: Dict[str, Any]) -> str:
    key = hashlib.sha256(
        json.dumps(
            {
                "id": profile.get("row", {}).get("Business ID"),
                "period": profile.get("period"),
                "generated_at": profile.get("generated_at"),
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()[:20]
    (CACHE_DIR / f"{key}.json").write_text(
        json.dumps(profile, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return key


def load_profile_cache(key: str) -> Optional[Dict[str, Any]]:
    if not key or not key.replace("-", "").isalnum():
        return None
    path = CACHE_DIR / f"{key}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None
