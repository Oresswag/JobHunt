#!/usr/bin/env python3
"""One-time Playwright login bootstrap for creating a reusable browser profile."""

import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

raw_profile_dir = Path(os.environ.get("CHROME_PROFILE_DIR", str(BASE_DIR / "chrome-profile"))).expanduser()
CHROME_PROFILE_DIR = (
    raw_profile_dir if raw_profile_dir.is_absolute() else (BASE_DIR / raw_profile_dir).resolve()
)
BROWSER_EXECUTABLE_PATH = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip()
START_URL = os.environ.get(
    "START_URL",
    "https://www.indeed.com/account/login",
)
NAV_TIMEOUT_MS = int(os.environ.get("PLAYWRIGHT_NAV_TIMEOUT_MS", "20000"))

CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)


def main() -> None:
    print(f"Profile directory: {CHROME_PROFILE_DIR}")
    print(f"Opening Chromium to: {START_URL}")
    print("Log in manually, complete any CAPTCHA, then press Enter here to close.")

    with sync_playwright() as playwright:
        launch_options = {
            "user_data_dir": str(CHROME_PROFILE_DIR),
            "headless": False,
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        }
        if BROWSER_EXECUTABLE_PATH:
            launch_options["executable_path"] = BROWSER_EXECUTABLE_PATH

        browser = playwright.chromium.launch_persistent_context(**launch_options)
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto(START_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        input()
        browser.close()

    print("Login session saved.")


if __name__ == "__main__":
    main()
