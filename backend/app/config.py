import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("TC_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
BANK_NAME = os.environ.get("TC_BANK_NAME", "Demo Bank")
ADMIN_USER = os.environ.get("TC_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("TC_ADMIN_PASSWORD", "admin")
CODE_TTL_HOURS = int(os.environ.get("TC_CODE_TTL_HOURS", "72"))
MAX_UPLOAD_MB = int(os.environ.get("TC_MAX_UPLOAD_MB", "300"))
DEFAULT_MONTHS = int(os.environ.get("TC_DEFAULT_MONTHS", "24"))
# Monthly monitoring: a refresh is due once this day of the month is reached
# (so the previous month is in the books), and flagged overdue after N days.
MONITOR_DAY = int(os.environ.get("TC_MONITOR_DAY", "5"))
MONITOR_OVERDUE_DAYS = int(os.environ.get("TC_MONITOR_OVERDUE_DAYS", "40"))
# Optional: path to the built TallyConnector.exe, served at /download.
CONNECTOR_EXE = os.environ.get("TC_CONNECTOR_EXE", "")
