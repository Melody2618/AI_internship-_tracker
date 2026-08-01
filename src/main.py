import json
from pathlib import Path

import requests

import ashby
import greenhouse


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


def save_jobs(jobs: list[dict], output_path: Path) -> None:
    """Save the combined job list."""

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
        company_name = company.get("name", "Unknown company")
        ats = company.get("ats", "").lower()

        print(f"\nFetching {company_name} from {ats}...")

        try:
            if ats == "greenhouse":
                jobs = scrape_greenhouse_company(company)

            elif ats == "ashby":
                jobs = scrape_ashby_company(company)

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
            print(f"Failed to fetch {company_name}: {error}")
            continue

        combined_jobs.extend(jobs)

        print(f"Found {len(jobs)} internship-like jobs")

        for job in jobs:
            print(
                f"- {job.get('title')} | "
                f"{job.get('location')}"
            )

    combined_jobs.sort(
        key=lambda job: (
            str(job.get("company", "")).lower(),
            str(job.get("title", "")).lower(),
        )
    )

    save_jobs(
        jobs=combined_jobs,
        output_path=OUTPUT_PATH,
    )

    print(
        f"\nSaved {len(combined_jobs)} combined jobs "
        f"to {OUTPUT_PATH}"
    )


if __name__ == "__main__":
    main()