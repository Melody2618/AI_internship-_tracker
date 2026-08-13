import json
import re
from pathlib import Path

import requests

import ashby
import greenhouse
import workday

from gemini_extract import classify_jobs_batch


CONFIG_PATH = Path("config/companies.json")
OUTPUT_PATH = Path("data/jobs.json")

# Job titles containing any of these words are almost never student
# internships. Filtering them out BEFORE sending anything to Gemini
# keeps API usage manageable as more companies are added — most of a
# large company's postings (e.g. senior/staff/director roles) never
# need AI judgment at all.
SENIOR_TITLE_MARKERS = (
    "senior",
    "sr.",
    "staff",
    "principal",
    "director",
    "vp",
    "vice president",
    "head of",
    "chief",
    "manager",
    "lead ",
    "president",
    "executive",
)


def is_obviously_not_internship(job: dict) -> bool:
    """
    Loose pre-filter, NOT a replacement for classify_and_tag_jobs().
    Only rules out titles that are unambiguously senior/full-time —
    anything even slightly ambiguous is left for Gemini to judge, so
    we don't risk dropping a real internship posting to save API calls.
    """

    title = str(job.get("title") or "").lower()
    return any(marker in title for marker in SENIOR_TITLE_MARKERS)


def load_companies(config_path: Path) -> list[dict]:
    """Load enabled companies from the configuration file."""

    with config_path.open("r", encoding="utf-8") as file:
        companies = json.load(file)

    return [
        company
        for company in companies
        if company.get("enabled", True)
    ]


def scrape_greenhouse_company(company: dict) -> list[dict]:
    """Fetch and normalize ALL postings from Greenhouse.

    Note: this no longer pre-filters by the regex is_internship() check —
    that filtering now happens centrally in classify_and_tag_jobs(), so
    regex-rejected postings can get a second look from Gemini instead of
    being discarded here.
    """

    company_name = company["name"]
    board_token = company["board_token"]

    all_jobs = greenhouse.fetch_jobs(board_token)

    normalized = [
        greenhouse.normalize_job(
            job=job,
            company_name=company_name,
            board_token=board_token,
        )
        for job in all_jobs
    ]

    for job, raw_job in zip(normalized, all_jobs):
        job["regex_internship"] = greenhouse.is_internship(raw_job)

    return normalized


def scrape_ashby_company(company: dict) -> list[dict]:
    """Fetch and normalize ALL postings from Ashby (see note above)."""

    company_name = company["name"]
    board_name = company["board_name"]

    all_jobs = ashby.fetch_jobs(board_name)

    normalized = [
        ashby.normalize_job(
            job=job,
            company_name=company_name,
            board_name=board_name,
        )
        for job in all_jobs
    ]

    for job, raw_job in zip(normalized, all_jobs):
        job["regex_internship"] = ashby.is_internship(raw_job)

    return normalized


def scrape_workday_company(company: dict) -> list[dict]:
    """Fetch and normalize ALL student-adjacent roles from Workday (see note above)."""

    all_jobs = workday.fetch_all_jobs(company)

    normalized = [
        workday.normalize_job(
            job=job,
            company=company,
        )
        for job in all_jobs
    ]

    for job, raw_job in zip(normalized, all_jobs):
        job["regex_internship"] = workday.is_student_role(raw_job)

    return normalized


def classify_and_tag_jobs(jobs: list[dict]) -> list[dict]:
    """
    Central internship filter + major-tagging pass, batched to stay well
    within the free tier's rate limits (25 jobs per API call instead of
    one call per job).

    Before anything reaches Gemini, obvious non-student roles (senior,
    staff, director, VP, principal, lead, etc.) are filtered out for
    free — this matters a lot as more companies are added, since most
    of a large company's postings are regular full-time roles that
    don't need AI judgment at all. Anything not obviously senior still
    goes to Gemini, so oddly-worded internship titles aren't lost.

    A job is kept if EITHER the regex filter flagged it OR Gemini's
    batch classification says it's an internship.
    """

    likely_senior_pattern = re.compile(
        r"\b("
        r"senior|sr\.?|staff|principal|director|vp|vice president|"
        r"head of|chief|manager|lead\b|architect|distinguished"
        r")\b",
        re.IGNORECASE,
    )

    needs_gemini: list[dict] = []
    auto_kept: list[dict] = []
    auto_dropped_count = 0

    for job in jobs:
        title = job.get("title") or ""

        if job.get("regex_internship"):
            # Already confirmed an internship by regex — skip the
            # senior-title filter and send straight to Gemini for
            # major tagging only.
            needs_gemini.append(job)
        elif likely_senior_pattern.search(title):
            # Clearly a senior/full-time role — no point spending an
            # API call confirming what the title already makes obvious.
            auto_dropped_count += 1
        else:
            # Ambiguous — let Gemini make the call.
            needs_gemini.append(job)

    print(
        f"Pre-filter: skipped {auto_dropped_count} obviously senior/"
        f"full-time postings before sending the rest to Gemini"
    )

    results = classify_jobs_batch(needs_gemini)

    kept_jobs: list[dict] = []

    for job, result in zip(needs_gemini, results):
        if job.get("regex_internship") or result["is_internship"]:
            job["majors"] = result["majors"] or ["General/Other"]
            kept_jobs.append(job)

        job.pop("regex_internship", None)

    return kept_jobs


def deduplicate_jobs(jobs: list[dict]) -> list[dict]:
    """Remove duplicate postings using normalized job IDs."""

    unique_jobs: dict[str, dict] = {}

    for job in jobs:
        job_id = str(job.get("id", "")).strip()

        if not job_id:
            continue

        unique_jobs[job_id] = job

    return list(unique_jobs.values())


def is_us_job(job: dict) -> bool:
    """Return True when a normalized job appears to be US-based."""

    location = str(job.get("location", "")).strip().lower()

    if not location:
        return False

    direct_us_markers = (
        "united states",
        "usa",
        "u.s.",
        "us-",
        "remote-friendly, united states",
    )

    if any(marker in location for marker in direct_us_markers):
        return True

    state_markers = (
        ", al",
        ", ak",
        ", az",
        ", ar",
        ", ca",
        ", co",
        ", ct",
        ", de",
        ", fl",
        ", ga",
        ", hi",
        ", id",
        ", il",
        ", in",
        ", ia",
        ", ks",
        ", ky",
        ", la",
        ", me",
        ", md",
        ", ma",
        ", mi",
        ", mn",
        ", ms",
        ", mo",
        ", mt",
        ", ne",
        ", nv",
        ", nh",
        ", nj",
        ", nm",
        ", ny",
        ", nc",
        ", nd",
        ", oh",
        ", ok",
        ", or",
        ", pa",
        ", ri",
        ", sc",
        ", sd",
        ", tn",
        ", tx",
        ", ut",
        ", vt",
        ", va",
        ", wa",
        ", wv",
        ", wi",
        ", wy",
        ", dc",
    )

    return any(marker in location for marker in state_markers)


def save_jobs(jobs: list[dict], output_path: Path) -> None:
    """Save the combined job list to JSON."""

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(
            jobs,
            file,
            indent=2,
            ensure_ascii=False,
        )


def main() -> None:
    companies = load_companies(CONFIG_PATH)
    combined_jobs: list[dict] = []

    for company in companies:
        company_name = company.get(
            "name",
            "Unknown company",
        )

        ats = str(
            company.get("ats", "")
        ).strip().lower()

        print(f"\nFetching {company_name} from {ats}...")

        try:
            if ats == "greenhouse":
                jobs = scrape_greenhouse_company(company)

            elif ats == "ashby":
                jobs = scrape_ashby_company(company)

            elif ats == "workday":
                jobs = scrape_workday_company(company)

            else:
                print(f"Unsupported ATS: {ats}")
                continue

        except KeyError as error:
            print(
                f"Missing configuration field for "
                f"{company_name}: {error}"
            )
            continue

        except requests.RequestException as error:
            print(
                f"Failed to fetch {company_name}: "
                f"{error}"
            )
            continue

        combined_jobs.extend(jobs)

        print(
            f"Fetched {len(jobs)} total postings "
            "(internship filtering happens after all companies are fetched)"
        )

    combined_jobs = deduplicate_jobs(combined_jobs)

    pre_filtered_jobs = [
        job for job in combined_jobs
        if not is_obviously_not_internship(job)
    ]
    skipped_count = len(combined_jobs) - len(pre_filtered_jobs)

    print(
        f"\nPre-filter dropped {skipped_count} obviously senior/full-time "
        f"postings before sending anything to Gemini"
    )

    print(
        f"Running Gemini classification + major tagging on "
        f"{len(pre_filtered_jobs)} remaining postings..."
    )
    combined_jobs = classify_and_tag_jobs(pre_filtered_jobs)
    print(f"Kept {len(combined_jobs)} postings after classification")

    total_before_us_filter = len(combined_jobs)

    combined_jobs = [
        job
        for job in combined_jobs
        if is_us_job(job)
    ]

    combined_jobs.sort(
        key=lambda job: (
            str(job.get("company", "")).lower(),
            str(job.get("title", "")).lower(),
            str(job.get("location", "")).lower(),
        )
    )

    save_jobs(
        jobs=combined_jobs,
        output_path=OUTPUT_PATH,
    )

    print(
        f"\nKept {len(combined_jobs)} US-based jobs "
        f"out of {total_before_us_filter} unique jobs"
    )

    print(
        f"Saved {len(combined_jobs)} combined US jobs "
        f"to {OUTPUT_PATH}"
    )


if __name__ == "__main__":
    main()