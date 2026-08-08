from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List

from business_service import field_value, normalize_text, parse_datetime, related_display_name
from opost_client import OpostClient

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_PATH = DATA_DIR / "live_analytics.json"
BUSINESS_CACHE_PATH = PROJECT_DIR / "cache" / "profiles" / "business_directory.json"
# The automatic refresh requested by the user: once every minute.
REFRESH_SECONDS = 60

_LOCK = threading.RLock()
_CONDITION = threading.Condition(_LOCK)
_REFRESHING = False
_LAST_REFRESH_STARTED = 0.0
_LAST_ERROR = ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _field_candidates(item: Dict[str, Any], aliases: Iterable[str]) -> List[Dict[str, Any]]:
    targets = {normalize_text(alias) for alias in aliases}
    matches: List[Dict[str, Any]] = []
    fields = item.get("fields")
    if not isinstance(fields, list):
        return matches
    for field in fields:
        if not isinstance(field, dict):
            continue
        names = {
            normalize_text(field.get("name")),
            normalize_text(field.get("related_name")),
            normalize_text(field.get("attribute")),
            normalize_text(field.get("label")),
        }
        if names & targets:
            matches.append(field)
    return matches


def _walk_display(value: Any) -> str:
    """Extract the human-readable label from OPOST relationship payloads."""
    if isinstance(value, dict):
        fields = value.get("fields")
        if isinstance(fields, list):
            for field in fields:
                if not isinstance(field, dict):
                    continue
                key = normalize_text(field.get("name") or field.get("related_name") or field.get("attribute") or field.get("label"))
                if key in {"name", "full name", "display", "title", "short name"}:
                    text = _walk_display(field.get("value_label") or field.get("value") or field.get("related"))
                    if text:
                        return text
        for key in ("display", "name", "full_name", "title", "label", "value_label", "text"):
            text = _text(value.get(key))
            if text and not text.isdigit():
                return text
        for key in ("related", "value", "data", "users", "office", "account_manager"):
            text = _walk_display(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, list):
        for entry in value:
            text = _walk_display(entry)
            if text:
                return text
        return ""
    text = _text(value)
    return text if text and not text.isdigit() else ""


def _relationship_name(item: Dict[str, Any], aliases: List[str]) -> str:
    # First read the exact relationship field. This avoids counting generic users
    # or unrelated nested names as account managers/offices.
    for field in _field_candidates(item, aliases):
        for key in ("value_label", "related", "value"):
            text = _walk_display(field.get(key))
            if text:
                return text

    # Some OPOST responses expose the relationship directly at the resource root.
    for alias in aliases:
        for key in (alias, alias.replace(" ", "_"), alias.replace("_", " ")):
            if key in item:
                text = _walk_display(item.get(key))
                if text:
                    return text

    # Keep compatibility with older response shapes.
    value = related_display_name(item, *aliases)
    if value and not value.isdigit():
        return value.strip()
    return ""


def _walk_relation_id(value: Any) -> str:
    """Extract the related OPOST resource ID without mistaking labels for IDs."""
    if isinstance(value, dict):
        for key in ("resource_id", "resourceId", "id", "key"):
            raw = value.get(key)
            text = _text(raw)
            if text and text.isdigit():
                return text
        for key in ("related", "value", "data", "office", "account_manager"):
            found = _walk_relation_id(value.get(key))
            if found:
                return found
        fields = value.get("fields")
        if isinstance(fields, list):
            for field in fields:
                if isinstance(field, dict):
                    found = _walk_relation_id(field)
                    if found:
                        return found
        return ""
    if isinstance(value, list):
        for entry in value:
            found = _walk_relation_id(entry)
            if found:
                return found
        return ""
    text = _text(value)
    return text if text.isdigit() else ""


def _relationship_detail(item: Dict[str, Any], aliases: List[str]) -> Dict[str, str]:
    """Return the exact Business relation label + ID from the business row."""
    for field in _field_candidates(item, aliases):
        name = ""
        relation_id = ""
        for key in ("value_label", "related", "value"):
            if not name:
                name = _walk_display(field.get(key))
            if not relation_id:
                relation_id = _walk_relation_id(field.get(key))
        if not relation_id:
            relation_id = _walk_relation_id(field)
        if name or relation_id:
            return {"id": relation_id, "name": name}

    for alias in aliases:
        for key in (alias, alias.replace(" ", "_"), alias.replace("_", " ")):
            if key not in item:
                continue
            value = item.get(key)
            name = _walk_display(value)
            relation_id = _walk_relation_id(value)
            if name or relation_id:
                return {"id": relation_id, "name": name}

    name = _relationship_name(item, aliases)
    return {"id": "", "name": name}


def _first_value(item: Dict[str, Any], aliases: List[str]) -> str:
    for alias in aliases:
        value = field_value(item, alias)
        if _text(value):
            return _text(value)
    return ""


def _business_name(item: Dict[str, Any]) -> str:
    return _text(item.get("display") or item.get("name") or field_value(item, "name"))


def _manager_detail(item: Dict[str, Any]) -> Dict[str, str]:
    detail = _relationship_detail(
        item,
        ["account_manager", "account manager", "account_manager_id", "business account manager"],
    )
    detail["name"] = detail.get("name") or "غير محدد"
    return detail


def _manager_name(item: Dict[str, Any]) -> str:
    return _manager_detail(item)["name"]


def _office_detail(item: Dict[str, Any]) -> Dict[str, str]:
    detail = _relationship_detail(
        item,
        ["office", "office_id", "business office", "business_office"],
    )
    detail["name"] = detail.get("name") or "غير محدد"
    return detail


def _office_name(item: Dict[str, Any]) -> str:
    return _office_detail(item)["name"]


def _created_at(item: Dict[str, Any]) -> str:
    return _first_value(item, ["created_at", "created at", "creation_date", "date_created"])


def _status(item: Dict[str, Any]) -> str:
    return _first_value(item, ["status", "active", "is_active"]) or "غير محدد"


def _load_business_cache() -> List[Dict[str, Any]]:
    try:
        payload = json.loads(BUSINESS_CACHE_PATH.read_text(encoding="utf-8"))
        return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(temp, path)


def _deduplicate_businesses(businesses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Count each OPOST business exactly once, even if a page is repeated."""
    seen: set[str] = set()
    rows: List[Dict[str, Any]] = []
    for index, item in enumerate(businesses):
        if not isinstance(item, dict):
            continue
        business_id = _text(item.get("id") or field_value(item, "id"))
        key = business_id or f"row:{index}:{_business_name(item)}:{_created_at(item)}"
        if key in seen:
            continue
        seen.add(key)
        rows.append(item)
    return rows


def build_snapshot(businesses: List[Dict[str, Any]], source: str = "OPOST") -> Dict[str, Any]:
    businesses = _deduplicate_businesses(businesses)
    managers: Counter[str] = Counter()
    offices: Counter[str] = Counter()
    manager_ids: Dict[str, str] = {}
    office_ids: Dict[str, str] = {}
    months: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    accounts: List[Dict[str, Any]] = []
    now = datetime.now()
    incubation_cutoff = now - timedelta(days=40)
    incubation_count = 0

    for item in businesses:
        business_id = _text(item.get("id") or field_value(item, "id"))
        name = _business_name(item)
        manager_detail = _manager_detail(item)
        office_detail = _office_detail(item)
        manager = manager_detail["name"]
        office = office_detail["name"]
        if manager != "غير محدد" and manager_detail.get("id"):
            manager_ids.setdefault(manager, manager_detail["id"])
        if office != "غير محدد" and office_detail.get("id"):
            office_ids.setdefault(office, office_detail["id"])
        created_raw = _created_at(item)
        status = _status(item)
        created = parse_datetime(created_raw)
        if created:
            months[created.strftime("%Y-%m")] += 1
            if created >= incubation_cutoff:
                incubation_count += 1
        managers[manager] += 1
        offices[office] += 1
        statuses[status] += 1
        accounts.append({
            "business_id": business_id,
            "business_name": name,
            "created_at": created_raw,
            "office": office,
            "account_manager": manager,
            "status": status,
        })

    named_managers = [name for name in managers if name != "غير محدد"]
    named_offices = [name for name in offices if name != "غير محدد"]
    return {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at_epoch": time.time(),
        "source": source,
        "total_accounts": len(accounts),
        "incubation_accounts": incubation_count,
        "manager_count": len(named_managers),
        "office_count": len(named_offices),
        "unassigned_manager_accounts": int(managers.get("غير محدد", 0)),
        "unassigned_office_accounts": int(offices.get("غير محدد", 0)),
        "managers": [{"id": manager_ids.get(k, ""), "name": k, "count": v, "source": "Business Account Manager"} for k, v in managers.most_common() if k != "غير محدد"],
        "offices": [{"id": office_ids.get(k, ""), "name": k, "count": v, "source": "Business Office"} for k, v in offices.most_common() if k != "غير محدد"],
        "months": [{"month": k, "count": months[k]} for k in sorted(months)],
        "statuses": [{"name": k, "count": v} for k, v in statuses.most_common()],
        "validation": {
            "manager_accounts_counted": sum(managers.values()),
            "office_accounts_counted": sum(offices.values()),
            "manager_totals_match": sum(managers.values()) == len(accounts),
            "office_totals_match": sum(offices.values()) == len(accounts),
        },
        "accounts": accounts,
    }


def load_snapshot() -> Dict[str, Any]:
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
    except (OSError, json.JSONDecodeError):
        pass
    businesses = _load_business_cache()
    if businesses:
        snapshot = build_snapshot(businesses, source="Local OPOST cache")
        try:
            _write_json_atomic(CACHE_PATH, snapshot)
        except OSError:
            pass
        return snapshot
    return {
        "updated_at": "", "updated_at_epoch": 0, "source": "Waiting for OPOST",
        "total_accounts": 0, "incubation_accounts": 0, "manager_count": 0,
        "office_count": 0, "managers": [], "offices": [], "months": [],
        "statuses": [], "accounts": [],
    }


def _perform_refresh() -> Dict[str, Any]:
    global _REFRESHING, _LAST_ERROR
    client: OpostClient | None = None
    try:
        client = OpostClient()
        client.start()
        client.login()
        # OPOST pagination is handled by get_all. A high concurrency keeps a full
        # refresh short while the de-duplication below protects the totals.
        businesses = client.get_all("businesses?limit=5000", concurrency=32)
        valid_rows = [row for row in businesses if isinstance(row, dict)]
        snapshot = build_snapshot(valid_rows, source="OPOST live")
        _write_json_atomic(CACHE_PATH, snapshot)
        BUSINESS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(BUSINESS_CACHE_PATH, valid_rows)
        with _LOCK:
            _LAST_ERROR = ""
        return snapshot
    except Exception as error:
        with _LOCK:
            _LAST_ERROR = str(error)
        raise
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        with _CONDITION:
            _REFRESHING = False
            _CONDITION.notify_all()


def refresh_now(wait_timeout: float = 180.0) -> Dict[str, Any]:
    """Run a manual refresh now and return only after the new snapshot is saved."""
    global _REFRESHING, _LAST_REFRESH_STARTED
    with _CONDITION:
        if _REFRESHING:
            deadline = time.monotonic() + wait_timeout
            while _REFRESHING:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("انتهت مهلة انتظار تحديث الإحصائيات.")
                _CONDITION.wait(timeout=min(1.0, remaining))
            return load_snapshot()
        _REFRESHING = True
        _LAST_REFRESH_STARTED = time.time()
    return _perform_refresh()


def refresh_in_background(force: bool = False) -> bool:
    global _REFRESHING, _LAST_REFRESH_STARTED
    current = load_snapshot()
    age = time.time() - float(current.get("updated_at_epoch") or 0)
    with _CONDITION:
        if _REFRESHING:
            return False
        if not force and age < REFRESH_SECONDS:
            return False
        _REFRESHING = True
        _LAST_REFRESH_STARTED = time.time()

    def worker() -> None:
        try:
            _perform_refresh()
        except Exception as error:
            print(f"Analytics refresh failed: {error}")

    threading.Thread(target=worker, name="analytics-refresh", daemon=True).start()
    return True


def refresh_state() -> Dict[str, Any]:
    with _LOCK:
        return {
            "refreshing": _REFRESHING,
            "refresh_started_at": _LAST_REFRESH_STARTED,
            "last_error": _LAST_ERROR,
            "refresh_interval_seconds": REFRESH_SECONDS,
        }
