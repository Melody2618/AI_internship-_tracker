# ============================================================
# TEAM NOTE (read before running):
# This uses a Gemini API key stored in a local .env file.
# Do NOT use my key for your own testing — each of us
# should create our own free API key at https://aistudio.google.com/apikey
# and store it in our OWN .env file (which is git-ignored, so
# it won't be pushed or overwritten by anyone else's).
#
# Steps to add your own key:
#   1. Go to https://aistudio.google.com/apikey and create a key
#   2. In this project folder, create a file named .env
#   3. Add this line to it: GEMINI_API_KEY=your_key_here
#   4. Save. Don't commit this file — it's already in .gitignore
#
# Why separate keys matter: Gemini's free tier rate limit
# (currently ~15 requests/minute, ~1500/day) is per API key.
# If we all test using the same key, we'll hit that ceiling
# much faster and start seeing errors.
# ============================================================
 
from google import genai
from dotenv import load_dotenv
import os
import json
import re
import time
 
load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
 
MODEL = "gemini-flash-latest"  # auto-updates to Google's current Flash model
 
# Retry settings for hitting the rate limit (HTTP 429 errors)
MAX_RETRIES = 3
RETRY_WAIT_SECONDS = 60  # free tier resets roughly every 60 seconds
 
 
def build_prompt(raw_text):
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
    """
    Sends one posting's text to Gemini and returns a parsed dict.
    Automatically waits and retries if we hit the rate limit.
    """
    prompt = build_prompt(raw_text)
 
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=prompt
            )
            text = response.text.strip()
            text = re.sub(r"^```json|```$", "", text, flags=re.MULTILINE).strip()
 
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                print("Failed to parse Gemini's response as JSON:", text)
                return None
 
        except Exception as e:
            # Check if this looks like a rate limit / quota error
            if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
                print(f"Rate limit hit (attempt {attempt}/{MAX_RETRIES}). "
                      f"Waiting {RETRY_WAIT_SECONDS} seconds before retrying...")
                time.sleep(RETRY_WAIT_SECONDS)
            else:
                print("Unexpected error calling Gemini:", e)
                return None
 
    print("Gave up after hitting the rate limit multiple times.")
    return None