"""Verify UI rendering at 375px (mobile) and 1280px (desktop) using Playwright."""
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

application = get_wsgi_application()

def run_server():
    httpd = make_server("127.0.0.1", 8089, application)
    httpd.serve_forever()

def verify():
    # Start server in thread
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    time.sleep(1)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for width, label in [(375, "mobile"), (1280, "desktop")]:
            context = browser.new_context(
                viewport={"width": width, "height": 812 if width == 375 else 800}
            )
            page = context.new_page()

            # Test Styleguide
            page.goto("http://127.0.0.1:8089/styleguide/")
            title = page.title()
            assert "Styleguide" in title, f"Unexpected title {title}"
            
            # Check neo-brutalist buttons exist
            btn_count = page.locator(".btn").count()
            assert btn_count >= 5, f"Expected >= 5 buttons, got {btn_count}"

            # Check stickers and stamps
            assert page.locator(".sticker-reward").count() >= 2
            assert page.locator(".stamp-badge").count() >= 4

            # Test an informational page
            page.goto("http://127.0.0.1:8089/about/")
            assert "About" in page.title()

            # Test Accounts pages
            page.goto("http://127.0.0.1:8089/accounts/login/")
            assert "Login" in page.title()
            assert page.locator("input[name='username_or_email']").is_visible()

            page.goto("http://127.0.0.1:8089/accounts/signup/")
            assert "Sign Up" in page.title()
            assert page.locator("input[name='email']").is_visible()

            print(f"[{label.upper()} {width}px] UI verified successfully.")
            context.close()

        browser.close()

if __name__ == "__main__":
    verify()
