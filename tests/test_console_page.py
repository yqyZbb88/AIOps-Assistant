"""图形化控制台只提供公开空壳，数据和操作继续走受保护 API。"""
from fastapi.testclient import TestClient

from src.main import create_app
from src.utils.config_loader import AuthSettings, Settings


def test_console_shell_is_public_but_operational_data_stays_protected():
    settings = Settings(auth=AuthSettings(enabled=True, tokens='{"secret-token":"admin"}'))
    with TestClient(create_app(settings=settings)) as client:
        page = client.get("/console")
        hosts = client.get("/hosts")
        approvals = client.get("/approvals")

    assert page.status_code == 200
    assert "AIOps Assistant 控制台" in page.text
    assert "secret-token" not in page.text
    assert hosts.status_code == 401
    assert approvals.status_code == 401
    assert page.headers["cache-control"] == "no-store"
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert "connect-src 'self'" in page.headers["content-security-policy"]


def test_console_assets_are_local_public_and_dom_safe():
    with TestClient(create_app(settings=Settings())) as client:
        css = client.get("/console-assets/console.css")
        js = client.get("/console-assets/console.js")

    assert css.status_code == 200
    assert js.status_code == 200
    assert "innerHTML" not in js.text
    assert "outerHTML" not in js.text
    assert "insertAdjacentHTML" not in js.text
    assert "eval(" not in js.text
    assert "sessionStorage.setItem(TOKEN_KEY" in js.text
    assert "localStorage.setItem(PREF_KEY" in js.text
    assert "localStorage.setItem(TOKEN_KEY" not in js.text
    assert 'fetch("http://' not in js.text
    assert 'fetch("https://' not in js.text


def test_console_contains_all_views_and_safe_workflows():
    with TestClient(create_app(settings=Settings())) as client:
        page = client.get("/console").text
        js = client.get("/console-assets/console.js").text

    for view in ("overview", "hosts", "assistant", "approvals", "records", "admin", "settings"):
        assert f'data-view-panel="{view}"' in page
    assert 'id="chat-form"' in page
    assert 'id="approval-list"' in page
    assert 'id="metric-preferences"' in page
    assert 'id="widget-preferences"' in page
    assert 'href="/status"' not in page
    assert 'id="admin-nav" class="nav-item hidden"' in page
    assert 'request("/sessions"' in js
    assert 'request("/chat"' in js
    assert 'request("/approvals/"' in js
    assert 'createElementNS(ns, "svg")' in js
    assert "PromQL" in page and "SSH" in page and "Shell" in page


def test_removed_status_page_and_assets_return_404():
    with TestClient(create_app(settings=Settings())) as client:
        page = client.get("/status")
        script = client.get("/status-assets/status.js")

    assert page.status_code == 404
    assert script.status_code == 404


def test_chat_renders_user_turn_before_waiting_for_assistant():
    with TestClient(create_app(settings=Settings())) as client:
        js = client.get("/console-assets/console.js").text
        css = client.get("/console-assets/console.css").text

    send_start = js.index("async function sendChat")
    optimistic = js.index("appendPendingTurn(content)", send_start)
    request = js.index('await request("/chat"', send_start)
    assert optimistic < request
    assert "finishPendingTurn(pending" in js
    assert "typing-dots" in js
    assert "@keyframes typing-bounce" in css


def test_records_use_left_aligned_structured_details_and_safe_json_disclosure():
    with TestClient(create_app(settings=Settings())) as client:
        js = client.get("/console-assets/console.js").text
        css = client.get("/console-assets/console.css").text

    assert 'node("details", "record-json")' in js
    assert '"查看原始 JSON"' in js
    assert 'list.classList.remove("empty")' in js
    assert ".record-list{max-height:" in css and "text-align:left" in css


def test_layout_has_responsive_host_detail_right_aligned_choices_and_admin_tabs():
    with TestClient(create_app(settings=Settings())) as client:
        page = client.get("/console").text
        js = client.get("/console-assets/console.js").text
        css = client.get("/console-assets/console.css").text

    assert 'class="panel-head host-detail-head"' in page
    assert ".host-detail-head{align-items:flex-start;flex-wrap:wrap}" in css
    assert ".range-values{display:grid" in css
    assert "数值稳定" in js
    assert ".check-item,.admin-check{justify-content:space-between" in css
    assert "choice.append(node(\"span\", \"\", meta.label), visible)" in js
    assert "展开指标快照" in js
    for name in ("general", "rules", "commands", "hosts", "monitor"):
        assert f'data-admin-section="{name}"' in page
        assert f'data-admin-pane="{name}"' in page
    assert 'role="tablist"' in page
    assert "event.key === \"ArrowRight\"" in js
    assert "if (refreshDraft) renderAdmin()" in js
    assert "input.disabled = !state.hostVerification" in js
    assert 'id="admin-host-ssh-check"' in page
