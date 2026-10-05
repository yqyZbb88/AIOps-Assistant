"""Run only inside an isolated, key-free Compose test stack; never a real deployment.

Usage: docker compose exec -T assistant python - [--verify] < tests/compose_smoke.py
The first run writes a test session and a harmless config comment; --verify checks
both survived recreation. No external model or SSH connections are made.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def request(path, *, token=None, data=None, expected=200, base="http://127.0.0.1:8000", method=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = Request(base + path, data=None if data is None else json.dumps(data).encode(),
                  headers=headers, method=method)
    try:
        with urlopen(req, timeout=12) as response:
            status, raw = response.status, response.read()
    except HTTPError as error:
        status, raw = error.code, error.read()
    assert status == expected, f"{path}: expected {expected}, received {status}"
    return json.loads(raw) if raw else None


def eventually(check, seconds=50):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(2)
    raise AssertionError("Timed out waiting for local monitoring/alert delivery")


def main():
    secret = Path("/run/assistant-secrets/assistant.json")
    values = json.loads(secret.read_text())
    assert not values.get("DEEPSEEK_API_KEY"), "Smoke stack must have no model key"
    tokens = json.loads(values["OPS_API_TOKENS"])
    admin = next(k for k, v in tokens.items() if v == "admin")
    webhook = next(k for k, v in tokens.items() if v == "alertmanager")
    request("/ready")
    request("/hosts", expected=401)
    assert any(h["host_id"] == "local-node" for h in request("/hosts", token=admin))
    request("/admin/config", token=admin)
    request("/admin/config", token=webhook, expected=403)
    request("/alert", token=admin, data={"alerts": []}, expected=403)
    assert request("/alert", token=webhook, data={"alerts": []})["received"] == 0
    request("/api/health", base="http://grafana:3000")
    eventually(lambda: any(t["health"] == "up" and t["labels"].get("job") == "node-exporter"
                          for t in request("/api/v1/targets", base="http://prometheus:9090")["data"]["activeTargets"]))

    state_file = Path("/app/data/deployment-smoke.json")
    config_file = Path("/app/config/hosts.yaml")
    if "--verify" in sys.argv:
        state = json.loads(state_file.read_text())
        assert hashlib.sha256(secret.read_bytes()).hexdigest() == state["credential_hash"]
        assert hashlib.sha256(config_file.read_bytes()).hexdigest() == state["config_hash"]
        detail = request("/sessions/" + state["session_id"], token=admin)
        assert detail["session_id"] == state["session_id"]
        assert detail["messages"], "Chat history did not survive recreation"
        request("/sessions/" + state["session_id"], token=admin, method="DELETE")
        state_file.unlink()
        print("PASS: credentials, config, session and chat survived recreation")
        return

    session = request("/sessions", token=admin, data={"name": "Deployment smoke", "host_id": "local-node"}, expected=201)
    request("/chat", token=admin, data={"session_id": session["session_id"], "message": "CPU"})
    # Exercise real Alertmanager -> Assistant routing and its file-backed credential.
    name = "DeploymentSmoke" + uuid.uuid4().hex[:10]
    now = datetime.now(timezone.utc)
    request("/api/v2/alerts", base="http://alertmanager:9093", data=[{
        "labels": {"alertname": name, "job": "node-exporter", "instance": "node-exporter:9100", "severity": "info"},
        "annotations": {"summary": "Isolated deployment smoke check"},
        "startsAt": now.isoformat(), "endsAt": (now + timedelta(minutes=2)).isoformat(),
    }])
    eventually(lambda: name in json.dumps(request("/alerts/history?host_id=local-node", token=admin)))
    with config_file.open("a", encoding="utf-8") as handle:
        handle.write("\n# Isolated deployment smoke: preserve this edit across recreation.\n")
    state_file.write_text(json.dumps({
        "session_id": session["session_id"],
        "credential_hash": hashlib.sha256(secret.read_bytes()).hexdigest(),
        "config_hash": hashlib.sha256(config_file.read_bytes()).hexdigest(),
    }))
    print("PASS: readiness, auth, monitoring, keyword chat, Alertmanager delivery and persistence fixture")


if __name__ == "__main__":
    main()
