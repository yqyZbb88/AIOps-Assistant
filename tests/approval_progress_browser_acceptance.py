"""针对固定本机合成服务验证审批→执行→AI 的可见反馈，不连接真实 SSH/模型。"""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    out = Path(__file__).resolve().parents[1] / ".runtime/approval-rule-fix"
    out.mkdir(parents=True, exist_ok=True)
    results = {"checks": [], "page_errors": []}
    def check(name, condition):
        results["checks"].append({"name": name, "passed": bool(condition)})
        assert condition, name
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, locale="zh-CN")
        page.on("pageerror", lambda error: results["page_errors"].append(str(error)))
        page.goto("http://127.0.0.1:18769/console")
        page.locator("#open-auth").click(); page.locator("#token").fill("admin-token")
        page.locator("#auth-form").get_by_role("button", name="连接", exact=True).click()
        page.wait_for_function("() => document.querySelector('#auth-state').textContent === '已连接'")
        page.locator('[data-view="approvals"]').click()
        page.get_by_role("button", name="批准并执行", exact=True).click()
        page.get_by_role("button", name="确认批准", exact=True).click()
        check("确认框立即关闭", not page.locator("#confirm-dialog").is_visible())
        check("立即显示等待服务端确认", "等待服务端确认" in page.locator("#approval-progress").inner_text())
        page.wait_for_function("() => document.querySelector('#approval-progress').textContent.includes('审批已通过') && document.querySelector('#approval-progress').textContent.includes('执行中')")
        check("已通过时不冒充执行成功", "执行成功" not in page.locator("#approval-progress").inner_text())
        buttons = page.locator('#approval-list [data-approval-decision]')
        check("执行过程中禁止重复审批", buttons.count() == 0 or buttons.first.is_disabled())
        page.wait_for_function("() => document.querySelector('#approval-progress').textContent.includes('执行成功')")
        check("执行结束后仍明确等待 AI", "正在等待 AI 后续反馈" in page.locator("#approval-progress").inner_text())
        page.screenshot(path=str(out / "approval-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.evaluate("() => window.scrollTo(0, 0)")
        check("移动端无水平溢出", page.evaluate("() => document.documentElement.scrollWidth <= innerWidth"))
        page.screenshot(path=str(out / "approval-mobile.png"), full_page=True)
        page.wait_for_function("() => document.querySelector('#approval-progress').textContent.includes('AI 反馈已返回')")
        check("AI 回复独立显示且保留执行成功", "合成 AI 反馈" in page.locator("#approval-progress").inner_text() and "执行成功" in page.locator("#approval-progress").inner_text())
        check("无未捕获页面异常", not results["page_errors"])
        browser.close()
    (out / "browser-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
