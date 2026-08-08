import calendar
import hashlib
import json
import sys
import time

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


# ==========================================
# Main Project Path
# ==========================================

PROJECT_DIRECTORY = Path(
    __file__
).resolve().parent.parent

if str(PROJECT_DIRECTORY) not in sys.path:

    sys.path.insert(
        0,
        str(PROJECT_DIRECTORY)
    )


# ==========================================
# Project Imports
# ==========================================

from opost_client import OpostClient
from business_service import build_row
from excel import export_businesses


# ==========================================
# Rules
# ==========================================


SHIPMENTS_CACHE_DIR = PROJECT_DIRECTORY / "cache" / "shipments"
SHIPMENTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
SHIPMENTS_CACHE_TTL_SECONDS = 24 * 60 * 60
BUSINESSES_CACHE_DIR = PROJECT_DIRECTORY / "cache" / "businesses"
BUSINESSES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
BUSINESSES_CACHE_TTL_SECONDS = 24 * 60 * 60

DEFAULT_MAXIMUM_RETURN_PERCENTAGE = 10.0
DEFAULT_INCUBATION_DAYS = 40
DEFAULT_MINIMUM_DELIVERY_PERCENTAGE = 30.0

FAST_LOADING_CONCURRENCY = 16


# ==========================================
# Safe Number Helpers
# ==========================================

def safe_int(
    value: Any,
    default: int = 0
) -> int:
    """
    تحويل القيمة إلى رقم صحيح بأمان.
    """

    try:

        return int(
            float(
                value
            )
        )

    except (
        TypeError,
        ValueError,
        OverflowError
    ):

        return default


def safe_float(
    value: Any,
    default: float = 0.0
) -> float:
    """
    تحويل القيمة إلى رقم عشري بأمان.
    """

    try:

        return float(
            value
        )

    except (
        TypeError,
        ValueError,
        OverflowError
    ):

        return default


# ==========================================
# Date Helpers
# ==========================================

def validate_date(
    value: Any
) -> datetime:
    """
    التحقق من التاريخ وإعادته كـ datetime.
    """

    text = str(
        value or ""
    ).strip()

    if not text:

        raise ValueError(
            "التاريخ مطلوب."
        )

    try:

        return datetime.strptime(
            text,
            "%Y-%m-%d"
        )

    except ValueError as error:

        raise ValueError(
            "صيغة التاريخ يجب أن تكون YYYY-MM-DD."
        ) from error


def resolve_report_period(
    start_date: Any = None,
    end_date: Any = None,
    year: Any = None,
    month: Any = None
) -> Dict[str, str]:
    """
    دعم اختيار فترة مخصصة أو سنة وشهر.
    """

    start_text = str(
        start_date or ""
    ).strip()

    end_text = str(
        end_date or ""
    ).strip()

    # ======================================
    # Custom Date Range
    # ======================================

    if start_text or end_text:

        if not start_text or not end_text:

            raise ValueError(
                "يجب اختيار تاريخ البداية وتاريخ النهاية."
            )

        start_value = validate_date(
            start_text
        )

        end_value = validate_date(
            end_text
        )

        if start_value > end_value:

            raise ValueError(
                "تاريخ البداية يجب أن يكون قبل تاريخ النهاية."
            )

        return {
            "start_date":
                start_value.strftime(
                    "%Y-%m-%d"
                ),

            "end_date":
                end_value.strftime(
                    "%Y-%m-%d"
                ),

            "year":
                start_value.strftime(
                    "%Y"
                ),

            "month":
                start_value.strftime(
                    "%m"
                ),
        }

    # ======================================
    # Year And Month
    # ======================================

    year_text = str(
        year or ""
    ).strip()

    month_text = str(
        month or ""
    ).strip()

    if not year_text or not month_text:

        raise ValueError(
            "يرجى اختيار تاريخ البداية وتاريخ النهاية."
        )

    try:

        year_number = int(
            year_text
        )

        month_number = int(
            month_text
        )

    except (
        TypeError,
        ValueError
    ) as error:

        raise ValueError(
            "السنة أو الشهر غير صحيح."
        ) from error

    if not 1 <= month_number <= 12:

        raise ValueError(
            "الشهر يجب أن يكون من 1 إلى 12."
        )

    last_day = calendar.monthrange(
        year_number,
        month_number
    )[1]

    return {
        "start_date":
            (
                f"{year_number:04d}-"
                f"{month_number:02d}-01"
            ),

        "end_date":
            (
                f"{year_number:04d}-"
                f"{month_number:02d}-"
                f"{last_day:02d}"
            ),

        "year":
            str(
                year_number
            ),

        "month":
            f"{month_number:02d}",
    }


# ==========================================
# Business Helpers
# ==========================================

def business_id_value(
    business: Dict[str, Any]
) -> str:
    """
    توحيد Business ID إلى نص.
    """

    business_id = business.get(
        "id"
    )

    if business_id is None:

        return ""

    return str(
        business_id
    ).strip()


def business_name_value(
    business: Dict[str, Any]
) -> str:
    """
    قراءة اسم الحساب.
    """

    return str(
        business.get("display")
        or business.get("name")
        or ""
    ).strip()


def normalize_shipments_map(
    shipments_map: Any
) -> Dict[str, Any]:
    """
    توحيد نتيجة تحميل الشحنات أو الإحصائيات المجمعة.

    بعض النسخ تعيد المفتاح كـ int،
    وبعضها تعيده كـ string.
    """

    normalized: Dict[str, Any] = {}

    if not isinstance(
        shipments_map,
        dict
    ):

        return normalized

    for business_id, shipments in shipments_map.items():

        business_key = str(
            business_id
        ).strip()

        if isinstance(shipments, (list, dict)):
            normalized[business_key] = shipments
        else:
            normalized[business_key] = []

    return normalized


# ==========================================
# Shipment Loading
# ==========================================

def load_period_businesses_cached(
    client: OpostClient,
    start_date: str,
    end_date: str,
) -> List[Dict[str, Any]]:
    """Reuse the exact period business list instead of downloading it repeatedly."""
    key = hashlib.sha256(f"{start_date}|{end_date}|2.3.0".encode("utf-8")).hexdigest()
    path = BUSINESSES_CACHE_DIR / f"{key}.json"
    try:
        if path.exists() and time.time() - path.stat().st_mtime <= BUSINESSES_CACHE_TTL_SECONDS:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                print("✅ Using valid businesses cache")
                return [item for item in payload if isinstance(item, dict)]
    except (OSError, json.JSONDecodeError):
        pass

    businesses = client.get_businesses(start_date, end_date)
    businesses = [item for item in businesses if isinstance(item, dict)]
    try:
        temporary = path.with_name(f"{path.name}.{int(time.time() * 1000)}.tmp")
        temporary.write_text(json.dumps(businesses, ensure_ascii=False, default=str), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass
    return businesses


def load_business_shipments(
    client: OpostClient,
    businesses: List[Dict[str, Any]],
    start_date: str,
    end_date: str,
    page_progress_callback=None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Load the period shipments once, then group them locally by Business ID.

    OPOST was proven to ignore the ``business.name`` filter in the current
    account. Calling that endpoint once per business therefore duplicated the
    work and then still required a complete period download. Version 2.1.2
    deliberately skips the per-business requests and performs one verified
    period download only.
    """

    if not businesses:
        return {}

    business_ids = [
        business.get("id")
        for business in businesses
        if isinstance(business, dict) and business.get("id") is not None
    ]

    print()
    print("=" * 60)
    print("LOADING PERIOD SHIPMENTS ONCE")
    print("Businesses:", len(business_ids))
    print("Period:", start_date, "to", end_date)
    print("=" * 60)

    cache_key_source = json.dumps(
        {
            "start_date": start_date,
            "end_date": end_date,
            "business_ids": sorted(str(value) for value in business_ids),
            "strategy": "persistent-http-aggregated-period",
            "version": "2.3.2-compact-fast-pages",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    cache_key = hashlib.sha256(cache_key_source.encode("utf-8")).hexdigest()
    cache_path = SHIPMENTS_CACHE_DIR / f"{cache_key}.json"

    normalized: Optional[Dict[str, List[Dict[str, Any]]]] = None
    if cache_path.exists():
        cache_age = time.time() - cache_path.stat().st_mtime
        if cache_age <= SHIPMENTS_CACHE_TTL_SECONDS:
            try:
                cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
                normalized = normalize_shipments_map(cached_payload)
                print("✅ Using valid shipments cache")
                if page_progress_callback is not None:
                    page_progress_callback(1, 1)
            except (OSError, json.JSONDecodeError):
                normalized = None

    if normalized is None:
        shipments_by_business = client.get_shipment_statistics_grouped_by_business(
            start_date=start_date,
            end_date=end_date,
            business_ids=business_ids,
            concurrency=18,
            progress_callback=page_progress_callback,
        )
        normalized = normalize_shipments_map(shipments_by_business)

        try:
            temp_path = cache_path.with_suffix(".tmp")
            temp_path.write_text(
                json.dumps(normalized, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            temp_path.replace(cache_path)
        except OSError:
            pass

    # Keep every selected business in the map. A missing key means a confirmed
    # zero shipments after the complete period download, not a skipped account.
    for business_id in business_ids:
        normalized.setdefault(
            str(business_id).strip(),
            {
                "__aggregated__": True, "total": 0, "closed": 0,
                "delivered": 0, "returned": 0, "cancelled": 0,
                "unknown": 0, "status_counts": {},
            },
        )

    print()
    print("=" * 60)
    print("SHIPMENTS LOADING COMPLETED")
    print("Businesses In Result:", len(normalized))
    print("=" * 60)

    return normalized


# ==========================================
# Sorting
# ==========================================

def sort_report_rows(
    all_accounts: List[Dict[str, Any]],
    best_accounts: List[Dict[str, Any]],
    follow_up_accounts: List[Dict[str, Any]],
    no_shipments: List[Dict[str, Any]]
) -> None:
    """
    ترتيب صفحات التقرير.
    """

    all_accounts.sort(
        key=lambda row: (
            safe_float(
                row.get(
                    "Delivered %",
                    0
                )
            ),
            safe_int(
                row.get(
                    "Shipments",
                    0
                )
            ),
        ),
        reverse=True
    )

    best_accounts.sort(
        key=lambda row: (
            safe_float(
                row.get(
                    "Delivered %",
                    0
                )
            ),
            safe_int(
                row.get(
                    "Shipments",
                    0
                )
            ),
        ),
        reverse=True
    )

    follow_up_accounts.sort(
        key=lambda row: (
            safe_float(
                row.get(
                    "Returned %",
                    0
                )
            ),
            safe_int(
                row.get(
                    "Returned",
                    0
                )
            ),
            safe_int(
                row.get(
                    "Shipments",
                    0
                )
            ),
        ),
        reverse=True
    )

    no_shipments.sort(
        key=lambda row: str(
            row.get(
                "Created At",
                ""
            )
        ),
        reverse=True
    )


# ==========================================
# Account Classification
# ==========================================

def effective_delivery_percentage(row: Dict[str, Any]) -> float:
    """OPOST business performance: Closed is successful; Delivered is a returned parcel."""
    shipments = safe_int(row.get("Shipments"), 0)
    if shipments <= 0:
        return 0.0
    closed = safe_int(row.get("Closed"), 0)
    return round((closed / shipments) * 100, 2)


def effective_return_percentage(row: Dict[str, Any]) -> float:
    """Use OPOST Delivered as the negative/returned-to-business indicator."""
    shipments = safe_int(row.get("Shipments"), 0)
    if shipments <= 0:
        return 0.0
    delivered = safe_int(row.get("Delivered"), 0)
    return round((delivered / shipments) * 100, 2)


def _within_incubation(row: Dict[str, Any], incubation_days: Any) -> bool:
    age = safe_int(row.get("Account Age"), -1)
    maximum = max(1, safe_int(incubation_days, DEFAULT_INCUBATION_DAYS))
    return 1 <= age <= maximum


def classify_account(
    row: Dict[str, Any],
    minimum_delivery: Any,
    minimum_shipments: Any,
    maximum_return: Any,
    incubation_days: Any,
) -> tuple[str, str]:
    """Apply the requested classification priority exactly."""
    shipments = safe_int(row.get("Shipments"), 0)
    delivery = effective_delivery_percentage(row)
    returned = effective_return_percentage(row)
    min_delivery = max(0.0, min(100.0, safe_float(minimum_delivery, DEFAULT_MINIMUM_DELIVERY_PERCENTAGE)))
    min_shipments = max(0, safe_int(minimum_shipments, 30))
    max_return = max(0.0, min(100.0, safe_float(maximum_return, 10.0)))

    if not _within_incubation(row, incubation_days):
        return "Normal", ""
    if shipments == 0:
        return "No Shipments", ""
    if shipments < min_shipments:
        return "Normal", ""
    low_delivery = delivery < min_delivery
    high_return = returned > max_return
    return_is_best = returned < max_return

    if delivery >= min_delivery and return_is_best:
        return "Best Account", ""
    if low_delivery and high_return:
        return "Need Follow Up", "Low Delivery & High Return Rate"
    if low_delivery:
        return "Need Follow Up", "Low Delivery Rate"
    if high_return:
        return "Need Follow Up", "High Return Rate"
    # Exactly equal to the maximum return threshold is neither below the Best
    # limit nor above the Follow-Up limit, so it remains Normal.
    return "Normal", ""


def build_follow_up_reason(
    row: Dict[str, Any],
    minimum_delivery: Any,
    minimum_shipments: Any = 30,
    maximum_return: Any = 10,
    incubation_days: Any = 40,
) -> str:
    category, reason = classify_account(
        row, minimum_delivery, minimum_shipments, maximum_return, incubation_days
    )
    return reason if category == "Need Follow Up" else ""


def is_follow_up_account(
    row: Dict[str, Any],
    minimum_delivery: Any,
    minimum_shipments: Any = 30,
    maximum_return: Any = 10,
    incubation_days: Any = 40,
) -> bool:
    return classify_account(
        row, minimum_delivery, minimum_shipments, maximum_return, incubation_days
    )[0] == "Need Follow Up"


def is_best_account(
    row: Dict[str, Any],
    minimum_delivery: Any,
    minimum_shipments: Any,
    maximum_return: Any = 10,
    incubation_days: Any = 40,
) -> bool:
    return classify_account(
        row, minimum_delivery, minimum_shipments, maximum_return, incubation_days
    )[0] == "Best Account"


def _notify_progress(
    callback: Optional[Callable[[int, str, Dict[str, Any]], None]],
    progress: int,
    stage: str,
    **details: Any,
) -> None:
    if callback is not None:
        callback(max(0, min(int(progress), 99)), stage, details)


# ==========================================
# Summary
# ==========================================

def build_summary(
    total_accounts: int,
    accounts_with_shipments: int,
    accounts_without_shipments: int,
    best_accounts: List[Dict[str, Any]],
    follow_up_accounts: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    تجهيز الأرقام التي تظهر في الداشبورد.
    """

    if total_accounts > 0:

        with_shipments_percentage = round(
            (
                accounts_with_shipments
                / total_accounts
            )
            * 100,
            2
        )

        without_shipments_percentage = round(
            (
                accounts_without_shipments
                / total_accounts
            )
            * 100,
            2
        )

    else:

        with_shipments_percentage = 0.0
        without_shipments_percentage = 0.0

    return {
        "total_accounts":
            total_accounts,

        "accounts_with_shipments":
            accounts_with_shipments,

        "accounts_with_shipments_percentage":
            with_shipments_percentage,

        "accounts_without_shipments":
            accounts_without_shipments,

        "accounts_without_shipments_percentage":
            without_shipments_percentage,

        "best_accounts":
            len(
                best_accounts
            ),

        "follow_up_accounts":
            len(
                follow_up_accounts
            ),
    }


# ==========================================
# Generate Monthly Report
# ==========================================

def generate_monthly_report(
    year: Any = None,
    month: Any = None,
    start_date: Any = None,
    end_date: Any = None,
    minimum_delivery: Any = DEFAULT_MINIMUM_DELIVERY_PERCENTAGE,
    minimum_shipments: Any = 30,
    return_percentage: Any = 10,
    incubation_days: Any = 40,
    progress_callback: Optional[Callable[[int, str, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """
    إنشاء التقرير الكامل.
    """

    period = resolve_report_period(
        start_date=start_date,
        end_date=end_date,
        year=year,
        month=month,
    )

    start_date = period[
        "start_date"
    ]

    end_date = period[
        "end_date"
    ]

    report_year = period[
        "year"
    ]

    report_month = period[
        "month"
    ]

    minimum_delivery_value = safe_float(
        minimum_delivery,
        DEFAULT_MINIMUM_DELIVERY_PERCENTAGE
    )

    minimum_shipments_value = safe_int(
        minimum_shipments,
        30
    )

    maximum_return_value = safe_float(return_percentage, 10.0)
    incubation_days_value = safe_int(incubation_days, 40)

    if minimum_delivery_value < 0:

        minimum_delivery_value = 0.0

    if minimum_delivery_value > 100:

        minimum_delivery_value = 100.0

    if minimum_shipments_value < 0:

        minimum_shipments_value = 0

    maximum_return_value = max(0.0, min(100.0, maximum_return_value))
    incubation_days_value = max(1, incubation_days_value)

    all_accounts: List[
        Dict[str, Any]
    ] = []

    best_accounts: List[
        Dict[str, Any]
    ] = []

    follow_up_accounts: List[
        Dict[str, Any]
    ] = []

    no_shipments: List[
        Dict[str, Any]
    ] = []

    accounts_with_shipments = 0
    accounts_without_shipments = 0

    client = OpostClient()

    started_at = datetime.now()

    try:

        _notify_progress(progress_callback, 5, "تشغيل المتصفح")
        client.start()
        client.deadline_monotonic = time.monotonic() + 600

        _notify_progress(progress_callback, 12, "تسجيل الدخول إلى OPOST")
        client.login()
        _notify_progress(progress_callback, 20, "تم تسجيل الدخول إلى OPOST")

        print()
        print("=" * 60)
        print("REPORT STARTED")
        print(
            "Period:",
            start_date,
            "to",
            end_date
        )
        print(
            "Minimum Delivery:",
            minimum_delivery_value
        )
        print(
            "Minimum Shipments:",
            minimum_shipments_value
        )
        print("=" * 60)

        # ==================================
        # Load Businesses
        # ==================================

        _notify_progress(progress_callback, 25, "تحميل حسابات OPOST")
        businesses = load_period_businesses_cached(
            client,
            start_date,
            end_date,
        )

        if not isinstance(
            businesses,
            list
        ):

            businesses = []

        businesses = [
            business
            for business in businesses
            if isinstance(
                business,
                dict
            )
        ]

        total_accounts = len(
            businesses
        )

        print()
        print(
            "Businesses Found:",
            total_accounts
        )

        # ==================================
        # Load Shipments
        # ==================================

        _notify_progress(progress_callback, 40, "تحميل شحنات الفترة", accounts=total_accounts)

        last_page_progress = {"value": -1}
        def shipment_page_progress(loaded_pages: int, total_pages: int) -> None:
            if total_pages <= 0:
                return
            page_percent = 40 + int((loaded_pages / total_pages) * 24)
            # Writing job JSON on OneDrive for every page was a large hidden cost.
            # Persist only meaningful percentage changes and the final update.
            if loaded_pages != total_pages and page_percent <= last_page_progress["value"]:
                return
            last_page_progress["value"] = page_percent
            _notify_progress(
                progress_callback, min(page_percent, 64),
                f"تحميل شحنات الفترة ({loaded_pages}/{total_pages} صفحة)",
                completed=loaded_pages, total=total_pages,
            )

        shipments_by_business = (
            load_business_shipments(
                client=client,
                businesses=businesses,
                start_date=start_date,
                end_date=end_date,
                page_progress_callback=shipment_page_progress,
            )
        )

        # ==================================
        # Build Report Rows
        # ==================================

        print()
        print("=" * 60)
        print("BUILDING REPORT ROWS")
        print("=" * 60)

        for index, business in enumerate(
            businesses,
            start=1
        ):

            if total_accounts and (index == total_accounts or index == 1 or index % 10 == 0):
                row_progress = 65 + int((index / total_accounts) * 24)
                _notify_progress(
                    progress_callback, row_progress, "تحليل الحسابات",
                    completed=index, total=total_accounts,
                )

            business_id = business_id_value(
                business
            )

            business_name = business_name_value(
                business
            )

            shipments = (
                shipments_by_business.get(
                    business_id,
                    []
                )
            )

            if not isinstance(shipments, (list, dict)):
                shipments = []

            row = build_row(
                business,
                shipments,
                reference_date=datetime.strptime(end_date, "%Y-%m-%d")
            )
            

            shipments_count = safe_int(
                row.get(
                    "Shipments",
                    0
                )
            )

            return_percentage_value = safe_float(
                row.get(
                    "Returned %",
                    0
                )
            )

            row["Successful Delivery %"] = effective_delivery_percentage(row)
            row["Performance Return %"] = effective_return_percentage(row)
            row["Status"] = (
                "In Incubation" if _within_incubation(row, incubation_days_value)
                else "Out of Incubation"
            )
            category, follow_up_reason = classify_account(
                row,
                minimum_delivery_value,
                minimum_shipments_value,
                maximum_return_value,
                incubation_days_value,
            )
            row["Category"] = category
            row["Follow Up"] = "YES" if category == "Need Follow Up" else "NO"
            row["Follow Up Reason"] = follow_up_reason

            all_accounts.append(row)

            # ==============================
            # With / Without Shipments
            # ==============================

            if shipments_count <= 0:

                accounts_without_shipments += 1

                no_shipments.append(
                    row.copy()
                )

            else:

                accounts_with_shipments += 1

            # ==============================
            # Classified Accounts
            # ==============================

            if row["Category"] == "Best Account":
                best_accounts.append(row.copy())
            elif row["Category"] == "Need Follow Up":
                follow_up_accounts.append(row.copy())


        # ==================================
        # Sorting
        # ==================================

        sort_report_rows(
            all_accounts=all_accounts,
            best_accounts=best_accounts,
            follow_up_accounts=follow_up_accounts,
            no_shipments=no_shipments,
        )

        # ==================================
        # Summary
        # ==================================

        summary = build_summary(
            total_accounts=
                total_accounts,

            accounts_with_shipments=
                accounts_with_shipments,

            accounts_without_shipments=
                accounts_without_shipments,

            best_accounts=
                best_accounts,

            follow_up_accounts=
                follow_up_accounts,
        )

        finished_at = datetime.now()

        elapsed_seconds = round(
            (
                finished_at
                - started_at
            ).total_seconds(),
            2
        )

        summary[
            "generation_seconds"
        ] = elapsed_seconds

        summary[
            "minimum_delivery"
        ] = minimum_delivery_value

        summary[
            "minimum_shipments"
        ] = minimum_shipments_value

        summary["maximum_return"] = maximum_return_value
        summary["incubation_days"] = incubation_days_value

        summary[
            "follow_up_return_percentage"
        ] = maximum_return_value

        print()
        print("=" * 60)
        print("REPORT SUMMARY")
        print("=" * 60)

        print(
            "Accounts Created:",
            summary[
                "total_accounts"
            ]
        )

        print(
            "Accounts With Shipments:",
            summary[
                "accounts_with_shipments"
            ],
            f"({summary['accounts_with_shipments_percentage']}%)"
        )

        print(
            "Accounts Without Shipments:",
            summary[
                "accounts_without_shipments"
            ],
            f"({summary['accounts_without_shipments_percentage']}%)"
        )

        print(
            "Best Accounts:",
            summary[
                "best_accounts"
            ]
        )

        print(
            "Need Follow Up:",
            summary[
                "follow_up_accounts"
            ]
        )

        print(
            "Generation Time:",
            elapsed_seconds,
            "seconds"
        )

        print("=" * 60)

        # ==================================
        # Export Excel
        # ==================================

        _notify_progress(progress_callback, 92, "إنشاء ملف Excel")
        file_path = export_businesses(
            summary=summary,
            all_accounts=all_accounts,
            best_accounts=best_accounts,
            follow_up_accounts=
                follow_up_accounts,
            no_shipments=no_shipments,
            start_date=start_date,
            end_date=end_date,
        )

        return {
            "success":
                True,

            "summary":
                summary,

            "file_path":
                file_path,

            "follow_up_accounts":
                follow_up_accounts,

            "all_accounts":
                all_accounts,

            "best_accounts":
                best_accounts,

            "no_shipments":
                no_shipments,

            "year":
                report_year,

            "month":
                report_month,

            "start_date":
                start_date,

            "end_date":
                end_date,

            "generation_seconds":
                elapsed_seconds,
        }

    except Exception as error:

        print()
        print("=" * 60)
        print("REPORT ERROR")
        print(
            type(error).__name__,
            ":",
            error
        )
        print("=" * 60)

        return {
            "success":
                False,

            "error":
                str(
                    error
                ),

            "error_type":
                type(
                    error
                ).__name__,

            "start_date":
                start_date,

            "end_date":
                end_date,
        }

    finally:

        try:

            client.close()

        except Exception as close_error:

            print(
                "Client Close Error:",
                close_error
            )