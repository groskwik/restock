#!/usr/bin/env python
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

# Column header you provided
AVAILABLE_QTY_HEADER_XPATH = "//span[contains(@class,'th-title-content') and normalize-space()='Available quantity']"

# Restock button you provided
RESTOCK_BTN_XPATH = "//button[normalize-space()='Restock' and contains(@class,'primary-action__button')]"

# Edit quantity input you provided (in the restock dialog)
QTY_INPUT_CSS = "input[name='members[0][availableQuantity]']"

# Submit button in the restock dialog
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
    """
    Robustly replace the value in a text input and trigger input/change events.
    """
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)

    try:
        el.click()
    except Exception:
        js_click(driver, el)

    # Clear using keyboard (more reliable than .clear() on some UIs)
    el.send_keys(Keys.CONTROL, "a")
    el.send_keys(Keys.BACKSPACE)

    el.send_keys(value)

    # Fire events for React-style listeners
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
    """
    eBay tables often use aria-sort on the header/cell.
    We try to find an ancestor that carries aria-sort and click until it is 'ascending'.
    If aria-sort isn't available, we click once or twice and proceed.
    """
    wait = WebDriverWait(driver, timeout)

    header_span = wait.until(EC.presence_of_element_located((By.XPATH, AVAILABLE_QTY_HEADER_XPATH)))

    # Try to locate a container that has aria-sort (th or button-like element)
    def find_sort_container():
        span = driver.find_element(By.XPATH, AVAILABLE_QTY_HEADER_XPATH)
        # Search up a few levels for aria-sort
        for _ in range(6):
            parent = span.find_element(By.XPATH, "..")
            aria_sort = parent.get_attribute("aria-sort")
            if aria_sort:
                return parent
            span = parent
        return None

    sort_container = find_sort_container()

    if debug:
        print("Sort container aria-sort:", sort_container.get_attribute("aria-sort") if sort_container else None)

    # If we have aria-sort, click until ascending.
    if sort_container:
        for i in range(max_clicks):
            aria_sort = sort_container.get_attribute("aria-sort")
            if aria_sort == "ascending":
                if debug:
                    print("Already sorted ascending by Available quantity.")
                return

            if debug:
                print(f"Clicking Available quantity header (attempt {i+1}) to reach ascending… current aria-sort={aria_sort!r}")

            safe_click(driver, sort_container)

            # Wait for aria-sort to change or table to refresh
            time.sleep(0.6)
            try:
                wait.until(lambda d: (find_sort_container() or sort_container).get_attribute("aria-sort") != aria_sort)
            except TimeoutException:
                pass

            # Reacquire in case DOM was re-rendered
            sort_container = find_sort_container() or sort_container

        if debug:
            print("Reached max clicks; proceeding with whatever sort direction is currently set:",
                  sort_container.get_attribute("aria-sort"))
        return

    # Fallback: click the visible span itself (toggle) twice at most
    if debug:
        print("No aria-sort found; using fallback clicks on the header span.")
    for i in range(2):
        header_span = wait.until(EC.element_to_be_clickable((By.XPATH, AVAILABLE_QTY_HEADER_XPATH)))
        safe_click(driver, header_span)
        time.sleep(0.8)


@dataclass
class RestockResult:
    attempted: int = 0
    updated: int = 0
    skipped_nonzero: int = 0
    failed: int = 0


def restock_all_zero_to_one(driver, timeout=30, max_items=200, dry_run=False, debug=True) -> RestockResult:
    """
    Finds all visible 'Restock' buttons (after sorting), clicks each, changes 0->1, submits.
    Stops after max_items attempted.
    """
    wait = WebDriverWait(driver, timeout)
    res = RestockResult()

    # Wait for at least the table/action area to exist by waiting for any Restock buttons or for page body
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

    # We will iterate by repeatedly re-querying buttons because DOM can change after each submit.
    while res.attempted < max_items:
        try:
            buttons = driver.find_elements(By.XPATH, RESTOCK_BTN_XPATH)
        except StaleElementReferenceException:
            buttons = []

        # Filter visible/enabled
        buttons = [b for b in buttons if b.is_displayed() and b.is_enabled()]

        if not buttons:
            if debug:
                print("No more visible Restock buttons found. Stopping.")
            break

        # Always take the first visible restock button; after submit, table may refresh.
        btn = buttons[0]
        res.attempted += 1

        try:
            if debug:
                print(f"[{res.attempted}] Clicking Restock…")
            safe_click(driver, btn)

            # Wait for the quantity input to appear in the dialog/pane
            qty_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, QTY_INPUT_CSS)))
            wait.until(lambda d: d.find_element(By.CSS_SELECTOR, QTY_INPUT_CSS).is_displayed())

            # Read current value
            cur_val = qty_input.get_attribute("value") or ""
            cur_val_s = cur_val.strip()

            if debug:
                print(f"    Current availableQuantity value: {cur_val_s!r}")

            if cur_val_s not in ("0", ""):
                res.skipped_nonzero += 1
                if debug:
                    print("    Not zero; skipping this one (will try to close dialog).")
                # Try to close dialog by ESC
                ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                time.sleep(0.4)
                continue

            if dry_run:
                if debug:
                    print("    DRY RUN: would set quantity to 1 and submit.")
                ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                time.sleep(0.4)
                continue

            # Set value to 1
            qty_input = driver.find_element(By.CSS_SELECTOR, QTY_INPUT_CSS)  # reacquire
            set_input_value(driver, qty_input, "1")

            # Submit
            submit_btn = wait.until(EC.element_to_be_clickable((By.XPATH, SUBMIT_BTN_XPATH)))
            if debug:
                print("    Clicking Submit…")
            safe_click(driver, submit_btn)

            # Wait for dialog to go away OR for the input to become stale
            try:
                wait.until(EC.staleness_of(submit_btn))
            except TimeoutException:
                # Sometimes it doesn't go stale; wait until input disappears
                try:
                    wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, QTY_INPUT_CSS)) == 0)
                except TimeoutException:
                    pass

            res.updated += 1
            if debug:
                print("    Updated to 1.")

            # Small pause to allow table refresh
            time.sleep(0.8)

        except Exception as e:
            res.failed += 1
            if debug:
                print("    FAILED:", repr(e))
            # Attempt to recover by closing any open dialog
            try:
                ActionChains(driver).send_keys(Keys.ESCAPE).perform()
            except Exception:
                pass
            time.sleep(0.6)

    return res


def main():
    ap = argparse.ArgumentParser(description="eBay Seller Hub Active Listings: sort by Available quantity and restock 0->1.")
    ap.add_argument("--dry-run", action="store_true", help="Do not submit changes; just simulate.")
    ap.add_argument("--max-items", type=int, default=200, help="Maximum restocks to attempt in one run.")
    ap.add_argument("--timeout", type=int, default=30, help="WebDriverWait timeout seconds.")
    ap.add_argument("--debug", action="store_true", help="Verbose logging.")
    ap.add_argument("--profile-dir", type=str, default="", help="Chrome user-data-dir to reuse login; default creates ./chrome_profile_selenium")
    ap.add_argument("--headless", action="store_true", help="Run Chrome in headless mode")
    args = ap.parse_args()

    options = webdriver.ChromeOptions()

    # Headless support (explicit opt-in)
    if args.headless:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")

    # Dedicated profile (recommended) so you stay logged in
    if args.profile_dir.strip():
        profile_dir = Path(args.profile_dir).expanduser().resolve()
    else:
        profile_dir = Path(__file__).with_name("chrome_profile_selenium").resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={str(profile_dir)}")

    driver = webdriver.Chrome(options=options)
    try:
        driver.get(ACTIVE_URL)
        ensure_logged_in_or_pause(driver)

        # Reload after login if needed
        driver.get(ACTIVE_URL)

        # Sort by Available quantity (ascending = least -> most)
        click_available_qty_header_until_ascending(driver, timeout=args.timeout, debug=args.debug)

        # Process restocks
        res = restock_all_zero_to_one(
            driver,
            timeout=args.timeout,
            max_items=args.max_items,
            dry_run=args.dry_run,
            debug=args.debug,
        )

        print("\nSummary of restocking:")
        print(f"  Attempted:       {res.attempted}")
        print(f"  Updated (0->1):  {res.updated}")
        print(f"  Skipped nonzero: {res.skipped_nonzero}")
        print(f"  Failed:          {res.failed}")

        input("\nDone. Press Enter...")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()

