import json
import re
from pathlib import Path

import requests

import ashby
import greenhouse
import workday

from gemini_extract import classify_jobs_batch, keyword_fast_path_classify


CONFIG_PATH = Path("config/companies.json")
CLASSIFICATION_CACHE_PATH = Path("data/classification_cache.json")
OUTPUT_PATH = Path("data/jobs.json")
WORKLOG_PATH = Path("data/worklog.md")


def log_to_worklog(title: str, details: str) -> None:
    """
    Appends a timestamped entry to data/worklog.md — a running,
    append-only record for the team to check after each run (manual
    or cron). This is separate from console output, which disappears
    once a GitHub Actions run finishes; the worklog persists in the
    repo so anyone can review what happened without digging through
    Actions logs.

    Keep entries here reserved for things a human should actually
    look at (data-quality anomalies, repeated failures) — not routine
    per-run stats, or this file becomes noise no one reads.
    """

    import datetime

    WORKLOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n## {timestamp} — {title}\n\n{details}\n"

    with WORKLOG_PATH.open("a", encoding="utf-8") as file:
        file.write(entry)

    print(f"Logged to {WORKLOG_PATH}: {title}")


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

# Catches roles that ARE genuinely internships/co-ops but are only open
# to graduate students (MS/PhD) — these correctly match our internship
# keywords (they often literally say "Intern" or "Co-op" in the title),
# so the internship filter alone doesn't catch them. This is a separate
# check: "is this an internship" and "is this open to undergrads" are
# two different questions.
#
# Example that slipped through before this filter existed:
#   "Applied Research Intern, ... (PhD / Graduate Co-op)" at Block —
#   contains "Intern" AND "Co-op", correctly flagged as an internship,
#   but explicitly for students "returning to your MS or PhD program."
#
# TEAM NOTE: this currently DROPS graduate-only postings entirely,
# on the assumption most SHPE/SHE members are undergrads. If the team
# wants to keep these (e.g. tagged separately) instead of hiding them,
# that's a quick change — worth deciding together rather than me
# assuming the right call here.
GRADUATE_ONLY_MARKERS = (
    "phd",
    "ph.d",
    "graduate co-op",
    "graduate intern",
    "graduate program",
    "graduate student",
    "master's student",
    "master's degree",
    "returning to your program",
    "returning to your ms",
    "returning to your phd",
    "doctoral",
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


def is_graduate_only(job: dict) -> bool:
    """
    Checks whether a posting is explicitly restricted to graduate
    (MS/PhD) students, based on its title. This runs on title text
    only — a posting whose graduate-only requirement is only
    mentioned in the full description, not the title, will be missed.
    If that turns out to matter a lot in practice, this would need to
    check description text too, not just the title.
    """

    title = str(job.get("title") or "").lower()
    return any(marker in title for marker in GRADUATE_ONLY_MARKERS)


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


def load_classification_cache() -> dict:
    """
    Loads previously-computed classification results, keyed by job id.
    Without this, every run re-sends every postings to Gemini from
    scratch — even ones classified yesterday and still posted today.
    Given a company list that keeps growing, most postings persist
    run to run, so this is the single biggest lever for cutting daily
    Gemini usage (this is what caused repeated rate-limit exhaustion
    on a 15,304-posting run).
    """

    if not CLASSIFICATION_CACHE_PATH.exists():
        return {}
    try:
        with CLASSIFICATION_CACHE_PATH.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError):
        print(f"Warning: {CLASSIFICATION_CACHE_PATH} was unreadable, "
              f"starting with an empty cache")
        return {}


def save_classification_cache(cache: dict) -> None:
    CLASSIFICATION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CLASSIFICATION_CACHE_PATH.open("w", encoding="utf-8") as file:
        json.dump(cache, file, indent=2, ensure_ascii=False)


def classify_and_tag_jobs(jobs: list[dict]) -> list[dict]:
    """
    Central internship filter + major-tagging pass, batched to stay well
    within the free tier's rate limits (40 jobs per API call instead of
    one call per job).

    Before anything reaches Gemini, two free title-based checks run:
      1. Obvious non-student roles (senior, staff, director, VP,
         principal, lead, etc.) are dropped.
      2. Postings explicitly restricted to graduate (MS/PhD) students
         are dropped too — these often DO look like real internships
         (title literally says "Intern" or "Co-op"), so this has to be
         a separate check from "is this an internship", not folded
         into it. Runs BEFORE the Gemini call, not after, so we're not
         spending an API call classifying something we're about to
         throw away anyway.

    Anything not caught by either free check still goes to Gemini, so
    oddly-worded internship titles aren't lost.

    A job is kept if EITHER the regex filter flagged it OR Gemini's
    batch classification says it's an internship, AND it wasn't
    caught by the graduate-only filter.
    """

    likely_senior_pattern = re.compile(
        r"\b("
        r"senior|sr\.?|staff|principal|director|vp|vice president|"
        r"head of|chief|manager|lead\b|architect|distinguished"
        r")\b",
        re.IGNORECASE,
    )

    needs_gemini: list[dict] = []
    auto_dropped_senior_count = 0
    auto_dropped_graduate_count = 0

    for job in jobs:
        title = job.get("title") or ""

        if is_graduate_only(job):
            # Checked first, regardless of regex_internship — a
            # graduate-only posting is excluded even if it's a
            # genuine internship by every other measure.
            auto_dropped_graduate_count += 1
        elif job.get("regex_internship"):
            # Already confirmed an internship by regex — skip the
            # senior-title filter and send straight to Gemini for
            # major tagging only.
            needs_gemini.append(job)
        elif likely_senior_pattern.search(title):
            # Clearly a senior/full-time role — no point spending an
            # API call confirming what the title already makes obvious.
            auto_dropped_senior_count += 1
        else:
            # Ambiguous — let Gemini make the call.
            needs_gemini.append(job)

    print(
        f"Pre-filter: skipped {auto_dropped_senior_count} obviously "
        f"senior/full-time postings and {auto_dropped_graduate_count} "
        f"graduate-only postings before sending the rest to Gemini"
    )

    # KEYWORD FAST-PATH: check for unambiguous title patterns first —
    # free, instant, no cache lookup or Gemini call needed at all.
    # See KEYWORD_MAJOR_FAST_PATH / KEYWORD_REJECT_FAST_PATH in
    # gemini_extract.py for what counts as "unambiguous enough."
    # This scales down Gemini usage as more companies get added,
    # since standard title phrasing ("Civil Engineer Intern") shows
    # up constantly across companies.
    still_need_classification: list[dict] = []
    fast_path_results: dict[int, dict] = {}  # index into needs_gemini -> result

    for i, job in enumerate(needs_gemini):
        fast_result = keyword_fast_path_classify(job)
        if fast_result:
            fast_path_results[i] = fast_result
        else:
            still_need_classification.append(job)

    print(f"Keyword fast-path: {len(fast_path_results)} postings matched "
          f"an unambiguous title pattern (skipped Gemini entirely), "
          f"{len(still_need_classification)} still need real classification")

    # Split still_need_classification into cached (skip Gemini) and
    # genuinely-new (must actually call Gemini) — keyed by job id,
    # since the same posting reappears run after run as long as it's
    # still active. A job missing an "id" always goes to Gemini (no
    # safe way to cache it).
    old_cache = load_classification_cache()
    to_classify: list[dict] = []
    cached_results: dict[int, dict] = {}  # index into still_need_classification -> result

    for i, job in enumerate(still_need_classification):
        job_id = job.get("id")
        if job_id and job_id in old_cache:
            cached_results[i] = old_cache[job_id]
        else:
            to_classify.append(job)

    print(f"Classification cache: {len(cached_results)} postings already "
          f"classified in a previous run (skipped), {len(to_classify)} "
          f"genuinely new — only these go to Gemini")

    # Rebuild the cache fresh (self-pruning — see save_classification_cache
    # docstring), starting with whatever we're carrying over from last
    # run, then saving again after EVERY batch as fresh results come
    # in. This means a rate-limit exhaustion or crash partway through
    # only loses the batch in progress, not the whole run's work —
    # this was the actual gap that caused an earlier failure to waste
    # everything, not just the batches that failed.
    running_cache: dict = {}
    for i, job in enumerate(still_need_classification):
        if i in cached_results:
            job_id = job.get("id")
            if job_id:
                running_cache[job_id] = cached_results[i]
    save_classification_cache(running_cache)

    # Tracks consecutive batch failures so we can log a worklog entry
    # once — not once per failed batch, which would spam the file if
    # dozens of remaining batches all fail the same way after the
    # daily quota is hit (this mirrors gemini_extract.py's own
    # CONSECUTIVE_FAILURE_LIMIT=2 early-exit threshold, so the two
    # stay in sync about what counts as "likely the daily cap, not
    # just bad luck").
    consecutive_failures = 0
    already_logged_failure_streak = False

    def _save_batch_to_cache(batch_jobs, batch_results, success):
        nonlocal consecutive_failures, already_logged_failure_streak

        if not success:
            consecutive_failures += 1
            if consecutive_failures >= 2 and not already_logged_failure_streak:
                already_logged_failure_streak = True
                log_to_worklog(
                    "Gemini API failures — likely daily rate limit hit",
                    f"{consecutive_failures} consecutive batch failures during "
                    f"classification. This usually means the Gemini free tier's "
                    f"daily quota (not just the per-minute one) has been "
                    f"exhausted — it resets around midnight Pacific. Any "
                    f"postings that didn't get classified this run will be "
                    f"picked up automatically on the next run, thanks to the "
                    f"classification cache. No action needed unless this "
                    f"keeps happening across multiple days in a row, which "
                    f"would suggest the daily volume has outgrown the free tier.",
                )
            # Gemini failed for this batch — do NOT cache these as
            # real results, or real internships that failed to
            # classify due to rate limiting would be permanently
            # mislabeled as rejected and never get a fair retry.
            return

        consecutive_failures = 0
        for job, result in zip(batch_jobs, batch_results):
            job_id = job.get("id")
            if job_id:
                running_cache[job_id] = result
        save_classification_cache(running_cache)

    fresh_results = classify_jobs_batch(to_classify, on_batch_complete=_save_batch_to_cache)

    # Recombine still_need_classification in its original order: cached
    # results where we had them, freshly-computed ones for the rest.
    gemini_or_cache_results: list[dict] = []
    fresh_iter = iter(fresh_results)
    for i in range(len(still_need_classification)):
        if i in cached_results:
            gemini_or_cache_results.append(cached_results[i])
        else:
            gemini_or_cache_results.append(next(fresh_iter))

    # Now recombine at the TOP level: fast-path results where we had
    # them, cache-or-Gemini results for everything else — back into
    # the original needs_gemini order.
    results: list[dict] = []
    remainder_iter = iter(gemini_or_cache_results)
    for i in range(len(needs_gemini)):
        if i in fast_path_results:
            results.append(fast_path_results[i])
        else:
            results.append(next(remainder_iter))

    kept_jobs: list[dict] = []
    dropped_not_engineering_relevant = 0

    for job, result in zip(needs_gemini, results):
        is_internship = job.get("regex_internship") or result["is_internship"]

        # Being flagged as an internship isn't enough on its own anymore —
        # it also has to be genuinely engineering-relevant (see the
        # engineering_relevant field in the classification prompt). This
        # is what catches things like DVM veterinary externships or a
        # "Retirement Sales AE" role that matched internship keywords
        # but have nothing to do with engineering.
        if is_internship and result["engineering_relevant"]:
            job["majors"] = result["majors"] or ["General/Other"]
            kept_jobs.append(job)
        elif is_internship:
            dropped_not_engineering_relevant += 1

        job.pop("regex_internship", None)

    if dropped_not_engineering_relevant:
        print(f"Dropped {dropped_not_engineering_relevant} postings that were "
              f"internships but not engineering-relevant (sales, veterinary, "
              f"non-technical roles, etc.)")

    anomaly_details = check_major_distribution_anomaly(kept_jobs)
    if anomaly_details:
        log_to_worklog(
            "Data quality warning: major distribution anomaly",
            anomaly_details,
        )
        # Warn-only: the run continues, data/jobs.json still gets
        # published normally. A human reviews the worklog entry and
        # decides whether this needs action — see the docstring above
        # for why this isn't a hard stop anymore.

    return kept_jobs


def check_major_distribution_anomaly(
    jobs: list[dict],
    dominance_threshold: float = 0.92,
    general_other_floor: float = 0.08,
) -> str | None:
    """
    Data-quality guardrail: catches SYSTEMIC classification bugs, not
    individual ambiguous postings, and not legitimate skew in real
    data. This is deliberately NOT a hard stop on one weird title —
    that would be too fragile for a run processing thousands of
    postings, where a few genuinely ambiguous ones are normal.

    WARN-ONLY, not a run-halting check: this used to raise SystemExit
    and prevent data/jobs.json from being overwritten. That's been
    intentionally relaxed — an anomaly here now gets logged to
    data/worklog.md for the team to review, but the run (and the
    site) continues normally. A human decides whether it's a real
    problem, rather than an unattended cron run stopping the site
    from updating over what might be a false alarm.

    IMPORTANT NUANCE: one major being the single largest is NOT by
    itself suspicious — given this tracker's actual sources (lots of
    software-heavy startups, semiconductor companies like Intel/
    Micron/Analog Devices), Computer Engineering or Electrical
    Engineering legitimately being the plurality is expected, not a
    bug. A single-threshold check on "share of one major" would false-
    trigger on real, correct data constantly.

    What actually distinguishes a real systemic bug (like an earlier
    real incident: near-universal "Electrical and Computer
    Engineering" tagging) from legitimate skew is TWO signals
    together:
      1. One major dominates almost everything (>= dominance_threshold)
      2. AND "General/Other" is suspiciously rare (< general_other_floor
         of all engineering-relevant postings) — since the prompt
         explicitly instructs generic software/AI/data roles with no
         real domain specificity to get General/Other, NOT a specific
         major. If General/Other is near-absent, that's a sign the
         classifier stopped applying that instruction.
    Both conditions must be true to flag — either alone is expected,
    normal variation in real data.

    Returns a details string if an anomaly was found (for logging),
    or None if everything looks normal.
    """

    major_counts: dict[str, int] = {}
    general_other_count = 0
    total_engineering_relevant = 0

    for job in jobs:
        majors = job.get("majors") or []
        if not majors:
            continue
        total_engineering_relevant += 1
        if majors == ["General/Other"]:
            general_other_count += 1
            continue
        for major in majors:
            if major == "General/Other":
                continue
            major_counts[major] = major_counts.get(major, 0) + 1

    if total_engineering_relevant == 0 or not major_counts:
        return None

    total_specific_tags = sum(major_counts.values())
    general_other_share = general_other_count / total_engineering_relevant

    for major, count in major_counts.items():
        share = count / total_specific_tags
        if share >= dominance_threshold and general_other_share < general_other_floor:
            details = (
                f"'{major}' accounts for {share:.0%} of specific major tags "
                f"({count}/{total_specific_tags}), AND General/Other is only "
                f"{general_other_share:.0%} of engineering-relevant postings "
                f"(expected to be meaningfully higher for generic software/AI/"
                f"data roles). This combination matches the pattern of a "
                f"classifier defaulting to one major instead of correctly "
                f"using General/Other for non-specific roles. Worth a human "
                f"spot-checking a sample of this run's `{major}`-tagged "
                f"postings in data/jobs.json before assuming it's fine."
            )
            print(f"\n{'=' * 60}")
            print(f"DATA QUALITY WARNING: {details}")
            print(f"{'=' * 60}\n")
            return details

    return None


def deduplicate_jobs(jobs: list[dict]) -> list[dict]:
    """
    Remove duplicate postings, two ways:
      1. By exact job ID (catches the same source returning the same
         posting twice, e.g. across re-runs).
      2. By normalized (company, title, location), since our own
         scrapers and the external feed can independently find the
         SAME real posting with different ID formats — e.g. Notion's
         "Software Engineer Intern (Summer 2027)" showing up once
         from our own Ashby scrape and once from the external feed.
         Without this second check, that shows up twice on the site.
    """

    def normalize_key(job: dict) -> str:
        company = str(job.get("company", "")).strip().lower()
        title = str(job.get("title", "")).strip().lower()
        location = str(job.get("location", "")).strip().lower()
        return f"{company}|{title}|{location}"

    unique_by_id: dict[str, dict] = {}

    for job in jobs:
        job_id = str(job.get("id", "")).strip()

        if not job_id:
            continue

        unique_by_id[job_id] = job

    unique_by_content: dict[str, dict] = {}
    for job in unique_by_id.values():
        key = normalize_key(job)
        # Keep the first one seen — our own scrapers run before the
        # external feed in main(), so this naturally prefers our own
        # (usually more complete) version when both exist.
        if key not in unique_by_content:
            unique_by_content[key] = job

    return list(unique_by_content.values())


def is_us_job(job: dict) -> bool:
    """
    Return True when a normalized job appears to be US-based.

    Uses regex word boundaries for the state-abbreviation check —
    NOT simple substring matching. Substring matching had a real,
    confirmed bug: ", ne" (Nebraska) matched ", New Zealand" and
    ", in" (Indiana) matched ", Indonesia", letting international
    postings (Caterpillar/Christchurch NZ, Xendit/Jakarta Indonesia)
    incorrectly pass as US-based. A word boundary after the state
    code means "in" only matches when followed by a non-letter
    character (end of string, comma, etc.) — not when it's the start
    of a longer word like "Indonesia".
    """

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

    state_codes = (
        "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga",
        "hi", "id", "il", "in", "ia", "ks", "ky", "la", "me", "md",
        "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj",
        "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc",
        "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy",
        "dc",
    )

    state_pattern = r",\s*(?:" + "|".join(state_codes) + r")\b"
    return bool(re.search(state_pattern, location))


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


EXTERNAL_FEED_URL = (
    "https://zshah101.github.io/Automated-List-Of-Summer-2027-and-Fall-2026-"
    "Tech-Internships/api/jobs.json"
)


def fetch_external_feed_jobs() -> list[dict]:
    """
    Pulls postings from a free, community-run internship tracker
    (zshah101's "Automated List" project — MIT licensed, updated every
    30 minutes, ~4,000 employers across 12 ATS platforms). This is a
    much bigger and faster-growing source than our own company-by-
    company scraping, since it already does the hard work of finding
    postings across companies we haven't added (or can't easily find
    via Ashby/Greenhouse slug-guessing or Workday discovery).

    Each entry is normalized into the same shape our own scrapers
    produce, so it flows through the exact same pre-filter, Gemini
    classification, and major-tagging pipeline as everything else —
    no special-casing needed downstream.

    If the feed is unreachable, this returns an empty list (with a
    printed warning) rather than crashing the whole run — the rest
    of the pipeline (our own scraped companies) should still work
    even if this external source is temporarily down.
    """

    try:
        response = requests.get(EXTERNAL_FEED_URL, timeout=15)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as error:
        print(f"Failed to fetch external feed: {error}")
        return []

    jobs = []
    for entry in data.get("jobs", []):
        # IMPORTANT: every job needs an "id" — deduplicate_jobs() in
        # main() silently DROPS any job with no id (or an empty one).
        # This was a real bug here before: without this field, every
        # external feed job was discarded before pre-filter, before
        # Gemini, before the US filter even ran — not a filtering
        # issue, just a missing field. The feed's own "id" is already
        # unique per posting (e.g. "ashby:replit:7e0dafe8-...").
        feed_id = entry.get("id", "")
        job_id = f"external-{feed_id}" if feed_id else None
        if not job_id:
            continue  # skip anything the feed itself didn't give an id for

        jobs.append({
            "id": job_id,
            "title": entry.get("title", ""),
            "company": entry.get("company", ""),
            "location": entry.get("location", ""),
            "department": entry.get("category", ""),
            "team": "",
            "posting_url": entry.get("url", ""),
            "apply_url": entry.get("url", ""),
            "ats": entry.get("source", "external_feed"),
            "posted_at": entry.get("posted_at"),
            # Regex pre-filter still runs on these too (see main()) —
            # this feed already scopes to internships/co-ops, but our
            # own senior/grad-only filters are a free extra safety net.
        })

    print(f"Fetched {len(jobs)} postings from the external feed "
          f"({data.get('count', len(jobs))} total available)")

    return jobs


JOBRIGHT_ENGINEERING_URL = (
    "https://raw.githubusercontent.com/jobright-ai/2026-Engineer-Internship/"
    "master/README.md"
)


def fetch_jobright_engineering_jobs() -> list[dict]:
    """
    Pulls postings from jobright-ai's "Engineering and Development"
    internship list — a markdown-table README covering mechanical,
    civil, biomedical, aerospace, industrial, and other NON-software
    engineering disciplines that our main external feed (zshah101's
    tracker, which is tech/software/CS/quant focused) barely touches.
    This is what actually fixes the "too few mechanical/civil/
    biomedical results" gap, rather than just adding more software
    roles.

    The table only lists postings from the last 7 days, so this
    naturally stays a rolling window rather than growing forever.

    Parses the raw README.md table directly (not a JSON API — this
    project publishes as a markdown table), row by row. If GitHub is
    unreachable or the table format changes unexpectedly, this
    returns an empty list with a printed warning rather than crashing
    the whole run.
    """

    try:
        response = requests.get(JOBRIGHT_ENGINEERING_URL, timeout=15)
        response.raise_for_status()
        text = response.text
    except requests.RequestException as error:
        print(f"Failed to fetch jobright engineering feed: {error}")
        return []

    jobs = []
    last_company = ""  # markdown uses "↳" for repeated companies in a row

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or "---" in line:
            continue

        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue

        # Skip the header row
        if cells[0].lower() in ("company", ""):
            if cells[0].lower() == "company":
                continue

        company_cell, title_cell, location, work_model = cells[0], cells[1], cells[2], cells[3]

        # Extract "Name" from markdown "**[Name](url)**" formatting
        company_match = re.search(r"\[([^\]]+)\]", company_cell)
        if company_match:
            company = company_match.group(1)
            last_company = company
        elif company_cell in ("↳", ""):
            company = last_company
        else:
            company = company_cell or last_company

        title_match = re.search(r"\[([^\]]+)\]\(([^)]+)\)", title_cell)
        if not title_match:
            continue
        title = title_match.group(1)
        url = title_match.group(2).split("?")[0]  # strip tracking params

        if not company or not title:
            continue

        job_id = f"jobright-{re.sub(r'[^a-z0-9]', '', (company + title).lower())[:80]}"

        jobs.append({
            "id": job_id,
            "title": title,
            "company": company,
            "location": location,
            "department": "",
            "team": "",
            "posting_url": url,
            "apply_url": url,
            "ats": "jobright_engineering_feed",
            "posted_at": None,
        })

    print(f"Fetched {len(jobs)} postings from the jobright engineering feed")

    return jobs


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

    print(f"\nFetching external feed...")
    external_feed_jobs = fetch_external_feed_jobs()
    for job in external_feed_jobs:
        job["_from_external_feed"] = True  # temporary tracking tag, stripped before saving
    combined_jobs.extend(external_feed_jobs)

    print(f"\nFetching jobright engineering feed (mechanical/civil/"
          f"biomedical/other non-software disciplines)...")
    jobright_jobs = fetch_jobright_engineering_jobs()
    for job in jobright_jobs:
        job["_from_external_feed"] = True  # same tracking tag, same downstream treatment
    combined_jobs.extend(jobright_jobs)

    combined_jobs = deduplicate_jobs(combined_jobs)
    external_after_dedup = sum(1 for j in combined_jobs if j.get("_from_external_feed"))
    print(f"External feed jobs remaining after dedup: {external_after_dedup} "
          f"of {len(external_feed_jobs) + len(jobright_jobs)} fetched "
          f"({len(external_feed_jobs)} tech feed + {len(jobright_jobs)} "
          f"engineering feed)")

    pre_filtered_jobs = [
        job for job in combined_jobs
        if not is_obviously_not_internship(job)
    ]
    skipped_count = len(combined_jobs) - len(pre_filtered_jobs)
    external_after_prefilter = sum(1 for j in pre_filtered_jobs if j.get("_from_external_feed"))
    print(f"External feed jobs remaining after senior/grad pre-filter: "
          f"{external_after_prefilter}")

    print(
        f"\nPre-filter dropped {skipped_count} obviously senior/full-time "
        f"postings before sending anything to Gemini"
    )

    print(
        f"Running Gemini classification + major tagging on "
        f"{len(pre_filtered_jobs)} remaining postings..."
    )
    combined_jobs = classify_and_tag_jobs(pre_filtered_jobs)
    external_after_classification = sum(1 for j in combined_jobs if j.get("_from_external_feed"))
    print(f"External feed jobs remaining after Gemini classification: "
          f"{external_after_classification}")
    print(f"Kept {len(combined_jobs)} postings after classification")

    total_before_us_filter = len(combined_jobs)

    combined_jobs = [
        job
        for job in combined_jobs
        if is_us_job(job)
    ]
    external_after_us_filter = sum(1 for j in combined_jobs if j.get("_from_external_feed"))
    print(f"External feed jobs remaining after US-location filter: "
          f"{external_after_us_filter}")

    # Strip the temporary tracking tag before saving — it's not part of
    # the real job schema the frontend expects.
    for job in combined_jobs:
        job.pop("_from_external_feed", None)

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