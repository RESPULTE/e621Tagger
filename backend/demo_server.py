"""Isolated synthetic-data server for browser tests and local UI exploration."""

import argparse
import tempfile
from pathlib import Path

import uvicorn

from backend.app.auth import hash_password
from backend.app.config import Settings
from backend.app.main import create_app

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8847)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="hydra-studio-demo-") as data_root:
        settings = Settings(
            _env_file=None,
            data_root=Path(data_root),
            mock_e621=True,
            password_hash=hash_password("studio-browser-test-password"),
            secure_cookie=False,
            public_origin=f"http://127.0.0.1:{args.port}",
            frontend_root=Path(__file__).resolve().parent.parent / "frontend" / "dist",
        )
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, access_log=False)
