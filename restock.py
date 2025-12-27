#!/usr/bin/env python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import argparse
import time

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
    cur = driver.current_url.lower()
    if "signin" in cur or "login" in cur:
        print("Redirected to sign-in. Log in in the opened browser window, then press Enter here.")
        input()


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


def main():
    ap = argparse.ArgumentParser(description="eBay Seller Hub: restock Available quantity 0→1")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-items", type=int, default=200)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--profile-dir", type=str, default="")
    ap.add_argument("--no-headless", action="store_true", help="Run Chrome with a visible window")
    args = ap.parse_args()

    options = webdriver.ChromeOptions()

    # DEFAULT = headless
    if not args.no_headless:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")

    if args.profile_dir.strip():
        profile_dir = Path(args.profile_dir).expanduser().resolve()
    else:
        profile_dir = Path(__file__).with_name("chrome_profile_selenium").resolve()

    profile_dir.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={profile_dir}")

    driver = webdriver.Chrome(options=options)
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
