import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("TC_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
BANK_NAME = os.environ.get("TC_BANK_NAME", "Demo Bank")
# The dashboard login has no fallback outside dev, so a deploy that forgets to set it
# fails at startup instead of serving every applicant's books behind admin/admin.
DEV = os.environ.get("TC_DEV") == "1"
ADMIN_USER = os.environ.get("TC_ADMIN_USER", "admin" if DEV else "")
ADMIN_PASSWORD = os.environ.get("TC_ADMIN_PASSWORD", "admin" if DEV else "")
if not (ADMIN_USER and ADMIN_PASSWORD):
    raise RuntimeError("Set TC_ADMIN_USER and TC_ADMIN_PASSWORD, or TC_DEV=1 for a local admin/admin login")
CODE_TTL_HOURS = int(os.environ.get("TC_CODE_TTL_HOURS", "72"))
MAX_UPLOAD_MB = int(os.environ.get("TC_MAX_UPLOAD_MB", "300"))
DEFAULT_MONTHS = int(os.environ.get("TC_DEFAULT_MONTHS", "24"))
# Monthly monitoring: a refresh is due once this day of the month is reached
# (so the previous month is in the books), and flagged overdue after N days.
MONITOR_DAY = int(os.environ.get("TC_MONITOR_DAY", "5"))
MONITOR_OVERDUE_DAYS = int(os.environ.get("TC_MONITOR_OVERDUE_DAYS", "40"))
# Optional: path to the built TallyConnector.exe, served at /download.
CONNECTOR_EXE = os.environ.get("TC_CONNECTOR_EXE", "")
