"""Isolated Compose acceptance: two users/hosts, rotation, revoke and recreation.
Run only in a disposable deployment with no model key or SSH connection.
Usage: docker compose exec -T assistant python - [--verify] < tests/compose_access_smoke.py
"""
import json
import sys
from copy import deepcopy
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def request(path, token=None, data=None, method=None, expected=200):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = Request("http://127.0.0.1:8000" + path, headers=headers,
        data=None if data is None else json.dumps(data).encode(), method=method)
    try:
        with urlopen(req, timeout=15) as response:
            status, raw = response.status, response.read()
    except HTTPError as error:
        status, raw = error.code, error.read()
    assert status == expected, f"{method or 'GET'} {path}: {status}, expected {expected}"
    return json.loads(raw) if raw else None


def main():
    secrets = json.loads(Path("/run/assistant-secrets/assistant.json").read_text())
    assert not secrets.get("MODEL_API_KEY"), "Isolated key-free stack required"
    tokens = json.loads(secrets["OPS_API_TOKENS"])
    admin = next(k for k,v in tokens.items() if v == "admin")
    webhook = next(k for k,v in tokens.items() if v == "alertmanager")
    state_file = Path("/app/data/host-access-smoke.json")
    def save(grants):
        snap = request("/admin/config", admin)
        draft = deepcopy(snap["config"])
        draft["settings"]["host_access"].update(grants)
        return request("/admin/config", admin, {"revision": snap["revision"], "config": draft}, "PUT")
    def credential(path, data=None, method="POST"):
        revision = request("/admin/credentials", admin)["revision"]
        return request("/admin/credentials/" + path, admin, {"revision": revision, "data": data or {}}, method)
    def denied(token, host):
        for path in [f"/hosts/{host}", f"/hosts/{host}/status", f"/hosts/{host}/metrics/cpu.usage",
                     f"/hosts/{host}/metrics/cpu.usage/trend", f"/alerts/history?host_id={host}",
                     f"/alerts/recent?host_id={host}", f"/alerts/active?host_id={host}",
                     f"/inspections/recent?host_id={host}", f"/executions?host_id={host}"]:
            request(path, token, expected=404)
        request("/sessions", token, {"host_id": host}, expected=404)
    if "--verify" in sys.argv:
        state = json.loads(state_file.read_text())
        a, b = state["alice"], state["bob"]
        assert [h["host_id"] for h in request("/hosts", a)] == ["local-node"]
        assert [h["host_id"] for h in request("/hosts", b)] == ["restricted-node"]
        assert request("/sessions/" + state["alice_session"], a)["messages"]
        assert request("/sessions/" + state["bob_session"], b)["messages"]
        request("/sessions/" + state["alice_session"], b, expected=404)
        denied(a, "restricted-node"); denied(b, "local-node")
        save({"access-alice": []})
        assert request("/hosts", a) == []
        assert request("/sessions", a) == []
        request("/sessions/" + state["alice_session"], a, expected=404)
        request("/chat", a, {"session_id": state["alice_session"], "message": "CPU"}, expected=404)
        save({"access-alice": ["local-node"]})
        assert request("/sessions/" + state["alice_session"], a)["messages"]
        credential("tokens/" + state["bob_id"], method="DELETE")
        request("/hosts", b, expected=401)
        request("/alert", webhook, {"alerts": []})
        request("/admin/config", admin)
        state_file.unlink()
        print("PASS: recreated user identities, host grants and private chats; revoke/restore/token revoke enforced")
        return
    # Add a static second fixture target through the same disk-apply boundary used in deployment.
    if not any(host["host_id"] == "restricted-node" for host in request("/hosts", admin)):
        host_file = Path("/app/config/hosts.yaml")
        from ruamel.yaml import YAML
        yaml = YAML()
        yaml.preserve_quotes = True
        doc = yaml.load(host_file.read_text(encoding="utf-8"))
        doc["hosts"].append({
            "id": "restricted-node", "name": "Restricted test node", "enabled": True,
            "prometheus": {"job": "prometheus", "instance": "prometheus:9090"},
            "allow": {"diagnostics": [], "rule_actions": [], "agent_actions": []},
        })
        with host_file.open("w", encoding="utf-8") as handle:
            yaml.dump(doc, handle)
        snapshot = request("/admin/config", admin)
        request("/admin/config/apply", admin, {"revision": snapshot["revision"]}, "POST")
    first = credential("tokens", {"identity": "access-alice"})
    second = credential("tokens", {"identity": "access-bob"})
    a,b = first["token_once"], second["token_once"]
    assert request("/hosts", a) == [] and request("/hosts", b) == []
    save({"access-alice": ["local-node"], "access-bob": ["restricted-node"]})
    sa = request("/sessions", a, {"host_id": "local-node"}, expected=201)["session_id"]
    sb = request("/sessions", b, {"host_id": "restricted-node"}, expected=201)["session_id"]
    request("/chat", a, {"session_id": sa, "message": "hello"})
    request("/chat", b, {"session_id": sb, "message": "hello"})
    for intruder in (b,admin):
        request("/sessions/" + sa, intruder, expected=404)
        request("/sessions/" + sa, intruder, {"name": "stolen"}, "PATCH", expected=404)
        request("/sessions/" + sa, intruder, method="DELETE", expected=404)
        request("/chat", intruder, {"session_id": sa, "message": "CPU"}, expected=404)
    denied(a, "restricted-node"); denied(b, "local-node")
    rotated = credential("tokens/" + first["token_id"] + "/rotate")
    request("/hosts", a, expected=401)
    a = rotated["token_once"]
    assert request("/sessions/" + sa, a)["messages"]
    request("/admin/config", b, expected=403)
    request("/alert", a, {"alerts": []}, expected=403)
    state_file.write_text(json.dumps({"alice": a, "bob": b, "alice_session": sa,
        "bob_session": sb, "bob_id": second["token_id"]}))
    state_file.chmod(0o600)
    print("PASS: two users/hosts, session ownership and rotated token isolated")


if __name__ == "__main__":
    main()
