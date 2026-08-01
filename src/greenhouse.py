import json
import re
from pathlib import Path

import requests

INTERNSHIP_PATTERNS = (
    r"\bintern\b",
    r"\binterns\b",
    r"\binternship\b",
    r"\binternships\b",
    r"\bco[- ]?op\b",
    r"\bstudent\b",
    r"\bfellow\b",
    r"\bfellows\b",
)


def load_companies(config_path: Path) -> list[dict]:
    """Load enabled companies from the configuration file."""

    with config_path.open("r", encoding="utf-8") as file:
        companies = json.load(file)

    return [
        company
        for company in companies
        if company.get("enabled", True)
    ]


def fetch_jobs(board_token: str) -> list[dict]:
    """Fetch all public jobs from a Greenhouse board."""

    url = (
        "https://boards-api.greenhouse.io/v1/boards/"
        f"{board_token}/jobs?content=true"
    )

    response = requests.get(url, timeout=30)
    response.raise_for_status()

    data = response.json()
    return data.get("jobs", [])


def is_internship(job: dict) -> bool:
    """Check whether a job title looks like an internship-style role."""

    title = job.get("title", "").lower()

    return any(
        re.search(pattern, title)
        for pattern in INTERNSHIP_PATTERNS
    )


def normalize_job(
    job: dict,
    company_name: str,
    board_token: str,
) -> dict:
    """Convert a Greenhouse job into the tracker's standard format."""

    location = job.get("location") or {}

    return {
        "id": f"greenhouse-{board_token}-{job.get('id')}",
        "company": company_name,
        "title": job.get("title"),
        "location": location.get("name"),
        "ats": "greenhouse",
        "updated_at": job.get("updated_at"),
        "apply_url": job.get("absolute_url"),
    }


def save_jobs(jobs: list[dict], output_path: Path) -> None:
    """Save normalized jobs to a JSON file."""

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with output_path.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            jobs,
            file,
            indent=2,
            ensure_ascii=False,
        )


def main() -> None:
    config_path = Path("config/companies.json")
    output_path = Path("data/jobs.json")

    companies = load_companies(config_path)
    all_internship_jobs: list[dict] = []

    for company in companies:
        if company.get("ats") != "greenhouse":
            continue

        company_name = company["name"]
        board_token = company["board_token"]

        print(f"\nFetching {company_name}...")

        try:
            all_jobs = fetch_jobs(board_token)
        except requests.RequestException as error:
            print(f"Failed to fetch {company_name}: {error}")
            continue

        internship_jobs = [
            normalize_job(
                job=job,
                company_name=company_name,
                board_token=board_token,
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
                f"{job['location']}"
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