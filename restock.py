#!/usr/bin/env python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import argparse
import time
import os

import psutil
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException


ACTIVE_URL = "https://www.ebay.com/sh/lst/active"

AVAILABLE_QTY_HEADER_XPATH = "//span[contains(@class,'th-title-content') and normalize-space()='Available quantity']"
RESTOCK_BTN_XPATH = "//button[normalize-space()='Restock' and contains(@class,'primary-action__button')]"
QTY_INPUT_CSS = "input[name='members[0][availableQuantity]']"
SUBMIT_BTN_XPATH = "//button[@type='submit' and contains(@class,'btn--primary') and normalize-space()='Submit']"


def js_click(driver, el):
    driver.execute_script("arguments[0].click();", el)


def safe_click(driver, el):
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    try:
        el.click()
    except Exception:
        js_click(driver, el)


def set_input_value(driver, el, value: str):
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)

    try:
        el.click()
    except Exception:
        js_click(driver, el)

    el.send_keys(Keys.CONTROL, "a")
    el.send_keys(Keys.BACKSPACE)
    el.send_keys(value)

    driver.execute_script(
        """
        const el = arguments[0];
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        """,
        el
    )


def ensure_logged_in_or_pause(driver):
    cur = (driver.current_url or "").lower()
    if "signin" in cur or "login" in cur:
        print("Redirected to sign-in. Log in in the opened browser window, then press Enter here.")
        input()


def kill_chrome_using_profile(profile_dir: str, debug: bool = True) -> None:
    profile_dir_abs = os.path.abspath(profile_dir)

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if "chrome" not in name:
                continue
            cmdline_list = proc.info.get("cmdline") or []
            if not cmdline_list:
                continue
            cmdline = " ".join(cmdline_list)
            if profile_dir_abs in cmdline:
                if debug:
                    print(f"[INFO] Killing stale Chrome PID={proc.pid} using profile: {profile_dir_abs}")
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    time.sleep(0.5)


def click_available_qty_header_until_ascending(driver, timeout=30, max_clicks=4, debug=True):
    wait = WebDriverWait(driver, timeout)
    wait.until(EC.presence_of_element_located((By.XPATH, AVAILABLE_QTY_HEADER_XPATH)))

    def find_sort_container():
        span = driver.find_element(By.XPATH, AVAILABLE_QTY_HEADER_XPATH)
        for _ in range(6):
            parent = span.find_element(By.XPATH, "..")
            if parent.get_attribute("aria-sort"):
                return parent
            span = parent
        return None

    sort_container = find_sort_container()

    if debug:
        print("Sort container aria-sort:", sort_container.get_attribute("aria-sort") if sort_container else None)

    if sort_container:
        for i in range(max_clicks):
            aria_sort = sort_container.get_attribute("aria-sort")
            if aria_sort == "ascending":
                return

            if debug:
                print(f"Clicking Available quantity header ({i+1}/{max_clicks})")

            safe_click(driver, sort_container)
            time.sleep(0.6)

            try:
                wait.until(lambda d: (find_sort_container() or sort_container).get_attribute("aria-sort") != aria_sort)
            except TimeoutException:
                pass

            sort_container = find_sort_container() or sort_container
        return

    if debug:
        print("Fallback sorting (no aria-sort)")
    for _ in range(2):
        safe_click(driver, driver.find_element(By.XPATH, AVAILABLE_QTY_HEADER_XPATH))
        time.sleep(0.8)


@dataclass
class RestockResult:
    attempted: int = 0
    updated: int = 0
    skipped_nonzero: int = 0
    failed: int = 0


def restock_all_zero_to_one(driver, timeout=30, max_items=200, dry_run=False, debug=True) -> RestockResult:
    wait = WebDriverWait(driver, timeout)
    res = RestockResult()

    wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

    while res.attempted < max_items:
        try:
            buttons = driver.find_elements(By.XPATH, RESTOCK_BTN_XPATH)
        except StaleElementReferenceException:
            buttons = []

        buttons = [b for b in buttons if b.is_displayed() and b.is_enabled()]

        if not buttons:
            if debug:
                print("No more Restock buttons.")
            break

        btn = buttons[0]
        res.attempted += 1

        try:
            if debug:
                print(f"[{res.attempted}] Restock")

            safe_click(driver, btn)

            qty_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, QTY_INPUT_CSS)))
            cur_val = (qty_input.get_attribute("value") or "").strip()

            if cur_val not in ("0", ""):
                res.skipped_nonzero += 1
                ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                time.sleep(0.4)
                continue

            if dry_run:
                ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                time.sleep(0.4)
                continue

            set_input_value(driver, qty_input, "1")

            submit_btn = wait.until(EC.element_to_be_clickable((By.XPATH, SUBMIT_BTN_XPATH)))
            safe_click(driver, submit_btn)

            try:
                wait.until(EC.staleness_of(submit_btn))
            except TimeoutException:
                pass

            res.updated += 1
            time.sleep(0.8)

        except Exception as e:
            res.failed += 1
            if debug:
                print("FAILED:", e)
            try:
                ActionChains(driver).send_keys(Keys.ESCAPE).perform()
            except Exception:
                pass
            time.sleep(0.6)

    return res


def hide_window_offscreen(driver, debug: bool = False) -> None:
    """
    Make a normal (non-headless) Chrome window effectively invisible by moving it off-screen.
    """
    try:
        driver.set_window_position(-32000, -32000)
        driver.minimize_window()
        if debug:
            print("[INFO] Chrome window moved off-screen and minimized.")
    except Exception as e:
        if debug:
            print("[WARN] Could not move/minimize window:", e)


def main():
    ap = argparse.ArgumentParser(description="eBay Seller Hub: restock Available quantity 0→1")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-items", type=int, default=200)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--profile-dir", type=str, default="")

    # Keep your existing switches (compatibility)
    ap.add_argument("--no-headless", action="store_true", help="Run Chrome with a visible UI renderer (non-headless)")
    ap.add_argument("--hide-window", action="store_true", help="Non-headless, but move Chrome off-screen/minimize")
    ap.add_argument("--no-kill-profile", action="store_true", help="Do NOT kill stale Chrome using the same profile")

    # New override: allow showing the window while non-headless
    ap.add_argument("--show-window", action="store_true", help="Force showing the Chrome window (non-headless only)")

    args = ap.parse_args()

    options = webdriver.ChromeOptions()

    #
    # NEW DEFAULT:
    #   - non-headless (because eBay UI can differ in real headless)
    #   - hidden window (equivalent to --no-headless --hide-window)
    #
    headless = False  # default is NOT headless
    hide_window = True  # default is hidden

    # If user explicitly asks for headless behavior by NOT setting --no-headless previously,
    # we keep your old meaning: headless is only used when --no-headless is NOT provided.
    # But since we changed default to non-headless, we need an explicit way to go headless.
    # To avoid breaking your CLI, we interpret:
    #   - if user does NOT pass --no-headless AND does NOT pass --hide-window and does NOT pass --show-window,
    #     we still stay non-headless by default (hidden).
    # If you want true headless, add it as an explicit flag later.
    #
    # For now: preserve your working non-headless path; headless is only enabled if you remove this default.
    #
    if args.no_headless:
        headless = False
    else:
        # KEEP default non-headless; do NOT switch to true headless silently.
        headless = False

    # Determine hiding behavior:
    # - default: hidden (unless --show-window)
    # - if user passes --hide-window explicitly, keep hidden
    # - if user passes --show-window, show it
    if args.show_window:
        hide_window = False
    elif args.hide_window:
        hide_window = True
    else:
        hide_window = True  # default

    # Configure Chrome options
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
    else:
        options.add_argument("--window-size=1200,900")
        if hide_window:
            options.add_argument("--window-position=-32000,-32000")

    if args.profile_dir.strip():
        profile_dir = Path(args.profile_dir).expanduser().resolve()
    else:
        profile_dir = Path(__file__).with_name("chrome_profile_selenium").resolve()

    profile_dir.mkdir(parents=True, exist_ok=True)

    if not args.no_kill_profile:
        kill_chrome_using_profile(str(profile_dir), debug=args.debug)

    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-notifications")
    options.add_argument("--remote-debugging-port=0")

    driver = webdriver.Chrome(options=options)

    # If requested (or default), hide it after startup as well (more reliable than flags alone)
    if (not headless) and hide_window:
        hide_window_offscreen(driver, debug=args.debug)

    try:
        driver.get(ACTIVE_URL)
        ensure_logged_in_or_pause(driver)
        driver.get(ACTIVE_URL)

        click_available_qty_header_until_ascending(driver, timeout=args.timeout, debug=args.debug)

        res = restock_all_zero_to_one(
            driver,
            timeout=args.timeout,
            max_items=args.max_items,
            dry_run=args.dry_run,
            debug=args.debug,
        )

        print("\nSummary")
        print(f"  Attempted: {res.attempted}")
        print(f"  Updated:   {res.updated}")
        print(f"  Skipped:   {res.skipped_nonzero}")
        print(f"  Failed:    {res.failed}")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
