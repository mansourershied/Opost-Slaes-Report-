from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional


# ==========================================
# Shipment Status Groups
# ==========================================

# الحالات التي نعتبرها شحنة مغلقة/مكتملة بنجاح.
CLOSED_STATUSES = {
    "closed",
    "close",
    "completed",
    "complete",
    "تم الاغلاق",
    "تم الإغلاق",
    "مغلق",
    "مغلقة",
    "مكتمل",
    "مكتملة",
}

# الحالات التي يعيدها النظام باسم Delivered.
DELIVERED_STATUSES = {
    "delivered",
    "delivery",
    "تم التسليم",
    "مسلم",
    "مسلّم",
    "مسلمة",
}

# حالات الرواجع، لا تدخل ضمن Delivered.
RETURNED_STATUSES = {
    "returned",
    "return",
    "returned to sender",
    "return to sender",
    "returned_to_sender",
    "تم الارجاع",
    "تم الإرجاع",
    "راجع",
    "راجعة",
    "رواجع",
}

# حالات الإلغاء.
CANCELLED_STATUSES = {
    "cancelled",
    "canceled",
    "cancel",
    "ملغي",
    "ملغى",
    "ملغاة",
    "تم الالغاء",
    "تم الإلغاء",
}


# ==========================================
# Basic Helpers
# ==========================================

def clean_text(value: Any) -> str:
    """
    تحويل أي قيمة إلى نص نظيف.
    """

    if value is None:
        return ""

    return str(value).strip()


def normalize_text(value: Any) -> str:
    """
    توحيد النص للمقارنة.
    """

    return (
        clean_text(value)
        .replace("_", " ")
        .replace("-", " ")
        .replace("ـ", "")
        .lower()
    )


def safe_int(
    value: Any,
    default: int = 0
) -> int:
    """
    تحويل القيمة إلى عدد صحيح بأمان.
    """

    try:
        return int(float(value))

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
    تحويل القيمة إلى عدد عشري بأمان.
    """

    try:
        return float(value)

    except (
        TypeError,
        ValueError,
        OverflowError
    ):
        return default


# ==========================================
# Field Helpers
# ==========================================

def get_account_fields(
    account: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    إرجاع fields بصورة آمنة.
    """

    fields = account.get(
        "fields",
        []
    )

    if not isinstance(
        fields,
        list
    ):
        return []

    return [
        field
        for field in fields
        if isinstance(
            field,
            dict
        )
    ]


def find_field(
    account: Dict[str, Any],
    field_name: str
) -> Optional[Dict[str, Any]]:
    """
    البحث عن حقل داخل الحساب.
    """

    target_name = normalize_text(
        field_name
    )

    for field in get_account_fields(
        account
    ):

        current_name = normalize_text(
            field.get("attribute")
            or field.get("name")
            or field.get("related_name")
            or field.get("label")
        )

        if current_name == target_name:
            return field

    return None


def extract_nested_value(
    value: Any
) -> Any:
    """
    استخراج قيمة قابلة للعرض من القيم المتداخلة.
    """

    if value is None:
        return ""

    if isinstance(
        value,
        dict
    ):

        possible_keys = (
            "value",
            "label",
            "name",
            "display",
            "title",
            "id",
        )

        for key in possible_keys:

            nested_value = value.get(
                key
            )

            if nested_value not in (
                None,
                ""
            ):
                return extract_nested_value(
                    nested_value
                )

        return ""

    if isinstance(
        value,
        list
    ):

        extracted_items = []

        for item in value:

            extracted_item = extract_nested_value(
                item
            )

            if extracted_item not in (
                None,
                ""
            ):

                extracted_items.append(
                    clean_text(
                        extracted_item
                    )
                )

        return ", ".join(
            extracted_items
        )

    return value


def field_value(
    account: Dict[str, Any],
    field_name: str
) -> Any:
    """
    قراءة قيمة حقل من بيانات الحساب.
    """

    if not isinstance(
        account,
        dict
    ):
        return ""

    normalized_name = normalize_text(
        field_name
    )

    # الاسم غالبًا موجود في display.
    if normalized_name in {
        "name",
        "business name",
        "display",
    }:

        display_value = account.get(
            "display"
        )

        if display_value not in (
            None,
            ""
        ):

            return clean_text(
                display_value
            )

    # أحيانًا تكون القيمة مباشرة داخل الحساب.
    direct_value = account.get(
        field_name
    )

    if direct_value not in (
        None,
        ""
    ):

        return extract_nested_value(
            direct_value
        )

    field = find_field(
        account,
        field_name
    )

    if field is None:
        return ""

    # القيمة الأساسية.
    value = field.get(
        "value"
    )

    extracted_value = extract_nested_value(
        value
    )

    if extracted_value not in (
        None,
        ""
    ):
        return extracted_value

    # بعض الحقول تستعمل value_label.
    value_label = field.get(
        "value_label"
    )

    if value_label not in (
        None,
        ""
    ):
        return extract_nested_value(
            value_label
        )

    # الحقول المرتبطة قد تكون داخل related.
    related = field.get(
        "related"
    )

    if related not in (
        None,
        ""
    ):
        return extract_nested_value(
            related
        )

    return ""


def related_name(
    account: Dict[str, Any],
    field_name: str
) -> str:
    """
    قراءة اسم حقل مرتبط مثل الموظف أو المكتب.
    """

    field = find_field(
        account,
        field_name
    )

    if field is None:

        direct_value = field_value(
            account,
            field_name
        )

        return clean_text(
            direct_value
        )

    related = field.get(
        "related"
    )

    related_value = extract_nested_value(
        related
    )

    if related_value not in (
        None,
        ""
    ):
        return clean_text(
            related_value
        )

    value = extract_nested_value(
        field.get("value")
    )

    return clean_text(
        value
    )


def related_display_name(
    account: Dict[str, Any],
    *field_names: str
) -> str:
    """Return a human-readable related person/office name instead of an ID."""

    def walk(value: Any) -> str:
        if isinstance(value, dict):
            # OPOST resource lists often contain nested ``fields`` objects.
            fields = value.get("fields")
            if isinstance(fields, list):
                for item in fields:
                    if not isinstance(item, dict):
                        continue
                    key = normalize_text(item.get("name") or item.get("related_name") or item.get("attribute"))
                    if key in {"name", "full name", "display", "title"}:
                        candidate = item.get("value_label")
                        if candidate in (None, ""):
                            candidate = item.get("value")
                        text = clean_text(extract_nested_value(candidate))
                        if text and not text.isdigit():
                            return text
            for key in ("name", "full_name", "display", "title", "label", "value_label"):
                text = clean_text(value.get(key))
                if text and not text.isdigit():
                    return text
            for key in ("related", "value", "users", "data"):
                text = walk(value.get(key))
                if text:
                    return text
        elif isinstance(value, list):
            for item in value:
                text = walk(item)
                if text:
                    return text
        else:
            text = clean_text(value)
            if text and not text.isdigit():
                return text
        return ""

    for field_name in field_names:
        field = find_field(account, field_name)
        if field is not None:
            for candidate in (field.get("related"), field.get("value"), field.get("value_label")):
                text = walk(candidate)
                if text:
                    return text
    return ""


# ==========================================
# Date Helpers
# ==========================================

def parse_datetime(
    value: Any
) -> Optional[datetime]:
    """
    قراءة التاريخ من أكثر من صيغة محتملة.
    """

    if isinstance(
        value,
        datetime
    ):
        return value

    text = clean_text(
        value
    )

    if not text:
        return None

    # إزالة Z الخاصة بـ UTC.
    if text.endswith("Z"):
        text = text[:-1]

    # تجربة ISO أولًا.
    try:

        parsed_value = datetime.fromisoformat(
            text
        )

        # إزالة timezone لمنع خطأ المقارنة.
        if parsed_value.tzinfo is not None:

            parsed_value = parsed_value.replace(
                tzinfo=None
            )

        return parsed_value

    except ValueError:
        pass

    date_formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
    )

    for date_format in date_formats:

        try:

            return datetime.strptime(
                text,
                date_format
            )

        except ValueError:
            continue

    return None


def calculate_age(
    created_at: Any,
    reference_date: Optional[datetime] = None
) -> Optional[int]:
    """
    حساب عمر الحساب بالأيام.

    يعيد None عند وجود تاريخ غير صالح بدل إعادة 0؛
    لأن إعادة 0 كانت تجعل الحساب يظهر كأنه حساب جديد.
    """

    created_date = parse_datetime(
        created_at
    )

    if created_date is None:
        return None

    current_date = (
        reference_date
        if reference_date is not None
        else datetime.now()
    )

    age_days = (
        current_date.date()
        - created_date.date()
    ).days + 1

    return age_days


def incubation_status(
    account_age: Optional[int]
) -> str:
    """
    حساب الحضانة من اليوم الأول وحتى اليوم الأربعين.
    """

    if account_age is None:
        return "UNKNOWN"

    if 1 <= account_age <= 40:
        return "YES"

    return "NO"


# ==========================================
# Shipment Helpers
# ==========================================

def get_shipment_fields(
    shipment: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    إرجاع حقول الشحنة بصورة آمنة.
    """

    fields = shipment.get(
        "fields",
        []
    )

    if not isinstance(
        fields,
        list
    ):
        return []

    return [
        field
        for field in fields
        if isinstance(
            field,
            dict
        )
    ]


def shipment_field_value(
    shipment: Dict[str, Any],
    field_names: Iterable[str]
) -> Any:
    """
    قراءة حقل من الشحنة باستخدام أكثر من اسم محتمل.
    """

    normalized_names = {
        normalize_text(
            field_name
        )
        for field_name in field_names
    }

    # بعض الاستجابات تضع status مباشرة.
    for field_name in field_names:

        direct_value = shipment.get(
            field_name
        )

        if direct_value not in (
            None,
            ""
        ):

            return extract_nested_value(
                direct_value
            )

    for field in get_shipment_fields(
        shipment
    ):

        current_name = normalize_text(
            field.get("attribute")
            or field.get("name")
            or field.get("related_name")
            or field.get("label")
        )

        if current_name not in normalized_names:
            continue

        value_label = extract_nested_value(
            field.get("value_label")
        )

        if value_label not in (
            None,
            ""
        ):
            return value_label

        value = extract_nested_value(
            field.get("value")
        )

        if value not in (
            None,
            ""
        ):
            return value

        display_value = extract_nested_value(
            field.get("display")
        )

        if display_value not in (
            None,
            ""
        ):
            return display_value


        if value_label not in (
            None,
            ""
        ):
            return value_label

        related = extract_nested_value(
            field.get("related")
        )

        if related not in (
            None,
            ""
        ):
            return related

    return ""


def shipment_status(
    shipment: Dict[str, Any]
) -> str:
    """
    قراءة وتوحيد حالة الشحنة.
    """

    status = shipment_field_value(
        shipment,
        (
            "status",
            "shipment_status",
            "shipment status",
            "state",
        )
    )

    return normalize_text(
        status
    )


def status_matches(
    current_status: str,
    accepted_statuses: Iterable[str]
) -> bool:
    """
    مقارنة حالة الشحنة بقائمة الحالات المقبولة.
    """

    normalized_current = normalize_text(
        current_status
    )

    if not normalized_current:
        return False

    normalized_accepted = {
        normalize_text(
            status
        )
        for status in accepted_statuses
    }

    if normalized_current in normalized_accepted:
        return True

    # دعم حالات مثل:
    # delivered shipment
    # returned to sender - completed
    for accepted_status in normalized_accepted:

        if (
            normalized_current.startswith(
                accepted_status + " "
            )
            or normalized_current.endswith(
                " " + accepted_status
            )
        ):
            return True

    return False


def shipment_statistics(
    shipments: Any
) -> Dict[str, Any]:
    """حساب إحصائيات الشحنات أو قبول ملخص مُجمّع مسبقًا."""

    if isinstance(shipments, dict) and shipments.get("__aggregated__"):
        total_count = int(shipments.get("total", 0) or 0)
        closed_count = int(shipments.get("closed", 0) or 0)
        delivered_count = int(shipments.get("delivered", 0) or 0)
        returned_count = int(shipments.get("returned", 0) or 0)
        cancelled_count = int(shipments.get("cancelled", 0) or 0)
        unknown_count = int(shipments.get("unknown", 0) or 0)
        status_counts = dict(shipments.get("status_counts", {}) or {})
        if total_count <= 0:
            closed_rate = delivered_rate = returned_rate = cancelled_rate = 0.0
        else:
            closed_rate = round(closed_count / total_count * 100, 2)
            delivered_rate = round(delivered_count / total_count * 100, 2)
            returned_rate = round(returned_count / total_count * 100, 2)
            cancelled_rate = round(cancelled_count / total_count * 100, 2)
        return {
            "total": total_count, "closed": closed_count,
            "delivered": delivered_count, "returned": returned_count,
            "cancelled": cancelled_count, "unknown": unknown_count,
            "closed_rate": closed_rate, "delivered_rate": delivered_rate,
            "returned_rate": returned_rate, "cancelled_rate": cancelled_rate,
            "status_counts": status_counts,
        }

    if not isinstance(shipments, list):
        shipments = []

    total_count = len(
        shipments
    )

    closed_count = 0
    delivered_count = 0
    returned_count = 0
    cancelled_count = 0
    unknown_count = 0

    status_counts: Dict[str, int] = {}

    for shipment in shipments:

        if not isinstance(
            shipment,
            dict
        ):
            unknown_count += 1
            continue

        status = shipment_status(
            shipment
        )

        display_status = (
            status
            if status
            else "unknown"
        )

        status_counts[
            display_status
        ] = (
            status_counts.get(
                display_status,
                0
            )
            + 1
        )

        if status_matches(
            status,
            CLOSED_STATUSES
        ):

            closed_count += 1

        elif status_matches(
            status,
            DELIVERED_STATUSES
        ):

            delivered_count += 1

        elif status_matches(
            status,
            RETURNED_STATUSES
        ):

            returned_count += 1

        elif status_matches(
            status,
            CANCELLED_STATUSES
        ):

            cancelled_count += 1

        else:

            unknown_count += 1

    if total_count <= 0:

        closed_rate = 0.0
        delivered_rate = 0.0
        returned_rate = 0.0
        cancelled_rate = 0.0

    else:

        closed_rate = round(
            (
                closed_count
                / total_count
            )
            * 100,
            2
        )

        delivered_rate = round(
            (
                delivered_count
                / total_count
            )
            * 100,
            2
        )

        returned_rate = round(
            (
                returned_count
                / total_count
            )
            * 100,
            2
        )

        cancelled_rate = round(
            (
                cancelled_count
                / total_count
            )
            * 100,
            2
        )

    return {
        "total": total_count,
        "closed": closed_count,
        "delivered": delivered_count,
        "returned": returned_count,
        "cancelled": cancelled_count,
        "unknown": unknown_count,
        "closed_rate": closed_rate,
        "delivered_rate": delivered_rate,
        "returned_rate": returned_rate,
        "cancelled_rate": cancelled_rate,
        "status_counts": status_counts,
    }


# ==========================================
# Business Row
# ==========================================

def build_row(
    account: Dict[str, Any],
    shipments: Any,
    reference_date: Optional[datetime] = None
) -> Dict[str, Any]:
    """
    بناء صف الحساب الذي سيظهر في Excel والداشبورد.
    """

    if not isinstance(
        account,
        dict
    ):
        account = {}

    stats = shipment_statistics(
        shipments
    )

    created_at = field_value(
        account,
        "created_at"
    )

    account_age = calculate_age(
        created_at,
        reference_date=reference_date
    )

    business_name = (
        clean_text(
            account.get("display")
        )
        or clean_text(
            field_value(
                account,
                "name"
            )
        )
        or clean_text(
            account.get("name")
        )
    )

    phone = clean_text(
        field_value(
            account,
            "phone"
        )
    )

    mobile_intro = clean_text(
        field_value(
            account,
            "mobile_intro"
        )
    )

    # إضافة مقدمة الهاتف فقط عندما لا تكون موجودة.
    if (
        mobile_intro
        and phone
        and not phone.startswith(
            mobile_intro
        )
    ):

        full_phone = (
            mobile_intro
            + phone
        )

    else:

        full_phone = phone

    account_manager = (
        related_display_name(account, "account_manager", "account manager", "users")
        or related_name(account, "account_manager")
        or related_name(account, "account manager")
    )

    office = (
        related_display_name(account, "office")
        or related_name(account, "office")
    )

    return {
        "Business ID":
            account.get(
                "id",
                ""
            ),

        "Business Name":
            business_name,

        "Phone":
            full_phone,

        "Created At":
            clean_text(
                created_at
            ),

        "Account Age":
            (
                account_age
                if account_age is not None
                else ""
            ),

        "Incubation":
            incubation_status(
                account_age
            ),

        "Account Manager":
            account_manager,

        "Office":
            office,

        "Shipments":
            stats["total"],

        "Closed":
            stats["closed"],

        "Delivered":
            stats["delivered"],

        "Returned":
            stats["returned"],

        "Cancelled":
            stats["cancelled"],

        "Closed %":
            stats["closed_rate"],

        "Delivered %":
            stats["delivered_rate"],

        "Returned %":
            stats["returned_rate"],

        "Cancelled %":
            stats["cancelled_rate"],
    }