"""Load encrypted systemd runtime credentials without logging or command-line secrets."""
import os
from pathlib import Path
import sys

credential_dir = os.environ.get("CREDENTIALS_DIRECTORY")
if not credential_dir:
    raise RuntimeError("systemd PostgreSQL credential is required")
os.environ["PGPASSWORD"] = (Path(credential_dir) / "pgpassword").read_text().strip()
os.environ["LLM_API_KEY"] = (Path(credential_dir) / "llmapikey").read_text().strip()
if not os.environ["PGPASSWORD"] or not os.environ["LLM_API_KEY"]:
    raise RuntimeError("Database and model credentials must not be empty")
os.execv(sys.executable, [sys.executable, "-m", "uvicorn", "app.main:app",
                        "--host", "127.0.0.1", "--port", "8010", "--workers", "1"])
