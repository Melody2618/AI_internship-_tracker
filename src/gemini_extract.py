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
    "Electrical Engineering",
    "Computer Engineering",
    "Energy Systems Engineering",
    "Environmental Engineering",
    "Industrial Engineering",
    "Materials Science and Engineering",
    "Mechanical Engineering",
    "Packaging Engineering",
    "General/Other",
]

# ============================================================
# KEYWORD FAST-PATH (free, no Gemini call needed)
#
# TEAM NOTE: this is intentionally CONSERVATIVE — only exact,
# unambiguous title phrases that mean one thing and one thing only.
# It exists to reduce Gemini usage as the company list grows (many
# companies use standard title phrasing like "Civil Engineer Intern"
# verbatim), NOT to replace Gemini's judgment on ambiguous titles.
#
# Do NOT add loose/partial keywords here (e.g. bare "engineer" or
# "AI") — that's exactly what caused the earlier bug where almost
# everything got tagged "Electrical and Computer Engineering" by
# default. Every pattern below should be specific enough that a
# human would agree it ALWAYS means that major, no exceptions.
#
# A title matching one of these patterns is assumed engineering-
# relevant AND assigned the mapped major, skipping Gemini entirely.
# Everything else (the majority of ambiguous real-world titles)
# still goes to Gemini as before.
# ============================================================
KEYWORD_MAJOR_FAST_PATH = {
    r"\bcivil engineer(ing)?\b": ["Civil Engineering"],
    r"\bmechanical engineer(ing)?\b": ["Mechanical Engineering"],
    r"\bchemical engineer(ing)?\b": ["Chemical Engineering"],
    r"\baerospace engineer(ing)?\b": ["Aerospace Engineering"],
    r"\bbiomedical engineer(ing)?\b": ["Biomedical Engineering"],
    r"\benvironmental engineer(ing)?\b": ["Environmental Engineering"],
    r"\bindustrial engineer(ing)?\b": ["Industrial Engineering"],
    r"\bpackaging engineer(ing)?\b": ["Packaging Engineering"],
    r"\bmaterials (science|engineering)\b": ["Materials Science and Engineering"],
    r"\bstructural engineer(ing)?\b": ["Civil Engineering"],
    r"\bgeotechnical\b": ["Civil Engineering"],
    r"\belectrical engineer(ing)?\b(?!.*\bcomputer\b)": ["Electrical Engineering"],
    # Added based on real titles seen across today's runs — same
    # conservative bar: only patterns that mean ONE thing, always.
    r"\bfirmware\b": ["Computer Engineering"],
    r"\bembedded (systems?|software)\b": ["Computer Engineering"],
    r"\bhardware engineer(ing)?\b": ["Electrical Engineering", "Computer Engineering"],
    r"\bcircuit design\b": ["Electrical Engineering"],
    r"\banalog design\b": ["Electrical Engineering"],
    r"\brf engineer(ing)?\b": ["Electrical Engineering"],
    r"\bsemiconductor (engineer|device|process)\b": ["Electrical Engineering", "Materials Science and Engineering"],
    r"\bpower systems? engineer(ing)?\b": ["Electrical Engineering", "Energy Systems Engineering"],
    r"\btransportation engineer(ing)?\b": ["Civil Engineering"],
    r"\bwater resources? engineer(ing)?\b": ["Civil Engineering", "Environmental Engineering"],
    r"\bconstruction management\b": ["Civil Engineering"],
    r"\bhvac\b": ["Mechanical Engineering"],
    r"\bthermal engineer(ing)?\b": ["Mechanical Engineering"],
    r"\bmanufacturing engineer(ing)?\b": ["Industrial Engineering", "Mechanical Engineering"],
    r"\bpowertrain\b": ["Mechanical Engineering", "Electrical Engineering"],
    r"\bnuclear engineer(ing)?\b": ["Energy Systems Engineering", "Mechanical Engineering"],
}

# Same idea, but for auto-REJECTION — titles that are unambiguously
# NOT engineering, so no Gemini call is needed to confirm that either.
# Just as conservative: only exact phrases that are always non-
# engineering, never a real engineering role in disguise.
KEYWORD_REJECT_FAST_PATH = (
    r"\bdvm student\b",
    r"\bveterinary\b",
    r"\bveterinarian\b",
    r"\bretail sales\b",
    r"\bsales associate\b",
    r"\baccount executive\b",
    r"\bmarketing intern\b",
    r"\bhuman resources intern\b",
    r"\bhr intern\b",
    r"\brecruiting intern\b",
    r"\btalent acquisition\b",
    r"\bretirement sales\b",
    r"\bparalegal\b",
    r"\blegal intern\b",
    r"\bcontent writer\b",
    r"\bsocial media intern\b",
)


def keyword_fast_path_classify(job: dict) -> dict | None:
    """
    Tries the free keyword fast-path before falling back to Gemini.
    Returns a classification dict ({"is_internship", ...}) if a
    strong, unambiguous match is found, or None if Gemini judgment
    is still needed (the common case — most titles are ambiguous
    enough that this returns None).

    is_internship is deliberately NOT decided here — that still needs
    either the existing regex_internship check or Gemini, since a
    title like "Civil Engineer" alone doesn't tell you if it's an
    internship or a senior full-time role. This function only
    shortcuts the MAJOR/relevance decision, not the internship check.
    """

    title = str(job.get("title") or "").lower()

    for pattern in KEYWORD_REJECT_FAST_PATH:
        if re.search(pattern, title):
            return {"is_internship": True, "engineering_relevant": False, "majors": []}

    for pattern, majors in KEYWORD_MAJOR_FAST_PATH.items():
        if re.search(pattern, title):
            return {"is_internship": True, "engineering_relevant": True, "majors": majors}

    return None


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
You are reviewing a batch of job postings for a student ENGINEERING internship
tracker used by an engineering student organization. Being strict here matters
more than being inclusive — students are relying on this list to only contain
real engineering-relevant opportunities.

For EACH job below (identified by its index number), decide THREE things:

1. is_internship: true if it's a student internship, co-op, or fellowship
   for someone CURRENTLY ENROLLED in school (not a full-time or senior
   role). Be careful with the word "Graduate" — it's ambiguous:
     - "Graduate Co-op", "Graduate Student Intern", "PhD Intern" usually
       mean a student who has graduated undergrad but is still enrolled
       in a graduate (MS/PhD) program — this IS a valid internship type.
     - "Graduate Program", "Graduate Engineer", "Graduate Software
       Engineer" (especially outside the US, e.g. UK/Australia/NZ) 
       usually means a FULL-TIME entry-level hire for someone who has
       ALREADY finished all schooling — this is NOT an internship, even
       though it contains the word "Graduate". Set is_internship to
       FALSE for these.
   When genuinely ambiguous, look at whether the role sounds like a
   fixed-duration, part-time-around-school placement (internship) vs.
   an ongoing full-time job (not an internship).

2. engineering_relevant: true ONLY if the role is genuinely engineering,
   computer science, or applied-technical work — actually building, coding,
   designing, testing, or researching a technical system or product.
   Set this to FALSE for: sales roles (even "AI sales" or "tech sales"),
   marketing/communications roles, veterinary/clinical/medical roles (e.g.
   "DVM Student Externship"), general business operations, HR, finance/
   accounting (unless genuinely quantitative/technical, like quant trading
   or financial engineering), vague "high-paying opportunities" postings
   with no real technical description, and any role whose title's core
   function isn't engineering or technical, even if the word "AI",
   "data", "systems", or "tech" appears somewhere in it.
   When in doubt, set this to false — false negatives here are far less
   harmful than including a role students didn't actually want listed.

3. majors: ONLY if engineering_relevant is true, list which engineering
   majors this role's actual day-to-day work matches, chosen ONLY from:
{majors_list}
   Be conservative and specific — do NOT default to a major just because
   a role mentions software, AI, or data in general.
   For "Electrical Engineering" vs "Computer Engineering" specifically:
     - Electrical Engineering: circuits, power systems, signal processing,
       analog/RF hardware, semiconductors, controls at the hardware level
     - Computer Engineering: embedded systems, firmware, hardware-software
       integration, chip/processor design, computer architecture
     - Neither of these is for general software engineering, web dev, or
       app dev roles with no hardware/embedded/circuit component — use
       "General/Other" for those instead, even if the role is technical.
   A generic "Data Analyst" or "AI Intern" role with no real engineering
   substance should get ["General/Other"], not a specific major it
   doesn't clearly match. If engineering_relevant is false, return an
   empty list for majors.

Jobs:
{jobs_block}

Return ONLY a valid JSON array, no explanation, no markdown formatting, one object
per job, in the SAME ORDER as the input, each shaped like:
{{"index": 0, "is_internship": true, "engineering_relevant": true, "majors": ["Mechanical Engineering"]}}
"""


def classify_jobs_batch(
    jobs: list[dict],
    batch_size: int = 40,
    on_batch_complete=None,
) -> list[dict]:
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

    `on_batch_complete`, if given, is called after EVERY batch as
    on_batch_complete(batch_jobs, batch_results, success: bool) — all
    same length except success, which is one bool for the batch.
    success=False means Gemini failed for this batch (rate limit
    exhausted, parse error) and batch_results are just safe fallback
    defaults, NOT real classifications — callers should NOT cache
    these as truth, since they'd permanently mislabel real postings
    as rejected. Only cache when success=True.

    This callback exists so a caller can save partial progress (e.g.
    the classification cache) incrementally, rather than only at the
    very end. Without this, a run that dies partway through a large
    batch (rate-limit exhaustion, a crash, a Ctrl+C) loses ALL
    classification work from that run, even the batches that
    succeeded before the failure.

    EARLY EXIT for cron/unattended runs: if the API call itself fails
    outright (rate limit exhausted after retries, network error —
    NOT a JSON parse error, which is a different problem and doesn't
    mean the API is unreachable) CONSECUTIVE_FAILURE_LIMIT times in a
    row, this stops attempting further batches entirely rather than
    grinding through every remaining batch, each independently
    burning 3 minutes (3 retries x 60s) only to fail the same way.
    This matters a lot for cron scheduling: once the daily quota is
    hit, every subsequent batch is doomed, so continuing to try them
    just wastes the cron job's runtime for nothing. Remaining
    unprocessed jobs get the same safe `default` + success=False
    treatment as any other failed batch, so nothing crashes and
    nothing gets wrongly cached.

    Returns a list the same length/order as `jobs`, each entry shaped like
    {"is_internship": bool, "engineering_relevant": bool, "majors": [str, ...]}.
    Any job that fails to parse gets a safe default (not an internship,
    not engineering-relevant, untagged) rather than crashing the whole batch.
    """

    CONSECUTIVE_FAILURE_LIMIT = 2
    consecutive_api_failures = 0

    results: list[dict] = [None] * len(jobs)
    default = {"is_internship": False, "engineering_relevant": False, "majors": []}

    total_batches = (len(jobs) + batch_size - 1) // batch_size

    for batch_num, start in enumerate(range(0, len(jobs), batch_size)):
        if batch_num > 0:
            time.sleep(SECONDS_BETWEEN_BATCH_CALLS)

        batch = jobs[start:start + batch_size]
        prompt = build_batch_classification_prompt(batch)
        text = _call_gemini_with_retry(prompt)

        if text is None:
            consecutive_api_failures += 1

            # Gemini genuinely failed here (rate limit exhausted after
            # retries, network error) — this is NOT the same as Gemini
            # judging these postings as non-internships. Using the
            # `default` fallback lets the CURRENT run keep going
            # without crashing, but callers must NOT treat this as a
            # real result worth caching — success=False signals that.
            batch_results_list = [default] * len(batch)
            for i, r in enumerate(batch_results_list):
                results[start + i] = r
            if on_batch_complete:
                on_batch_complete(batch, batch_results_list, False)

            if consecutive_api_failures >= CONSECUTIVE_FAILURE_LIMIT:
                remaining = len(jobs) - (start + len(batch))
                print(f"\n{'!' * 60}")
                print(f"STOPPING EARLY: {consecutive_api_failures} consecutive "
                      f"API failures in a row (likely the daily rate limit, "
                      f"not just the per-minute one — that resets at "
                      f"midnight Pacific, not in a few minutes).")
                print(f"Skipping the remaining {remaining} postings this run "
                      f"instead of burning cron runtime on batches that "
                      f"would almost certainly fail the same way. They'll "
                      f"be classified on the next run once the quota resets.")
                print(f"{'!' * 60}\n")
                for j in range(start + len(batch), len(jobs)):
                    results[j] = default
                break

            continue

        consecutive_api_failures = 0

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            print("Failed to parse Gemini's batch response:", text[:200])
            batch_results_list = [default] * len(batch)
            for i, r in enumerate(batch_results_list):
                results[start + i] = r
            if on_batch_complete:
                on_batch_complete(batch, batch_results_list, False)
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
                engineering_relevant = bool(entry.get("engineering_relevant", False))
                batch_results[idx] = {
                    "is_internship": bool(entry.get("is_internship", False)),
                    "engineering_relevant": engineering_relevant,
                    "majors": majors if engineering_relevant else [],
                }

        batch_results_list = [batch_results[i] for i in range(len(batch))]
        for i, r in enumerate(batch_results_list):
            results[start + i] = r

        if on_batch_complete:
            on_batch_complete(batch, batch_results_list, True)

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