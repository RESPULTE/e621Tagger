"""Generate the frontend contract without opening SQLite or loading configuration."""

import json
from pathlib import Path

from backend.app.main import app

if __name__ == "__main__":
    Path(__file__).with_name("openapi.json").write_text(json.dumps(app.openapi(), indent=2) + "\n", encoding="utf-8")
