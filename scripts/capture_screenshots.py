"""Capture high-resolution screenshots for README documentation."""
import os
import sys
from pathlib import Path
import threading
import time
from wsgiref.simple_server import make_server

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
import django
django.setup()
from django.core.wsgi import get_wsgi_application
from apps.bounties.models import Bounty

application = get_wsgi_application()

def run_server():
    httpd = make_server("127.0.0.1", 8091, application)
    httpd.serve_forever()

def capture():
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    time.sleep(1)

    shots_dir = BASE_DIR / "docs" / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # 1. Desktop Board (1280x900)
        desktop_ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = desktop_ctx.new_page()
        page.goto("http://127.0.0.1:8091/")
        page.wait_for_timeout(500)
        page.screenshot(path=str(shots_dir / "board-desktop.png"))
        print("Captured board-desktop.png")

        # 2. Bounty Detail (1280x900)
        detail_link = page.locator(".bounty-card a").first.get_attribute("href")
        if detail_link:
            page.goto(f"http://127.0.0.1:8091{detail_link}")
            page.wait_for_timeout(500)
            page.screenshot(path=str(shots_dir / "bounty-detail.png"))
            print("Captured bounty-detail.png")

        # 3. Styleguide (1280x900)
        page.goto("http://127.0.0.1:8091/styleguide/")
        page.wait_for_timeout(500)
        page.screenshot(path=str(shots_dir / "styleguide.png"))
        print("Captured styleguide.png")

        # 4. Mobile Board (375x812)
        mobile_ctx = browser.new_context(viewport={"width": 375, "height": 812})
        m_page = mobile_ctx.new_page()
        m_page.goto("http://127.0.0.1:8091/")
        m_page.wait_for_timeout(500)
        m_page.screenshot(path=str(shots_dir / "mobile-board.png"))
        print("Captured mobile-board.png")

        desktop_ctx.close()
        mobile_ctx.close()
        browser.close()

if __name__ == "__main__":
    capture()
