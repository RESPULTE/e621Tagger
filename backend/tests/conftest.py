import pytest
from fastapi.testclient import TestClient

from backend.app.auth import hash_password
from backend.app.config import Settings
from backend.app.main import create_app
from backend.init_library import initialize

PASSWORD = "studio-test-password"


@pytest.fixture
def settings(tmp_path):
    library_root = tmp_path / "smut"
    library_root.mkdir()
    initialize(library_root)
    return Settings(
        data_root=tmp_path,
        password_hash=hash_password(PASSWORD),
        mock_e621=True,
        library_root=library_root,
        secure_cookie=False,
        public_origin="http://testserver",
        media_interval=0,
    )


@pytest.fixture
def client(settings):
    app = create_app(settings, start_workers=False)
    with TestClient(app, base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
        result = client.post("/api/auth/login", json={"password": PASSWORD})
        assert result.status_code == 200
        client.headers["X-CSRF-Token"] = result.json()["csrf_token"]
        yield client
