# ============================================================
# TEAM NOTE:
# This script grows config/companies.json automatically, without
# a paid search API. It works by GUESSING likely Ashby/Greenhouse
# URL slugs from a company's name (e.g. "Notion Labs" -> "notion")
# and checking whether that URL actually exists.
#
# This is NOT perfect — some companies use board slugs that don't
# match their name at all, so this will miss some real matches.
# Andrew's manual research is still valuable as a backfill for
# whatever this script doesn't catch. Think of this as
# "cheaply catch the easy 60-70%", not "fully solve this."
#
# Data sources (both free, no API key required):
#   - S&P 500 company list: github.com/datasets/s-and-p-500-companies
#     (small, ~503 companies, barely changes — re-checked in full
#     every run, which is cheap)
#   - Y Combinator company list: github.com/yc-oss/api (public,
#     updated daily, ~5,900+ companies total). Since that's a much
#     bigger list, this script only checks a BATCH of it per run
#     (see fetch_yc_names_batch) and remembers where it left off in
#     config/discovery_state.json — so running this on a weekly cron
#     schedule gradually works through the entire YC directory over
#     time, instead of re-checking the same slice every time.
#
# Optional: Selenium-based discovery (find_ats_link_via_selenium /
# run_selenium_pass). Instead of guessing a slug or a tenant/shard/
# site combo blind, this visits the company's OWN website directly
# (guessing {company}.com), finds their real "Careers" link, and
# follows it — reading the actual ATS URL from wherever it lands.
# This is NOT the same as scraping Google's search results (which
# violates Google's ToS for automated querying) — visiting a
# company's own public site and clicking a real link on it is not
# a ToS issue.
#
# Requires: pip install selenium webdriver-manager
# (webdriver-manager auto-downloads the right Chrome driver version,
# so you don't need to manually install chromedriver yourself).
#
# Run it with:
#   python3 src/discover_companies.py
#
# It will PRINT what it found and APPEND new confirmed companies
# to config/companies.json (existing entries are never touched or
# duplicated — it checks names already in the file first).
# ============================================================

import json
import os
import re
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

CONFIG_PATH = Path("config/companies.json")
UNMATCHED_LOG_PATH = Path("config/unmatched_companies.txt")
DISCOVERY_STATE_PATH = Path("config/discovery_state.json")

SP500_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/"
    "s-and-p-500-companies/main/data/constituents.csv"
)
YC_COMPANIES_URL = "https://yc-oss.github.io/api/companies/all.json"

REQUEST_TIMEOUT = 10
DELAY_BETWEEN_CHECKS = 0.3  # be polite to Ashby/Greenhouse's servers

# Single-word slugs too generic to trust even with name verification —
# these are common enough as board slugs that they risk matching some
# OTHER company entirely (this is what caused "General Dynamics",
# "General Mills", and "General Motors" to all match slug "general").
GENERIC_SLUG_BLOCKLIST = {
    "general", "public", "charles", "international", "national",
    "united", "american", "global", "us", "first", "the", "group",
    "systems", "solutions", "services", "industries", "holdings",
}

# Workday shards actually seen in use, ordered by confirmed real-world
# frequency (wd1 confirmed for Boeing AND Medtronic, wd5 for Caterpillar
# — verified 2026-08-16 via live search, not guessed) — wd1 first since
# it's hit the most so far.
WORKDAY_SHARDS_TO_TRY = [1, 5, 3, 2, 12]

# Common Workday career-site naming patterns, seen across real tenants
# in the wild. Companies name these inconsistently, so this list will
# never be exhaustive — it's a bounded guess, not a guarantee.
COMMON_WORKDAY_SITE_NAMES = [
    "External",
    "ExternalCareerSite",
    "External_Career_Site",
    "Careers",
    "Global_Careers",
    "Student_Careers",
    "University_Recruiting",
    "Careers_Site",
]


def slugify(name: str) -> list[str]:
    """
    Given a company name, generate a few likely slug guesses.
    Returns them in order of likelihood (most obvious guess first).
    """

    # Strip common corporate suffixes that rarely appear in slugs
    cleaned = re.sub(
        r"\b(inc\.?|corp\.?|corporation|co\.?|ltd\.?|llc|holdings|group|"
        r"the|company)\b",
        "",
        name,
        flags=re.IGNORECASE,
    )
    cleaned = cleaned.strip()

    lower_no_space = re.sub(r"[^a-z0-9]", "", cleaned.lower())
    hyphenated = re.sub(r"[^a-z0-9]+", "-", cleaned.lower()).strip("-")
    first_word = re.sub(r"[^a-z0-9]", "", cleaned.lower().split()[0]) if cleaned else ""

    guesses = [lower_no_space, hyphenated, first_word]

    # De-duplicate while preserving order
    seen = set()
    unique_guesses = []
    for g in guesses:
        if g and g not in seen:
            seen.add(g)
            unique_guesses.append(g)

    return unique_guesses


def _slug_words(name: str) -> set[str]:
    """Breaks a company name into lowercase word tokens for name matching."""

    cleaned = re.sub(
        r"\b(inc\.?|corp\.?|corporation|co\.?|ltd\.?|llc|holdings|group|"
        r"the|company|class a|class b|\(class a\)|\(class b\))\b",
        "",
        name,
        flags=re.IGNORECASE,
    )
    words = re.findall(r"[a-z0-9]+", cleaned.lower())
    # Drop very short/common tokens that cause false positives
    # (e.g. "general" matching General Dynamics/Mills/Motors alike)
    return {w for w in words if len(w) > 2}


def _board_name_matches(company_name: str, board_company_name: str) -> bool:
    """
    Checks whether the board's own company name plausibly refers to the
    company we searched for. This is what catches false positives like
    a generic slug ("general", "public", "charles") resolving to some
    OTHER company entirely.
    """

    if not board_company_name:
        return False

    query_words = _slug_words(company_name)
    board_words = _slug_words(board_company_name)

    if not query_words or not board_words:
        return False

    # Require meaningful word overlap, not just "a board exists here"
    overlap = query_words & board_words
    return len(overlap) >= 1 and len(overlap) / len(query_words) >= 0.5


def check_ashby(slug: str, company_name: str) -> bool:
    """
    Returns True only if this slug resolves to a real Ashby job board
    AND the board's own listed company name plausibly matches the
    company we're searching for (prevents false positives from
    generic slug guesses like "general" or "public").
    """

    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            return False
        data = response.json()
        if not data.get("jobs"):
            return False
        board_company_name = data.get("organizationName", "") or ""
        return _board_name_matches(company_name, board_company_name)
    except (requests.RequestException, ValueError):
        return False


def check_greenhouse(slug: str, company_name: str) -> bool:
    """
    Returns True only if this slug resolves to a real Greenhouse job
    board AND the board's own listed company name plausibly matches
    the company we're searching for (see check_ashby docstring).
    """

    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            return False
        data = response.json()
        jobs = data.get("jobs")
        if not jobs:
            return False
        # Greenhouse's jobs endpoint doesn't return an org name directly,
        # but each job includes the company name in its metadata/URL —
        # check the first job's absolute_url or company field if present.
        first_job = jobs[0]
        board_company_name = (
            first_job.get("company_name")
            or first_job.get("metadata", {}).get("company_name", "")
            or ""
        )
        if board_company_name:
            return _board_name_matches(company_name, board_company_name)
        # Fallback: if Greenhouse doesn't expose a company name field,
        # require the slug itself to closely match the company name
        # rather than accepting any generic word match.
        return slug in _slug_words(company_name) or _slug_words(company_name) <= {slug}
    except (requests.RequestException, ValueError):
        return False


def find_ats_link_via_selenium(company_name: str, driver=None) -> dict | None:
    """
    Visits the company's own likely website (guessing {slug}.com) and
    crawls up to MAX_HOPS pages deep, following the most job-related
    link on each page, until it lands on a real ATS URL (or gives up).

    Why multiple hops: many large companies don't link straight from
    their homepage to their ATS. The real path is often homepage ->
    "Careers" (a marketing/life-at-the-company landing page) ->
    "Search Jobs" / "View Openings" (THIS is what actually goes to
    Workday/Ashby/Greenhouse). A single-hop follow stops one page too
    early for companies structured this way — this was confirmed
    directly: Lockheed Martin's "Careers" link lands on a marketing
    page (life-at-lm.html), not Workday itself; a second hop from
    there is needed.

    `driver` can be passed in (a shared Selenium webdriver instance)
    to avoid the overhead of starting a new browser for every single
    company when calling this in a loop — see run_selenium_pass().

    Returns None if the guessed domain doesn't resolve, no relevant
    link is found on any hop, or MAX_HOPS is reached without landing
    on a known ATS pattern (Ashby/Greenhouse/Workday).
    """

    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import (
        WebDriverException,
        TimeoutException,
        NoSuchElementException,
        StaleElementReferenceException,
    )

    MAX_HOPS = 3
    # Hop 1 (homepage) uses broad markers to find the initial
    # "Careers" link. Later hops use MORE SPECIFIC markers, since a
    # careers landing page usually has its own "Careers" link (self-
    # referential, would loop forever) but the real next step is
    # phrased differently — "search jobs", "view openings", etc.
    HOP_MARKERS = [
        ("career", "jobs", "join us", "join our team"),
        ("search jobs", "view openings", "view all jobs", "current openings",
         "find a job", "explore opportunities", "job search", "browse jobs",
         "see open roles", "open positions"),
    ]

    def find_best_link(markers: tuple[str, ...], debug_label: str = "") -> str | None:
        try:
            candidates = driver.find_elements(By.TAG_NAME, "a")
        except (NoSuchElementException, WebDriverException):
            return None

        all_texts_seen = []  # for debug output if nothing matches

        for element in candidates:
            try:
                text = (element.text or "").strip().lower()
                href = (element.get_attribute("href") or "").lower()
            except StaleElementReferenceException:
                continue

            if text or href:
                all_texts_seen.append(text or href[:60])

            if any(marker in text or marker in href for marker in markers):
                try:
                    href_value = element.get_attribute("href")
                except StaleElementReferenceException:
                    continue
                if href_value:
                    return href_value

        if debug_label:
            # Nothing matched — show what link text WAS on the page,
            # so we can see the actual wording to add to HOP_MARKERS.
            non_empty = [t for t in all_texts_seen if t][:40]
            print(f"  [{debug_label}] no marker matched. Link text seen "
                  f"on page ({len(non_empty)} shown, deduped): "
                  f"{sorted(set(non_empty))}")

        return None

    def check_iframes_for_ats() -> str | None:
        try:
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
        except (NoSuchElementException, WebDriverException):
            return None
        for iframe in iframes:
            try:
                src = iframe.get_attribute("src") or ""
            except StaleElementReferenceException:
                continue
            if _looks_like_ats_url(src):
                return src
        return None

    def scan_page_source_for_ats() -> str | None:
        """
        Scans the RAW page source (full HTML/JS, not just visible
        clickable links) for any URL matching a known ATS domain
        pattern. This catches cases a plain <a> tag search misses —
        e.g. a "Search Jobs" button that's actually a <button> with a
        JS onclick handler, a URL embedded in a script tag or data
        attribute, or a link inside a dropdown/mega-menu that isn't
        in the DOM as a normal clickable element. Companies often
        reference their real career-site URL SOMEWHERE in the page
        even when it's not a simple visible link with matching text.
        """

        try:
            source = driver.page_source
        except WebDriverException:
            return None

        for pattern in (
            r"https://[\w.-]+\.wd\d+\.myworkdayjobs\.com/[\w/%-]+",
            r"https://jobs\.ashbyhq\.com/[\w-]+",
            r"https://(?:boards|job-boards)\.greenhouse\.io/[\w-]+",
        ):
            match = re.search(pattern, source)
            if match:
                return match.group(0)

        return None

    owns_driver = driver is None
    if owns_driver:
        driver = _build_selenium_driver()

    try:
        for slug in slugify(company_name)[:2]:  # try top 2 slug guesses only
            domain = f"https://{slug}.com"

            try:
                driver.set_page_load_timeout(15)
                driver.get(domain)
                # Many corporate sites render their real nav menu via
                # JS shortly AFTER the page "load" event fires — give
                # it a moment before searching for links.
                time.sleep(2)
                print(f"  [{company_name}] loaded {domain} "
                      f"(landed on: {driver.current_url})")
            except (WebDriverException, TimeoutException) as e:
                print(f"  [{company_name}] {domain} failed to load: "
                      f"{type(e).__name__}")
                continue

            current_url = driver.current_url

            # NEW: scan the homepage's raw source FIRST, before even
            # trying to find/click a link — this alone can solve
            # cases like Lockheed's, where the real career-site URL
            # exists somewhere in the page's HTML/JS but isn't a
            # plain visible <a> link with matching text.
            source_ats_url = scan_page_source_for_ats()
            if source_ats_url:
                result = _parse_ats_url(source_ats_url, company_name)
                if result:
                    print(f"  [{company_name}] found ATS URL directly in "
                          f"page source: {source_ats_url}")
                    return result

            for hop in range(MAX_HOPS):
                markers = HOP_MARKERS[min(hop, len(HOP_MARKERS) - 1)]
                next_link = find_best_link(markers, debug_label=company_name)

                if not next_link or next_link == current_url:
                    print(f"  [{company_name}] hop {hop + 1}: no further "
                          f"job-related link found, stopping")
                    break

                print(f"  [{company_name}] hop {hop + 1}: following {next_link}")

                try:
                    driver.get(next_link)
                    time.sleep(2)
                except (WebDriverException, TimeoutException) as e:
                    print(f"  [{company_name}] hop {hop + 1} failed to load: "
                          f"{type(e).__name__}")
                    break

                current_url = driver.current_url
                print(f"  [{company_name}] hop {hop + 1} landed on: {current_url}")

                if _looks_like_ats_url(current_url):
                    result = _parse_ats_url(current_url, company_name)
                    if result:
                        return result

                iframe_ats_url = check_iframes_for_ats()
                if iframe_ats_url:
                    result = _parse_ats_url(iframe_ats_url, company_name)
                    if result:
                        return result

                # NEW: also scan this hop's page source, same reasoning
                # as the homepage scan above.
                source_ats_url = scan_page_source_for_ats()
                if source_ats_url:
                    result = _parse_ats_url(source_ats_url, company_name)
                    if result:
                        print(f"  [{company_name}] found ATS URL directly "
                              f"in page source: {source_ats_url}")
                        return result

        return None

    finally:
        if owns_driver:
            driver.quit()


def _looks_like_ats_url(url: str) -> bool:
    return any(
        marker in url
        for marker in ("myworkdayjobs.com", "ashbyhq.com", "greenhouse.io")
    )


def _parse_ats_url(url: str, company_name: str) -> dict | None:
    """Shared with search_for_ats_link — extracts ATS details from a real URL."""

    workday_match = re.search(
        r"https://([\w-]+)\.wd(\d+)\.myworkdayjobs\.com/(?:[\w-]+/)?([\w-]+)",
        url,
    )
    if workday_match:
        tenant, shard, site = workday_match.groups()
        return {
            "name": company_name,
            "ats": "workday",
            "host": f"https://{tenant}.wd{shard}.myworkdayjobs.com",
            "tenant": tenant,
            "site": site,
            "locale": "en-US",
            "enabled": True,
        }

    ashby_match = re.search(r"jobs\.ashbyhq\.com/([\w-]+)", url)
    if ashby_match:
        return {
            "name": company_name,
            "ats": "ashby",
            "board_name": ashby_match.group(1),
            "enabled": True,
        }

    greenhouse_match = re.search(
        r"(?:boards\.greenhouse\.io|job-boards\.greenhouse\.io)/([\w-]+)",
        url,
    )
    if greenhouse_match:
        return {
            "name": company_name,
            "ats": "greenhouse",
            "board_token": greenhouse_match.group(1),
            "enabled": True,
        }

    return None


def _build_selenium_driver():
    """Builds a headless Chrome driver, auto-downloading the right version."""

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def run_selenium_pass(limit: int = 50) -> None:
    """
    Runs find_ats_link_via_selenium() against companies from
    unmatched_companies.txt, reusing ONE browser instance across all
    of them (much faster than starting a new browser per company).

    Slower per-company than the API-based checks (real page loads,
    not lightweight API calls), so start with a modest `limit` to
    gauge how long a full pass takes before scaling up. Consider
    running this as a background/overnight job for a large batch.

    A miss here just stays in unmatched_companies.txt — it's NOT
    tracked as "confirmed not Workday" anywhere. Automated checks
    (this one included) have a real, confirmed false-negative rate —
    e.g. Boeing was missed by this exact method but found through
    manual research minutes later. Treating an automated miss as a
    reliable "this company doesn't use Workday" signal would be
    actively misleading, so this only records what it actually knows:
    a company IS Workday when it finds one, and says nothing definite
    when it doesn't.
    """

    if not UNMATCHED_LOG_PATH.exists():
        print(f"{UNMATCHED_LOG_PATH} doesn't exist yet — run the main "
              f"discovery pass first.")
        return

    with UNMATCHED_LOG_PATH.open("r", encoding="utf-8") as file:
        all_unmatched = [line.strip() for line in file if line.strip()]

    candidates = all_unmatched[:limit]

    print(f"Trying Selenium discovery for {len(candidates)} companies "
          f"from {UNMATCHED_LOG_PATH}. This is slower than the API-based "
          f"checks — real page loads, not lightweight requests.\n")

    existing = load_existing_companies()
    existing_names = {c["name"].lower() for c in existing}
    found = []

    # PER_COMPANY_TIMEOUT_SECONDS is a hard wall-clock ceiling, separate
    # from (and more reliable than) Selenium's own page_load_timeout /
    # set_script_timeout — those can fail to abort cleanly on certain
    # hangs (confirmed: a real 20+ minute hang happened on one company
    # despite both being set). If a company exceeds this, we forcibly
    # KILL the underlying chromedriver process — not just call
    # driver.quit(), which can itself hang if the driver is stuck mid-
    # command — and start a fresh browser for the next company.
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
    PER_COMPANY_TIMEOUT_SECONDS = 90

    executor = ThreadPoolExecutor(max_workers=1)
    driver = _build_selenium_driver()
    try:
        for name in candidates:
            if name.lower() in existing_names:
                continue

            future = executor.submit(find_ats_link_via_selenium, name, driver)
            try:
                result = future.result(timeout=PER_COMPANY_TIMEOUT_SECONDS)
            except FutureTimeoutError:
                print(f"TIMED OUT on {name} after {PER_COMPANY_TIMEOUT_SECONDS}s "
                      f"— killing the browser and starting fresh for the next company")
                try:
                    driver.service.process.kill()
                except Exception:
                    pass
                driver = _build_selenium_driver()
                continue
            except Exception as error:
                # Catches anything unexpected we didn't specifically
                # handle inside find_ats_link_via_selenium (a crashed
                # tab, an unusual redirect, etc.) — one bad company
                # should never take down the rest of a 50-company run.
                print(f"Skipped {name} after an error: {error}")
                continue

            if result:
                detail = (
                    result.get("board_name")
                    or result.get("board_token")
                    or f"{result.get('tenant')}/{result.get('site')}"
                )
                print(f"FOUND (selenium): {name} -> {result['ats']} ({detail})")
                found.append(result)
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        executor.shutdown(wait=False, cancel_futures=True)

    if found:
        updated = existing + found
        save_companies(updated)
        print(f"\nAdded {len(found)} companies to {CONFIG_PATH}")

        # Only remove companies we actually FOUND from the unmatched
        # list — everything else (misses, timeouts, errors) stays put
        # for a future pass or manual research to try again.
        found_names = {c["name"].lower() for c in found}
        remaining_unmatched = [n for n in all_unmatched if n.lower() not in found_names]
        _write_list(UNMATCHED_LOG_PATH, remaining_unmatched)
        print(f"Removed {len(all_unmatched) - len(remaining_unmatched)} "
              f"newly-found companies from {UNMATCHED_LOG_PATH} "
              f"({len(remaining_unmatched)} remaining)")
    else:
        print("\nNo matches found this run. unmatched_companies.txt unchanged.")


def search_for_ats_link(company_name: str) -> dict | None:
    """
    Uses Google Custom Search's free tier to find a company's actual
    ATS job board link (Workday, Ashby, or Greenhouse), instead of
    guessing tenant/shard/site combinations blind.

    This is the more reliable path for Workday specifically, since
    Workday needs 3 things to line up (tenant, shard, site name) and
    there's no public directory to guess against — searching finds
    the REAL link a human would find, same as clicking "Careers" on
    the company's own site.

    Requires GOOGLE_SEARCH_API_KEY and GOOGLE_SEARCH_CX in .env (see
    the team note at the top of this file for setup steps). Returns
    None (not an error) if either is missing, so the rest of
    discovery still works without search configured.

    Free tier: 100 queries/day. At that limit, only run this against
    a bounded batch (see run_search_pass below), not your whole
    candidate list at once.
    """

    api_key = os.getenv("GOOGLE_SEARCH_API_KEY")
    cx = os.getenv("GOOGLE_SEARCH_CX")

    if not api_key or not cx:
        return None

    query = f"{company_name} careers workday OR ashbyhq OR greenhouse"

    try:
        response = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": api_key, "cx": cx, "q": query, "num": 3},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            return None
        results = response.json().get("items", [])
    except (requests.RequestException, ValueError):
        return None

    for item in results:
        link = item.get("link", "")

        workday_match = re.search(
            r"https://([\w-]+)\.wd(\d+)\.myworkdayjobs\.com/(?:[\w-]+/)?([\w-]+)",
            link,
        )
        if workday_match:
            tenant, shard, site = workday_match.groups()
            return {
                "name": company_name,
                "ats": "workday",
                "host": f"https://{tenant}.wd{shard}.myworkdayjobs.com",
                "tenant": tenant,
                "site": site,
                "locale": "en-US",
                "enabled": True,
            }

        ashby_match = re.search(r"jobs\.ashbyhq\.com/([\w-]+)", link)
        if ashby_match:
            board_name = ashby_match.group(1)
            if check_ashby(board_name, company_name):
                return {
                    "name": company_name,
                    "ats": "ashby",
                    "board_name": board_name,
                    "enabled": True,
                }

        greenhouse_match = re.search(
            r"(?:boards\.greenhouse\.io|job-boards\.greenhouse\.io)/([\w-]+)",
            link,
        )
        if greenhouse_match:
            board_token = greenhouse_match.group(1)
            if check_greenhouse(board_token, company_name):
                return {
                    "name": company_name,
                    "ats": "greenhouse",
                    "board_token": board_token,
                    "enabled": True,
                }

    return None


def run_search_pass(limit: int = 90) -> None:
    """
    Runs search_for_ats_link() against companies from
    unmatched_companies.txt. Defaults to 90 (not 100) to leave a
    little headroom under the free tier's 100 queries/day cap for
    any other testing you might do the same day.

    A miss here just stays in unmatched_companies.txt — it's NOT
    tracked as "confirmed not Workday" anywhere. Search results can
    miss for reasons unrelated to whether a company actually uses
    Workday (an unusual URL structure, a company using multiple
    career-site paths, etc.), so treating a miss as reliable negative
    evidence would be misleading.
    """

    if not UNMATCHED_LOG_PATH.exists():
        print(f"{UNMATCHED_LOG_PATH} doesn't exist yet — run the main "
              f"discovery pass first.")
        return

    if not os.getenv("GOOGLE_SEARCH_API_KEY") or not os.getenv("GOOGLE_SEARCH_CX"):
        print("GOOGLE_SEARCH_API_KEY and/or GOOGLE_SEARCH_CX not set in "
              ".env — see the team note at the top of this file for setup.")
        return

    with UNMATCHED_LOG_PATH.open("r", encoding="utf-8") as file:
        all_unmatched = [line.strip() for line in file if line.strip()]

    candidates = all_unmatched[:limit]

    print(f"Searching for ATS links for {len(candidates)} companies "
          f"from {UNMATCHED_LOG_PATH} — 1 search API call each...\n")

    existing = load_existing_companies()
    existing_names = {c["name"].lower() for c in existing}
    found = []

    for name in candidates:
        if name.lower() in existing_names:
            continue

        result = search_for_ats_link(name)
        if result:
            detail = (
                result.get("board_name")
                or result.get("board_token")
                or f"{result.get('tenant')}/{result.get('site')}"
            )
            print(f"FOUND (search): {name} -> {result['ats']} ({detail})")
            found.append(result)

    if found:
        updated = existing + found
        save_companies(updated)
        print(f"\nAdded {len(found)} companies to {CONFIG_PATH}")

        found_names = {c["name"].lower() for c in found}
        remaining_unmatched = [n for n in all_unmatched if n.lower() not in found_names]
        _write_list(UNMATCHED_LOG_PATH, remaining_unmatched)
        print(f"Removed {len(all_unmatched) - len(remaining_unmatched)} "
              f"newly-found companies from {UNMATCHED_LOG_PATH} "
              f"({len(remaining_unmatched)} remaining)")
    else:
        print("\nNo matches found this run. unmatched_companies.txt unchanged.")


def check_workday(company_name: str) -> dict | None:
    """
    Attempts to guess a company's Workday tenant/shard/site combination.

    This is fundamentally harder than Ashby/Greenhouse: those need ONE
    guessed slug, Workday needs THREE things to line up at once (the
    tenant slug, which numbered shard — wd1, wd2, wd3... — the tenant
    lives on, and the site name, which companies name inconsistently:
    "ExternalCareerSite", "External_Career_Site", "Careers", etc, OR
    a "{CompanyName}Careers" pattern — e.g. Boeing's real site name is
    "EXTERNAL_CAREERS", Caterpillar's is "CaterpillarCareers",
    Medtronic's is "MedtronicCareers", all confirmed 2026-08-16).
    There's no public directory mapping companies to their Workday
    tenant, so this is a real trade-off: a bounded number of guesses,
    not exhaustive — it will miss real matches whose site name isn't
    generated by either guessing strategy, or whose shard is outside
    the range checked.

    Verification is via tenant/company-name overlap in the URL only —
    Workday's job list API doesn't return an organization display name
    to cross-check against, unlike Ashby. Tenant collisions are rare
    (tenant is a deliberately chosen unique identifier, not a short
    generic word like the Ashby/Greenhouse slug collisions we saw),
    so this is lower-risk than the earlier false-positive issue, but
    still not a guarantee.
    """

    tenant_candidates = slugify(company_name)
    tenant_candidates = [t for t in tenant_candidates if t not in GENERIC_SLUG_BLOCKLIST]

    # NEW: company-specific site name guesses, tried FIRST since
    # they're more targeted than the generic list below. Discovered
    # from real, verified examples on 2026-08-16: Boeing uses
    # "EXTERNAL_CAREERS", Caterpillar uses "CaterpillarCareers",
    # Medtronic uses "MedtronicCareers" — a "{CompanyName}Careers"
    # style pattern shows up often enough to guess proactively rather
    # than only relying on the generic list.
    company_clean = re.sub(r"[^A-Za-z0-9]", "", company_name)
    company_specific_sites = [
        f"{company_clean}Careers",
        f"{company_clean}CareerSite",
        f"{company_clean}External",
    ]

    all_site_guesses = company_specific_sites + COMMON_WORKDAY_SITE_NAMES

    for tenant in tenant_candidates:
        for shard in WORKDAY_SHARDS_TO_TRY:
            for site in all_site_guesses:
                time.sleep(DELAY_BETWEEN_CHECKS)

                host = f"https://{tenant}.wd{shard}.myworkdayjobs.com"
                url = f"{host}/wday/cxs/{tenant}/{site}/jobs"

                try:
                    response = requests.post(
                        url,
                        json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                        timeout=REQUEST_TIMEOUT,
                    )
                    if response.status_code != 200:
                        continue
                    data = response.json()
                    if data.get("total", 0) > 0:
                        return {
                            "name": company_name,
                            "ats": "workday",
                            "host": host,
                            "tenant": tenant,
                            "site": site,
                            "locale": "en-US",
                            "enabled": True,
                        }
                except (requests.RequestException, ValueError):
                    continue

    return None


def discover_company(name: str) -> dict | None:
    """
    Tries each slug guess against Ashby, then Greenhouse. Returns a
    companies.json-shaped entry on the first VERIFIED hit (slug
    resolves AND the board's own company name matches), or None.

    Does NOT try Workday here — Workday guessing is far more
    expensive (many combinations per company) and lower-confidence,
    so it's a separate, opt-in pass — see check_workday() and
    run_workday_pass() below, meant to run only against the
    unmatched list, not the full candidate pool.
    """

    for slug in slugify(name):
        # Skip overly generic single-word slugs that are likely to
        # collide with an unrelated company (e.g. "general", "public",
        # "charles", "international", "us") — these caused false
        # positives before the name-matching check was added, and
        # they're risky enough to skip outright rather than rely on
        # the verification catching every case.
        if slug in GENERIC_SLUG_BLOCKLIST:
            continue

        time.sleep(DELAY_BETWEEN_CHECKS)

        if check_ashby(slug, name):
            return {
                "name": name,
                "ats": "ashby",
                "board_name": slug,
                "enabled": True,
            }

        time.sleep(DELAY_BETWEEN_CHECKS)

        if check_greenhouse(slug, name):
            return {
                "name": name,
                "ats": "greenhouse",
                "board_token": slug,
                "enabled": True,
            }

    return None


def fetch_sp500_names() -> list[str]:
    """Pulls company names from the free S&P 500 dataset."""

    response = requests.get(SP500_CSV_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    lines = response.text.splitlines()
    names = []
    for line in lines[1:]:  # skip CSV header
        # constituents.csv columns: Symbol,Security,GICS Sector,...
        parts = line.split(",")
        if len(parts) > 1:
            names.append(parts[1].strip('"'))

    return names


def fetch_yc_names_batch(batch_size: int = 300) -> list[str]:
    """
    Pulls the NEXT batch of company names from the YC directory,
    picking up where the previous run left off (tracked in
    DISCOVERY_STATE_PATH). This lets a scheduled cron job gradually
    work through YC's full ~5,900+ company directory over many runs,
    instead of re-checking the same fixed slice every time or needing
    someone to manually raise a limit.

    Once it reaches the end of the list, it loops back to the start —
    useful since YC's directory does grow over time, so a second pass
    can catch companies that were added after the first pass began.
    """

    response = requests.get(YC_COMPANIES_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    all_companies = [c["name"] for c in response.json() if c.get("name")]
    total = len(all_companies)

    state = load_discovery_state()
    offset = state.get("yc_offset", 0) % total if total else 0

    end = offset + batch_size
    if end <= total:
        batch = all_companies[offset:end]
    else:
        # Wrap around to the start of the list
        batch = all_companies[offset:] + all_companies[: end - total]

    state["yc_offset"] = end % total if total else 0
    save_discovery_state(state)

    print(f"YC directory has {total} companies total. "
          f"Checking companies {offset}-{offset + len(batch) - 1} this run "
          f"(next run starts at {state['yc_offset']}).")

    return batch


def load_discovery_state() -> dict:
    if DISCOVERY_STATE_PATH.exists():
        with DISCOVERY_STATE_PATH.open("r", encoding="utf-8") as file:
            return json.load(file)
    return {}


def save_discovery_state(state: dict) -> None:
    with DISCOVERY_STATE_PATH.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)


def load_existing_companies() -> list[dict]:
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_companies(companies: list[dict]) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as file:
        json.dump(companies, file, indent=2, ensure_ascii=False)


def _write_list(path: Path, names: list[str]) -> None:
    """Directly overwrites `path` with exactly `names`, one per line."""
    with path.open("w", encoding="utf-8") as file:
        for name in names:
            file.write(f"{name}\n")


def _merge_into_file(path: Path, new_names: list[str]) -> tuple[list[str], int]:
    """
    Shared merge logic: reads whatever's already in `path`, adds any
    genuinely new names, de-duplicates (case-insensitive), and writes
    the combined result back. Returns (merged_list, count_of_new_names)
    so callers can print an accurate "added N" message.
    """

    existing: list[str] = []
    if path.exists():
        with path.open("r", encoding="utf-8") as file:
            existing = [line.strip() for line in file if line.strip()]

    seen = {name.lower() for name in existing}
    merged = list(existing)
    added_count = 0

    for name in new_names:
        if name.lower() not in seen:
            seen.add(name.lower())
            merged.append(name)
            added_count += 1

    with path.open("w", encoding="utf-8") as file:
        for name in merged:
            file.write(f"{name}\n")

    return merged, added_count


def save_unmatched(names: list[str]) -> None:
    """
    MERGES new unmatched names into unmatched_companies.txt rather
    than overwriting it. Without this, running discover_companies.py
    again would REPLACE the file with only this run's leftovers —
    silently dropping any company from a previous run that wasn't
    re-checked this time (e.g. an earlier YC batch, since each run
    checks a different 300-company slice of Y Combinator's directory).

    These aren't necessarily dead ends — a real chunk of them likely
    use Workday (which can't be slug-guessed the same way, since it
    needs a host/tenant/site combo that varies per company) or some
    other ATS entirely, or have no public board at all.

    This file is meant as Andrew's starting reference list for manual
    Workday research, not a finished result — every name on it still
    needs a human to actually check.
    """

    merged, added_count = _merge_into_file(UNMATCHED_LOG_PATH, names)
    print(f"Merged unmatched list: {added_count} newly-unmatched this run, "
          f"{len(merged)} total in {UNMATCHED_LOG_PATH}")


def main() -> None:
    existing = load_existing_companies()
    existing_names = {c["name"].lower() for c in existing}

    print("Fetching S&P 500 company list...")
    sp500_names = fetch_sp500_names()
    print(f"Got {len(sp500_names)} S&P 500 companies")

    print("Fetching Y Combinator company list (next batch)...")
    yc_names = fetch_yc_names_batch()
    print(f"Checking {len(yc_names)} YC companies this run")

    candidate_names = sp500_names + yc_names
    new_names = [
        name for name in candidate_names
        if name.lower() not in existing_names
    ]

    print(f"\nChecking {len(new_names)} new candidate companies "
          f"against Ashby + Greenhouse...\n")

    newly_found = []
    unmatched = []

    for name in new_names:
        result = discover_company(name)
        if result:
            print(f"FOUND: {name} -> {result['ats']} "
                  f"({result.get('board_name') or result.get('board_token')})")
            newly_found.append(result)
            existing_names.add(name.lower())
        else:
            unmatched.append(name)

    if newly_found:
        updated = existing + newly_found
        save_companies(updated)
        print(f"\nAdded {len(newly_found)} new companies to {CONFIG_PATH}")

        # If anything newly-found was previously sitting in the
        # unmatched file (e.g. found this time via a different
        # method, or company.json was hand-edited since), remove it
        # from unmatched now that it's actually covered.
        if UNMATCHED_LOG_PATH.exists():
            with UNMATCHED_LOG_PATH.open("r", encoding="utf-8") as file:
                still_listed = [line.strip() for line in file if line.strip()]
            newly_found_names = {c["name"].lower() for c in newly_found}
            cleaned = [n for n in still_listed if n.lower() not in newly_found_names]
            if len(cleaned) != len(still_listed):
                with UNMATCHED_LOG_PATH.open("w", encoding="utf-8") as file:
                    for name in cleaned:
                        file.write(f"{name}\n")
                print(f"Removed {len(still_listed) - len(cleaned)} newly-found "
                      f"companies from {UNMATCHED_LOG_PATH}")
    else:
        print("\nNo new companies found this run.")

    if unmatched:
        save_unmatched(unmatched)


def run_workday_pass(limit: int = 30) -> None:
    """
    Separate, opt-in pass: tries Workday tenant/shard/site guessing
    against companies already sitting in unmatched_companies.txt (i.e.
    ones that didn't resolve on Ashby or Greenhouse).

    `limit` caps how many companies to try per run, since each one is
    up to 5 shards x 8 site names x however many tenant slug guesses —
    meaningfully slower than the Ashby/Greenhouse pass. Run this
    periodically (e.g. its own weekly/monthly cron entry) rather than
    every time, and raise `limit` gradually once you've confirmed
    it's finding real matches worth the runtime.

    A miss here just stays in unmatched_companies.txt — it's NOT
    tracked as "confirmed not Workday" anywhere. This guessing method
    is bounded by design (a fixed set of shard/site-name patterns),
    so a miss only means none of the patterns tried happened to match
    — real companies get missed this way regularly (confirmed: Boeing
    was missed by this and the Selenium approach both, found only via
    manual search). Recording misses as reliable negative evidence
    would be actively misleading.
    """

    if not UNMATCHED_LOG_PATH.exists():
        print(f"{UNMATCHED_LOG_PATH} doesn't exist yet — run the main "
              f"discovery pass first.")
        return

    with UNMATCHED_LOG_PATH.open("r", encoding="utf-8") as file:
        all_unmatched = [line.strip() for line in file if line.strip()]

    candidates = all_unmatched[:limit]

    print(f"Trying Workday guessing against {len(candidates)} companies "
          f"from {UNMATCHED_LOG_PATH} — up to "
          f"{len(WORKDAY_SHARDS_TO_TRY) * len(COMMON_WORKDAY_SITE_NAMES)} "
          f"requests per company...\n")

    existing = load_existing_companies()
    existing_names = {c["name"].lower() for c in existing}
    found = []

    for name in candidates:
        if name.lower() in existing_names:
            continue

        result = check_workday(name)
        if result:
            print(f"FOUND (Workday): {name} -> "
                  f"{result['tenant']} / {result['site']} (shard {result['host'].split('.wd')[1].split('.')[0]})")
            found.append(result)

    if found:
        updated = existing + found
        save_companies(updated)
        print(f"\nAdded {len(found)} Workday companies to {CONFIG_PATH}")

        found_names = {c["name"].lower() for c in found}
        remaining_unmatched = [n for n in all_unmatched if n.lower() not in found_names]
        _write_list(UNMATCHED_LOG_PATH, remaining_unmatched)
        print(f"Removed {len(all_unmatched) - len(remaining_unmatched)} "
              f"newly-found companies from {UNMATCHED_LOG_PATH} "
              f"({len(remaining_unmatched)} remaining)")
    else:
        print("\nNo Workday matches found this run. unmatched_companies.txt unchanged.")


if __name__ == "__main__":
    main()
    # To try Selenium-based discovery against your unmatched list
    # (visits each company's own site, follows their real "Careers"
    # link, reads whatever ATS URL it lands on — the primary
    # recommended path for Workday specifically), run separately,
    # from inside src/:
    #   python3 -c "from discover_companies import run_selenium_pass; run_selenium_pass(limit=50)"
    # Requires: pip install selenium webdriver-manager
    #
    # If Google Custom Search is set up (GOOGLE_SEARCH_API_KEY and
    # GOOGLE_SEARCH_CX in .env), that's a lighter-weight alternative:
    #   python3 -c "from discover_companies import run_search_pass; run_search_pass(limit=90)"
    #
    # The blind-guessing check_workday()/run_workday_pass() is still
    # available as a last-resort fallback if neither of the above
    # works for you:
    #   python3 -c "from discover_companies import run_workday_pass; run_workday_pass(limit=30)"