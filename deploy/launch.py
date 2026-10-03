"""Read systemd's runtime PG credential; never put the password in argv or logs."""
import os
from pathlib import Path
import sys

credential_dir = os.environ.get("CREDENTIALS_DIRECTORY")
if not credential_dir:
    raise RuntimeError("systemd PostgreSQL credential is required")
os.environ["PGPASSWORD"] = (Path(credential_dir) / "pgpassword").read_text().strip()
os.execv(sys.executable, [sys.executable, "-m", "uvicorn", "app.main:app",
                        "--host", "127.0.0.1", "--port", "8010", "--workers", "1"])
