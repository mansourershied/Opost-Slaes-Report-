from calendar import monthrange

from database import engine
from models import Base

from opost_client import OpostClient
from business_service import build_row
from excel import export_businesses


Base.metadata.create_all(
    bind=engine
)


year = input(
    "Year (مثال 2026): "
).strip()

month = input(
    "Month (مثال 7): "
).strip().zfill(2)

minimum_closed = float(
    input("Minimum Closed % : ")
)


last_day = monthrange(
    int(year),
    int(month)
)[1]

start_date = (
    f"{year}-{month}-01"
)

end_date = (
    f"{year}-{month}-"
    f"{last_day:02d}"
)


client = OpostClient()

try:

    client.start()
    client.login()

    print(
        "\nLoading Businesses...\n"
    )

    businesses = client.get_businesses(
        start_date,
        end_date
    )

    print(
        f"\nBusinesses Found : "
        f"{len(businesses)}"
    )

    business_ids = {

        int(
            business["id"]
        )

        for business
        in businesses

        if business.get(
            "id"
        ) is not None
    }

    # تحميل شحنات الفترة مرة واحدة فقط.
    # لا يوجد طلب شحنات لكل حساب.

    shipments_by_business = (
        client
        .get_shipments_grouped_by_business(
            start_date=start_date,
            end_date=end_date,
            business_ids=business_ids,
        )
    )

    total_accounts = len(
        businesses
    )

    accounts_with_shipments = 0
    accounts_without_shipments = 0

    all_accounts = []
    best_accounts = []
    follow_up_accounts = []
    no_shipments = []

    for index, business in enumerate(
        businesses,
        start=1
    ):

        business_id = (
            business.get(
                "id"
            )
        )

        business_name = (
            business.get(
                "display"
            )
            or business.get(
                "name"
            )
            or ""
        )

        print(
            f"\n[{index}/{len(businesses)}] "
            f"{business_name}"
        )

        shipments = (
            shipments_by_business.get(
                str(
                    business_id
                ),
                []
            )
        )

        row = build_row(
            business,
            shipments
        )

        all_accounts.append(
            row
        )

        if row["Shipments"] == 0:

            accounts_without_shipments += 1

            no_shipments.append(
                row
            )

            print(
                "No Shipments"
            )

        else:

            accounts_with_shipments += 1

            print(
                business_name,
                "Shipments =",
                row["Shipments"],
                "Closed =",
                row["Closed"],
                "Delivered =",
                row["Delivered"],
                "Closed % =",
                row["Closed %"],
                "Delivered % =",
                row["Delivered %"]
            )

        if (
            row["Shipments"] >= 13
            and row["Closed %"]
            >= minimum_closed
        ):

            best_accounts.append(
                row
            )

        if (
            row["Shipments"] > 0
            and row["Delivered %"] >= 30
        ):

            follow_up_accounts.append(
                row
            )

    all_accounts.sort(
        key=lambda item: (
            item["Closed %"],
            item["Shipments"]
        ),
        reverse=True
    )

    best_accounts.sort(
        key=lambda item: (
            item["Closed %"],
            item["Shipments"]
        ),
        reverse=True
    )

    follow_up_accounts.sort(
        key=lambda item: (
            item["Delivered %"],
            item["Delivered"]
        ),
        reverse=True
    )

    summary = {

    "total_accounts": total_accounts,

    "accounts_with_shipments": accounts_with_shipments,

    "accounts_without_shipments": accounts_without_shipments,

    "accounts_with_shipments_percentage":
        round(
            (accounts_with_shipments / total_accounts) * 100,
            2
        ) if total_accounts else 0,

    "accounts_without_shipments_percentage":
        round(
            (accounts_without_shipments / total_accounts) * 100,
            2
        ) if total_accounts else 0,

    "best_accounts": len(best_accounts),

    "follow_up_accounts": len(follow_up_accounts),
}

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    print(
        "Accounts Created          :",
        summary[
            "total_accounts"
        ]
    )

    print(
        "Accounts With Shipments   :",
        summary[
            "accounts_with_shipments"
        ]
    )

    print(
        "Accounts Without Shipments:",
        summary[
            "accounts_without_shipments"
        ]
    )

    print(
        "Best Accounts             :",
        summary[
            "best_accounts"
        ]
    )

    print(
        "Need Follow Up            :",
        summary[
            "follow_up_accounts"
        ]
    )

    print("=" * 60)

    export_businesses(

        summary=summary,

        all_accounts=
            all_accounts,

        best_accounts=
            best_accounts,

        follow_up_accounts=
            follow_up_accounts,

        no_shipments=
            no_shipments,
    )

    print(
        "\nDone."
    )

finally:

    client.close()