from pathlib import Path

from fastapi.testclient import TestClient

from src.main import create_app
from src.utils.config_loader import AuthSettings, HostsSettings, Settings


def _host_config(tmp_path: Path) -> str:
    path = tmp_path / "hosts.yaml"
    path.write_text(
        """
version: 1
credentials:
  test:
    username: ops
hosts:
  - id: test-node
    name: 测试主机
    prometheus:
      job: node
      instance: test-node:9100
    ssh:
      host: 192.0.2.10
      credential_ref: test
    allow:
      diagnostics: []
      rule_actions: []
      agent_actions: []
""".strip(),
        encoding="utf-8",
    )
    return str(path)


def test_hosts_api_is_redacted(tmp_path):
    app = create_app(settings=Settings(auth=AuthSettings(tokens='{"test-ops":"ops"}', admin_identities='["ops"]'), hosts=HostsSettings(file=_host_config(tmp_path))))
    with TestClient(app, headers={"Authorization": "Bearer test-ops"}) as client:
        response = client.get("/hosts")
    assert response.status_code == 200
    item = response.json()[0]
    assert item["host_id"] == "test-node"
    assert "host" not in item and "credential_ref" not in item


def test_unknown_host_returns_404(tmp_path):
    app = create_app(settings=Settings(auth=AuthSettings(tokens='{"test-ops":"ops"}', admin_identities='["ops"]'), hosts=HostsSettings(file=_host_config(tmp_path))))
    with TestClient(app, headers={"Authorization": "Bearer test-ops"}) as client:
        response = client.get("/hosts/missing")
    assert response.status_code == 404
