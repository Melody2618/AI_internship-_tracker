import json
from pathlib import Path

import requests

import ashby
import greenhouse
import workday


CONFIG_PATH = Path("config/companies.json")
OUTPUT_PATH = Path("data/jobs.json")


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
    """Fetch and normalize internship postings from Greenhouse."""

    company_name = company["name"]
    board_token = company["board_token"]

    all_jobs = greenhouse.fetch_jobs(board_token)

    return [
        greenhouse.normalize_job(
            job=job,
            company_name=company_name,
            board_token=board_token,
        )
        for job in all_jobs
        if greenhouse.is_internship(job)
    ]


def scrape_ashby_company(company: dict) -> list[dict]:
    """Fetch and normalize internship postings from Ashby."""

    company_name = company["name"]
    board_name = company["board_name"]

    all_jobs = ashby.fetch_jobs(board_name)

    return [
        ashby.normalize_job(
            job=job,
            company_name=company_name,
            board_name=board_name,
        )
        for job in all_jobs
        if ashby.is_internship(job)
    ]


def scrape_workday_company(company: dict) -> list[dict]:
    """Fetch and normalize student roles from Workday."""

    all_jobs = workday.fetch_all_jobs(company)

    return [
        workday.normalize_job(
            job=job,
            company=company,
        )
        for job in all_jobs
        if workday.is_student_role(job)
    ]


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
            f"Found {len(jobs)} "
            "internship or student-like jobs before US filtering"
        )

        for job in jobs:
            print(
                f"- {job.get('title')} | "
                f"{job.get('location')}"
            )

    combined_jobs = deduplicate_jobs(combined_jobs)

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