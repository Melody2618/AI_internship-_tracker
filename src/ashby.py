import json
import re
from pathlib import Path
from urllib.parse import urlparse

import requests


INTERNSHIP_PATTERNS = (
    r"\bintern\b",
    r"\binterns\b",
    r"\binternship\b",
    r"\binternships\b",
    r"\bco[- ]?op\b",
    r"\bfellow\b",
    r"\bfellows\b",
    r"\bfellowship\b",
    r"\bfellowships\b",
)


def load_companies(config_path: Path) -> list[dict]:
    """Load enabled Ashby companies from the configuration file."""

    with config_path.open("r", encoding="utf-8") as file:
        companies = json.load(file)

    return [
        company
        for company in companies
        if company.get("enabled", True)
        and company.get("ats") == "ashby"
    ]


def fetch_jobs(board_name: str) -> list[dict]:
    """Fetch all public jobs from an Ashby job board."""

    url = (
        "https://api.ashbyhq.com/posting-api/job-board/"
        f"{board_name}"
    )

    response = requests.get(url, timeout=30)
    response.raise_for_status()

    data = response.json()
    return data.get("jobs", [])


def is_internship(job: dict) -> bool:
    """Determine whether an Ashby posting is an internship."""

    employment_type = str(
        job.get("employmentType", "")
    ).strip().lower()

    if employment_type in {"intern", "internship"}:
        return True

    title = str(job.get("title", "")).lower()

    return any(
        re.search(pattern, title)
        for pattern in INTERNSHIP_PATTERNS
    )


def get_job_id(job: dict) -> str:
    """Get a stable ID from the Ashby job record or job URL."""

    if job.get("id"):
        return str(job["id"])

    job_url = str(job.get("jobUrl", ""))
    path = urlparse(job_url).path.rstrip("/")

    if path:
        return path.split("/")[-1]

    return "unknown"


def normalize_job(
    job: dict,
    company_name: str,
    board_name: str,
) -> dict:
    """Convert an Ashby job into the tracker's standard format."""

    job_id = get_job_id(job)

    return {
        "id": f"ashby-{board_name}-{job_id}",
        "company": company_name,
        "title": job.get("title"),
        "location": job.get("location"),
        "team": job.get("team"),
        "department": job.get("department"),
        "employment_type": job.get("employmentType"),
        "ats": "ashby",
        "posted_at": job.get("publishedAt"),
        "apply_url": job.get("applyUrl") or job.get("jobUrl"),
        "source_url": job.get("jobUrl"),
    }


def save_jobs(jobs: list[dict], output_path: Path) -> None:
    """Save normalized Ashby jobs to a JSON file."""

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
    # TEAM NOTE: writes to its OWN file (ashby_jobs.json) — NOT
    # data/jobs.json, which is the combined output from the real
    # pipeline (src/main.py). Safe to run directly for testing the
    # Ashby scraper alone. For the actual pipeline, use:
    #   python3 src/main.py
    config_path = Path("config/companies.json")
    output_path = Path("data/ashby_jobs.json")

    companies = load_companies(config_path)
    all_internship_jobs: list[dict] = []

    for company in companies:
        company_name = company["name"]
        board_name = company["board_name"]

        print(f"\nFetching {company_name}...")

        try:
            all_jobs = fetch_jobs(board_name)
        except requests.RequestException as error:
            print(f"Failed to fetch {company_name}: {error}")
            continue

        internship_jobs = [
            normalize_job(
                job=job,
                company_name=company_name,
                board_name=board_name,
            )
            for job in all_jobs
            if is_internship(job)
        ]

        all_internship_jobs.extend(internship_jobs)

        print(f"Found {len(all_jobs)} total jobs")
        print(
            f"Found {len(internship_jobs)} "
            "internship-like jobs"
        )

        for job in internship_jobs:
            print(
                f"- {job['title']} | "
                f"{job['location']} | "
                f"{job['employment_type']}"
            )

    all_internship_jobs.sort(
        key=lambda job: (
            str(job["company"]).lower(),
            str(job["title"]).lower(),
        )
    )

    save_jobs(
        jobs=all_internship_jobs,
        output_path=output_path,
    )

    print(
        f"\nSaved {len(all_internship_jobs)} "
        f"internship-like jobs to {output_path}"
    )


if __name__ == "__main__":
    main()