"""显式页面验收：只连接 console_preview 的独立合成栈，输出到私有运行目录。"""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".runtime/console-improvements"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    results = {"checks": [], "page_errors": []}
    def check(name, passed):
        results["checks"].append({"name": name, "passed": bool(passed)})
        assert passed, name
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, locale="zh-CN")
        page.on("pageerror", lambda err: results["page_errors"].append(str(err)))
        page.goto("http://127.0.0.1:18769/console")
        def connect(token):
            page.locator("#open-auth").click()
            page.locator("#token").fill(token)
            page.locator("#auth-form").get_by_role("button", name="连接", exact=True).click()
            page.wait_for_function("() => document.querySelector('#auth-state').textContent === '已连接'")
        connect("admin-token")
        check("服务端身份与实际角色", page.locator("#identity-name").inner_text() == "admin" and "管理员" in page.locator("#identity-roles").inner_text())
        page.locator('[data-view="admin"]').click()
        page.locator('[data-admin-section="ssh"]').click()
        page.locator("#admin-ssh").get_by_role("button", name="编辑", exact=True).click()
        check("编辑后用户名可写且 ID 锁定", page.locator("#ssh-username").is_editable() and not page.locator("#ssh-credential-id").is_editable())
        check("敏感字段没有回显", page.locator("#ssh-password").input_value() == "" and page.locator("#ssh-known-hosts").input_value() == "")
        page.locator("#ssh-username").fill("updated-user")
        page.locator("#ssh-password").fill("synthetic-replacement")
        page.get_by_role("button", name="保存 SSH 凭据", exact=True).click()
        page.locator("#admin-ssh-feedback").filter(has_text="配置已保存并生效").wait_for()
        check("真实 API 更新后用户名生效", "updated-user" in page.locator("#admin-ssh .credential-card").filter(has_text="editable").first.inner_text())
        check("提交后清理密码", page.locator("#ssh-password").input_value() == "")
        page.locator("#admin-ssh").get_by_role("button", name="编辑", exact=True).click()
        page.locator("#ssh-password").fill("discard-me")
        page.get_by_role("button", name="取消编辑 / 新建", exact=True).click()
        check("取消后清理密码", page.locator("#ssh-password").input_value() == "")
        page.evaluate("() => window.scrollTo(0, 0)")
        page.screenshot(path=str(OUT / "ssh-desktop.png"), full_page=True)
        page.locator('[data-view="approvals"]').click()
        page.locator("#approval-history-list").filter(has_text="browser-history").wait_for()
        text = page.locator("#approval-history-list").inner_text()
        check("审批和执行结果分别展示", "审批结果：通过" in text and "执行结果：restart · failed" in text and "alice" in text)
        page.evaluate("() => window.scrollTo(0, 0)")
        page.screenshot(path=str(OUT / "history-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        check("移动端无水平溢出", page.evaluate("() => document.documentElement.scrollWidth <= innerWidth"))
        page.screenshot(path=str(OUT / "history-mobile.png"), full_page=True)
        connect("user-token")
        check("切换后身份与角色更新", page.locator("#identity-name").inner_text() == "alice" and page.locator("#identity-roles").inner_text() == "用户")
        page.locator('[data-view="approvals"]').click()
        check("非审批人无法读取跨用户历史", "browser-history" not in page.locator("#approval-history-list").inner_text())
        page.evaluate("() => sessionStorage.setItem('aiops_console_token', 'invalid-synthetic')")
        page.locator("#refresh").click()
        page.wait_for_function("() => document.querySelector('#identity-name').textContent === '未连接'")
        check("Token 失效清除旧身份", page.locator("#identity-roles").inner_text() == "身份尚未验证")
        check("无未捕获页面异常", not results["page_errors"])
        browser.close()
    (OUT / "browser-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
