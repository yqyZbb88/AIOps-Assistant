from fastapi.testclient import TestClient

from src.main import create_app
from src.utils.config_loader import HostsSettings, Settings


def test_hosts_api_is_redacted():
    app = create_app(settings=Settings(hosts=HostsSettings(file="config/hosts.yaml")))
    with TestClient(app) as client:
        response = client.get("/hosts")
    assert response.status_code == 200
    item = response.json()[0]
    assert item["host_id"] == "local-node"
    assert "host" not in item and "credential_ref" not in item


def test_unknown_host_returns_404():
    app = create_app(settings=Settings(hosts=HostsSettings(file="config/hosts.yaml")))
    with TestClient(app) as client:
        response = client.get("/hosts/missing")
    assert response.status_code == 404
