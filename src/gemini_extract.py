# ============================================================
# TEAM NOTE (read before running):
# This uses a Gemini API key stored in a local .env file.
# Do NOT use my key for your own testing — each of us should
# create our own free API key at https://aistudio.google.com/apikey
# and store it in our OWN .env file (which is git-ignored, so
# it won't be pushed or overwritten by anyone else's).
#
# Steps to add your own key:
#   1. Go to https://aistudio.google.com/apikey and create a key
#   2. In this project folder, create a file named .env
#   3. Add this line to it: GEMINI_API_KEY=your_key_here
#   4. Save. Don't commit this file — it's already in .gitignore
#
# Why separate keys matter: rate limits are applied per Google
# Cloud project, not per key. Since each of us creates a key
# from our own personal Google account, we each land on our
# own separate project automatically — using our own keys
# keeps our quotas independent of each other.
#
# Current free-tier limits (checked in AI Studio) for
# gemini-flash-lite-latest: 15 requests/minute, 250K tokens/
# minute, 500 requests/day. We use Flash-Lite instead of
# regular Flash (5 RPM / 20 RPD) since our tasks here
# (classification + tagging) don't need heavy reasoning.
# ============================================================

from google import genai
from dotenv import load_dotenv
import os
import json
import re
import time

load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

MODEL = "gemini-flash-lite-latest"  # auto-updates to Google's current Flash-Lite model

# Retry settings for hitting the rate limit (HTTP 429 errors)
MAX_RETRIES = 3
RETRY_WAIT_SECONDS = 60  # free tier resets roughly every 60 seconds

# Flash-Lite's free tier allows 15 requests/minute, so pacing calls
# ~4.5 seconds apart (60s / 15 = 4s, +buffer) keeps us under that
# limit proactively, instead of only reacting after a 429 error.
SECONDS_BETWEEN_BATCH_CALLS = 4.5

# Engineering majors we tag postings against (Rutgers SOE list).
# Keep this list in sync with the frontend filter buttons in site/app.js.
MAJOR_TAGS = [
    "Aerospace Engineering",
    "Applied Sciences in Engineering",
    "Biomedical Engineering",
    "Chemical Engineering",
    "Civil Engineering",
    "Electrical and Computer Engineering",
    "Energy Systems Engineering",
    "Environmental Engineering",
    "Industrial Engineering",
    "Materials Science and Engineering",
    "Mechanical Engineering",
    "Packaging Engineering",
    "General/Other",
]


def _call_gemini_with_retry(prompt: str):
    """Shared retry/backoff wrapper for any Gemini text call."""

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=prompt
            )
            text = response.text.strip()
            return re.sub(r"^```json|```$", "", text, flags=re.MULTILINE).strip()

        except Exception as e:
            if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
                print(f"Rate limit hit (attempt {attempt}/{MAX_RETRIES}). "
                      f"Waiting {RETRY_WAIT_SECONDS} seconds before retrying...")
                time.sleep(RETRY_WAIT_SECONDS)
            else:
                print("Unexpected error calling Gemini:", e)
                return None

    print("Gave up after hitting the rate limit multiple times.")
    return None


def build_prompt(raw_text):
    """Original field-extraction prompt (kept for reference/future use)."""
    return f"""
Extract the following fields from this job posting and return ONLY valid JSON, no explanation, no markdown formatting:

{{
  "company": "",
  "job_title": "",
  "location": "",
  "posting_url": "",
  "application_deadline": "",
  "requirements": [],
  "posted_date": ""
}}

If a field isn't present, use null. Posting text:
---
{raw_text}
---
"""


def extract_fields(raw_text):
    """Sends one posting's text to Gemini and returns a parsed dict."""
    text = _call_gemini_with_retry(build_prompt(raw_text))
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print("Failed to parse Gemini's response as JSON:", text)
        return None


def build_classification_prompt(job: dict) -> str:
    """Builds the combined internship-check + major-tagging prompt."""

    title = job.get("title") or ""
    department = job.get("department") or ""
    team = job.get("team") or ""

    majors_list = "\n".join(f"- {m}" for m in MAJOR_TAGS)

    return f"""
You are reviewing a job posting for a student engineering internship tracker.

Job title: {title}
Department: {department}
Team: {team}

Answer two questions and return ONLY valid JSON, no explanation, no markdown:

{{
  "is_internship": true or false,
  "majors": []
}}

is_internship: true if this is a student internship, co-op, or fellowship role
(not a full-time or senior position).

majors: a list of the engineering majors this posting is most relevant to,
chosen ONLY from this list:
{majors_list}

If it doesn't clearly match a specific major, use ["General/Other"].
A posting can match more than one major if genuinely relevant to both.
"""


def build_batch_classification_prompt(jobs: list[dict]) -> str:
    """Builds one prompt that classifies + tags a whole batch of jobs at once."""

    majors_list = "\n".join(f"- {m}" for m in MAJOR_TAGS)

    job_lines = []
    for i, job in enumerate(jobs):
        title = job.get("title") or ""
        department = job.get("department") or ""
        team = job.get("team") or ""
        job_lines.append(
            f'{i}: title="{title}", department="{department}", team="{team}"'
        )
    jobs_block = "\n".join(job_lines)

    return f"""
You are reviewing a batch of job postings for a student engineering internship tracker.

For EACH job below (identified by its index number), decide:
1. is_internship: true if it's a student internship, co-op, or fellowship
   (not a full-time or senior role)
2. majors: which engineering majors it's relevant to, chosen ONLY from:
{majors_list}
   Use ["General/Other"] if nothing else fits clearly. A job can match more than one.

Jobs:
{jobs_block}

Return ONLY a valid JSON array, no explanation, no markdown formatting, one object
per job, in the SAME ORDER as the input, each shaped like:
{{"index": 0, "is_internship": true, "majors": ["Mechanical Engineering"]}}
"""


def classify_jobs_batch(jobs: list[dict], batch_size: int = 40) -> list[dict]:
    """
    Classifies + tags a list of jobs in batches (one API call per batch,
    not per job) to stay well within the free tier's RPM/RPD limits.

    Two things keep this from hitting the rate limit as often as before:
      - A bigger batch size (40 instead of 25) means fewer total calls
        for the same number of postings.
      - A small proactive pause between calls (SECONDS_BETWEEN_BATCH_CALLS)
        paces requests under the 15/minute free-tier limit BEFORE hitting
        it, rather than only reacting with a 60-second wait after a 429
        error. This makes total runtime more predictable — steady and
        a bit slower throughout, instead of fast-then-stalled-then-fast.

    Returns a list the same length/order as `jobs`, each entry shaped like
    {"is_internship": bool, "majors": [str, ...]}. Any job that fails to
    parse gets a safe default (not an internship, untagged) rather than
    crashing the whole batch.
    """

    results: list[dict] = [None] * len(jobs)
    default = {"is_internship": False, "majors": []}

    total_batches = (len(jobs) + batch_size - 1) // batch_size

    for batch_num, start in enumerate(range(0, len(jobs), batch_size)):
        if batch_num > 0:
            time.sleep(SECONDS_BETWEEN_BATCH_CALLS)

        batch = jobs[start:start + batch_size]
        prompt = build_batch_classification_prompt(batch)
        text = _call_gemini_with_retry(prompt)

        if text is None:
            for i in range(len(batch)):
                results[start + i] = default
            continue

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            print("Failed to parse Gemini's batch response:", text[:200])
            for i in range(len(batch)):
                results[start + i] = default
            continue

        # Fill in whatever the model returned, keyed by its own index
        batch_results = {default_i: default for default_i in range(len(batch))}
        for entry in parsed:
            try:
                idx = int(entry.get("index"))
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(batch):
                majors = entry.get("majors", [])
                if not isinstance(majors, list):
                    majors = []
                majors = [m for m in majors if m in MAJOR_TAGS]
                batch_results[idx] = {
                    "is_internship": bool(entry.get("is_internship", False)),
                    "majors": majors,
                }

        for i in range(len(batch)):
            results[start + i] = batch_results[i]

        print(
            f"Classified batch {batch_num + 1}/{total_batches} "
            f"({len(batch)} postings, 1 API call)"
        )

    return results


def classify_job(job: dict) -> dict:
    """
    Runs the combined internship-check + major-tagging call for one job.
    Returns {"is_internship": bool, "majors": [str, ...]}.
    Falls back to a safe default (treated as non-internship, untagged)
    if Gemini fails or returns something unparseable, so a single bad
    call never crashes the whole scrape run.
    """

    prompt = build_classification_prompt(job)
    text = _call_gemini_with_retry(prompt)

    default = {"is_internship": False, "majors": []}

    if text is None:
        return default

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        print("Failed to parse Gemini's classification response:", text)
        return default

    is_internship = bool(result.get("is_internship", False))
    majors = result.get("majors", [])

    if not isinstance(majors, list):
        majors = []

    # Keep only majors we actually recognize, in case Gemini invents one
    majors = [m for m in majors if m in MAJOR_TAGS]

    return {"is_internship": is_internship, "majors": majors}