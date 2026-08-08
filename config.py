import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

EMAIL = os.getenv("OPOST_EMAIL", "Mansour_E@gmail.com")
PASSWORD = os.getenv("OPOST_PASSWORD", "mansour2007")
BASE_URL = "https://o.opost.ps"
LOGIN_URL = f"{BASE_URL}/login"
BUSINESSES_URL = f"{BASE_URL}/en/w/businesses"
ACCOUNT_MANAGER_ID = int(os.getenv("ACCOUNT_MANAGER_ID", "15122559"))
