"""通过 CDP 连接到用户已启动的 Chrome，读取 zhipin.com 的 cookie 写入 cookies.json。
用法：先启动 Chrome with --remote-debugging-port=9222，再跑本脚本。
"""
import json
import os
import sys

from playwright.sync_api import sync_playwright

CDP_URL = "http://localhost:9222"
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.json")

# 域名 → 平台 key 映射
DOMAIN_PLATFORM = [
    ("zhipin.com", "boss"),
    ("zhaopin.com", "zhaopin"),
    ("51job.com", "51job"),
]


def main():
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(f"CDP_CONNECT_FAIL: {type(e).__name__}: {e}")
            sys.exit(1)

        print(f"CDP connected, contexts={len(browser.contexts)}")
        ctx = browser.contexts[0]

        # 读所有 cookie（含 HttpOnly）
        all_cookies = ctx.cookies()
        print(f"total cookies in browser: {len(all_cookies)}")

        # 加载现有 cookies.json（如有）
        if os.path.exists(COOKIES_FILE):
            with open(COOKIES_FILE, "r", encoding="utf-8") as f:
                stored = json.load(f)
        else:
            stored = {}

        # 按平台分组写入
        for domain_key, platform in DOMAIN_PLATFORM:
            platform_cookies = [c for c in all_cookies if domain_key in c.get("domain", "")]
            cookie_dict = {c["name"]: c["value"] for c in platform_cookies}
            if cookie_dict:
                stored[platform] = cookie_dict
                print(f"  [{platform}] {len(cookie_dict)} cookies (domain={domain_key})")
                print(f"    names: {list(cookie_dict.keys())[:15]}")
            else:
                print(f"  [{platform}] no cookies (domain={domain_key})")

        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            json.dump(stored, f, ensure_ascii=False, indent=2)
        print(f"WROTE {COOKIES_FILE}")

        browser.close()


if __name__ == "__main__":
    main()
