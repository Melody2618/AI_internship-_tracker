import json
import re
from pathlib import Path
from urllib.parse import urljoin

import requests


STUDENT_ROLE_PATTERNS = (
    r"\bintern\b",
    r"\binternship\b",
    r"\bstudent\b",
    r"\bstudent worker\b",
    r"\bworking student\b",
    r"\bwerkstudent\b",
    r"\bgraduate\b",
    r"\bco[- ]?op\b",
    r"\bapprentice\b",
    r"\bausbildung\b",
    r"\bduales studium\b",
    r"\bstudiumplus\b",
)


def load_companies(config_path: Path) -> list[dict]:
    """Load enabled Workday companies from the configuration file."""

    with config_path.open("r", encoding="utf-8") as file:
        companies = json.load(file)

    return [
        company
        for company in companies
        if company.get("enabled", True)
        and company.get("ats") == "workday"
    ]


def build_jobs_endpoint(company: dict) -> str:
    """Build the public Workday jobs API endpoint."""

    host = company["host"].rstrip("/")
    tenant = company["tenant"]
    site = company["site"]

    return f"{host}/wday/cxs/{tenant}/{site}/jobs"


def fetch_job_page(
    company: dict,
    offset: int = 0,
    limit: int = 20,
) -> dict:
    """Fetch one page of jobs from a Workday board."""

    host = company["host"].rstrip("/")
    locale = company.get("locale", "en-US")
    site = company["site"]

    payload = {
        "appliedFacets": {},
        "limit": limit,
        "offset": offset,
        "searchText": "",
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": host,
        "Referer": f"{host}/{locale}/{site}",
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/127.0 Safari/537.36"
        ),
    }

    response = requests.post(
        build_jobs_endpoint(company),
        json=payload,
        headers=headers,
        timeout=30,
    )

    response.raise_for_status()
    return response.json()


def fetch_all_jobs(company: dict) -> list[dict]:
    """Fetch every available job from a Workday board."""

    all_jobs: list[dict] = []
    offset = 0
    limit = 20
    total = None

    while total is None or offset < total:
        data = fetch_job_page(
            company=company,
            offset=offset,
            limit=limit,
        )

        page_jobs = data.get("jobPostings", [])

        if total is None:
            total = data.get("total", len(page_jobs))

        all_jobs.extend(page_jobs)

        print(
            f"Fetched {len(all_jobs)} of "
            f"{total} jobs for {company['name']}"
        )

        if not page_jobs:
            break

        offset += len(page_jobs)

    return all_jobs


def is_student_role(job: dict) -> bool:
    """Check whether a Workday posting is student or early-career related."""

    title = str(job.get("title", "")).lower()

    return any(
        re.search(pattern, title)
        for pattern in STUDENT_ROLE_PATTERNS
    )


def get_job_id(job: dict) -> str:
    """Extract a stable Workday job ID from its external path."""

    external_path = str(job.get("externalPath", "")).rstrip("/")

    if not external_path:
        return "unknown"

    final_segment = external_path.split("/")[-1]

    if "_" in final_segment:
        return final_segment.rsplit("_", 1)[-1]

    return final_segment


def normalize_job(
    job: dict,
    company: dict,
) -> dict:
    """Convert a Workday posting into the common tracker format."""

    host = company["host"].rstrip("/")
    locale = company.get("locale", "en-US")
    site = company["site"]

    external_path = str(job.get("externalPath", ""))

    apply_url = urljoin(
        f"{host}/{locale}/{site}/",
        external_path.lstrip("/"),
    )

    job_id = get_job_id(job)

    return {
        "id": f"workday-{company['tenant']}-{job_id}",
        "company": company["name"],
        "title": job.get("title"),
        "location": job.get("locationsText"),
        "ats": "workday",
        "posted_at": job.get("postedOn"),
        "employment_type": None,
        "apply_url": apply_url,
        "source_url": apply_url,
    }


def save_jobs(
    jobs: list[dict],
    output_path: Path,
) -> None:
    """Save normalized Workday jobs to JSON."""

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
    config_path = Path("config/companies.json")
    output_path = Path("data/workday_jobs.json")

    companies = load_companies(config_path)
    all_student_jobs: list[dict] = []

    for company in companies:
        print(f"\nFetching {company['name']}...")

        try:
            jobs = fetch_all_jobs(company)
        except requests.RequestException as error:
            print(
                f"Failed to fetch {company['name']}: "
                f"{error}"
            )
            continue

        student_jobs = [
            normalize_job(
                job=job,
                company=company,
            )
            for job in jobs
            if is_student_role(job)
        ]

        all_student_jobs.extend(student_jobs)

        print(f"Found {len(jobs)} total jobs")
        print(
            f"Found {len(student_jobs)} "
            "student or internship-like jobs"
        )

        for job in student_jobs:
            print(
                f"- {job['title']} | "
                f"{job['location']}"
            )

    all_student_jobs.sort(
        key=lambda job: (
            str(job.get("company", "")).lower(),
            str(job.get("title", "")).lower(),
        )
    )

    save_jobs(
        jobs=all_student_jobs,
        output_path=output_path,
    )

    print(
        f"\nSaved {len(all_student_jobs)} Workday jobs "
        f"to {output_path}"
    )


if __name__ == "__main__":
    main()