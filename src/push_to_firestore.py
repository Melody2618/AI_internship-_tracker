"""
Pushes the pipeline's final job list into Firestore, replacing whatever
was previously in the `jobs` collection.

This is the file that actually makes the Firebase gating real: nothing in
this script, and nothing it reads (data/jobs_output.json), ever gets
committed to the repo. Once this runs, the job data lives only in
Firestore, guarded by firestore.rules. The repo itself stays public
(required for GitHub Pages on the free plan) without leaking listings.

Requires:
    pip install firebase-admin

Environment:
    GOOGLE_APPLICATION_CREDENTIALS - path to a service account JSON file.
    Set by the GitHub Actions workflow from the FIREBASE_SERVICE_ACCOUNT
    secret; never commit a service account file to the repo.
"""

import json
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore

INPUT_PATH = Path("data/jobs_output.json")
COLLECTION_NAME = "jobs"
BATCH_LIMIT = 400  # Firestore batched writes cap at 500 operations


def main() -> None:
    if not INPUT_PATH.exists():
        raise SystemExit(f"{INPUT_PATH} not found, run src/main.py first.")

    jobs = json.loads(INPUT_PATH.read_text())

    cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    collection_ref = db.collection(COLLECTION_NAME)

    current_ids = set()
    batch = db.batch()
    ops_in_batch = 0

    for job in jobs:
        doc_id = str(job.get("id", "")).strip().replace("/", "_")
        if not doc_id:
            continue

        current_ids.add(doc_id)
        batch.set(collection_ref.document(doc_id), job)
        ops_in_batch += 1

        if ops_in_batch >= BATCH_LIMIT:
            batch.commit()
            batch = db.batch()
            ops_in_batch = 0

    if ops_in_batch:
        batch.commit()

    # Remove postings that dropped off this run (closed/expired listings)
    # so Firestore doesn't accumulate stale postings forever.
    delete_batch = db.batch()
    delete_ops = 0
    removed = 0

    for doc in collection_ref.stream():
        if doc.id not in current_ids:
            delete_batch.delete(doc.reference)
            delete_ops += 1
            removed += 1

            if delete_ops >= BATCH_LIMIT:
                delete_batch.commit()
                delete_batch = db.batch()
                delete_ops = 0

    if delete_ops:
        delete_batch.commit()

    print(
        f"Pushed {len(current_ids)} postings to Firestore, "
        f"removed {removed} stale postings."
    )


if __name__ == "__main__":
    main()