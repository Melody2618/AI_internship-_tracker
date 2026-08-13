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
#   - Y Combinator company list: github.com/yc-oss/api (public,
#     updated daily, mirrors YC's own directory)
#
# Run it with:
#   python3 src/discover_companies.py
#
# It will PRINT what it found and APPEND new confirmed companies
# to config/companies.json (existing entries are never touched or
# duplicated — it checks names already in the file first).
# ============================================================

import json
import re
import time
from pathlib import Path

import requests

CONFIG_PATH = Path("config/companies.json")
UNMATCHED_LOG_PATH = Path("config/unmatched_companies.txt")

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


def discover_company(name: str) -> dict | None:
    """
    Tries each slug guess against Ashby, then Greenhouse.
    Returns a companies.json-shaped entry on the first VERIFIED hit
    (slug resolves AND the board's own company name matches), or None.
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


def fetch_yc_names(limit: int = 300) -> list[str]:
    """
    Pulls company names from the free YC directory mirror.
    Limited by default since YC has thousands of companies and most
    aren't currently hiring interns — raise `limit` once this is
    working well and you want to cast a wider net.
    """

    response = requests.get(YC_COMPANIES_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    companies = response.json()
    return [c["name"] for c in companies[:limit] if c.get("name")]


def load_existing_companies() -> list[dict]:
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_companies(companies: list[dict]) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as file:
        json.dump(companies, file, indent=2, ensure_ascii=False)


def save_unmatched(names: list[str]) -> None:
    """
    Writes out every candidate company that didn't resolve on Ashby or
    Greenhouse — one name per line, easy to scan or paste elsewhere.

    These aren't necessarily dead ends — a real chunk of them likely
    use Workday (which can't be slug-guessed the same way, since it
    needs a host/tenant/site combo that varies per company) or some
    other ATS entirely, or have no public board at all.

    This file is meant as Andrew's starting reference list for manual
    Workday research, not a finished result — every name on it still
    needs a human to actually check.
    """

    with UNMATCHED_LOG_PATH.open("w", encoding="utf-8") as file:
        for name in names:
            file.write(f"{name}\n")

    print(f"Saved {len(names)} unmatched company names to {UNMATCHED_LOG_PATH} "
          f"for manual follow-up (e.g. Workday research)")


def main() -> None:
    existing = load_existing_companies()
    existing_names = {c["name"].lower() for c in existing}

    print("Fetching S&P 500 company list...")
    sp500_names = fetch_sp500_names()
    print(f"Got {len(sp500_names)} S&P 500 companies")

    print("Fetching Y Combinator company list...")
    yc_names = fetch_yc_names()
    print(f"Got {len(yc_names)} YC companies (limited sample)")

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
    else:
        print("\nNo new companies found this run.")

    if unmatched:
        save_unmatched(unmatched)


if __name__ == "__main__":
    main()