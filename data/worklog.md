
## 2026-09-04 13:12 — Gemini API failures — likely daily rate limit hit

2 consecutive batch failures during classification. This usually means the Gemini free tier's daily quota (not just the per-minute one) has been exhausted — it resets around midnight Pacific. Any postings that didn't get classified this run will be picked up automatically on the next run, thanks to the classification cache. No action needed unless this keeps happening across multiple days in a row, which would suggest the daily volume has outgrown the free tier.
