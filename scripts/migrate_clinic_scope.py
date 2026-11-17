"""One-off migration to the clinic-scoped data model (clinic data contract).

1. Backfill ``clinicId`` (= this deployment's ``CLINIC_ID``) on every document
   written before scoping: clinic, doctors, patients, appointments, transcripts,
   pending_messages. Documents that already have a clinicId are left alone.
2. Create the clinic-scoped indexes (same as ``ensure_indexes`` on boot).
3. Drop the legacy phone-only unique indexes they replace, which would otherwise
   stop the same patient phone from existing in two clinics.

Idempotent — safe to re-run. Preview first:

    uv run python scripts/migrate_clinic_scope.py --dry-run
    uv run python scripts/migrate_clinic_scope.py
"""

from __future__ import annotations

import argparse

from dotenv import load_dotenv

load_dotenv()

from pymongo.errors import OperationFailure  # noqa: E402

from clinic_agent.config import settings  # noqa: E402
from clinic_agent.db import get_db  # noqa: E402
from clinic_agent.runtime import conversations, debounce  # noqa: E402
from clinic_agent.tools import repo  # noqa: E402

BACKFILL = ["clinic", "doctors", "patients", "appointments", "transcripts", "pending_messages"]
LEGACY_INDEXES = [("patients", "uniq_phone"), ("pending_messages", "uniq_pending_phone")]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    args = parser.parse_args()

    db = get_db()
    clinic_id = settings.CLINIC_ID
    mode = "DRY RUN — " if args.dry_run else ""
    print(f"{mode}db='{db.name}', clinicId='{clinic_id}'")

    missing = {"clinicId": {"$exists": False}}
    for name in BACKFILL:
        n = db[name].count_documents(missing)
        if n and not args.dry_run:
            db[name].update_many(missing, {"$set": {"clinicId": clinic_id}})
        print(f"  {name}: {n} document(s) {'to backfill' if args.dry_run else 'backfilled'}")

    if args.dry_run:
        for col, index in LEGACY_INDEXES:
            exists = index in db[col].index_information()
            print(f"  {col}.{index}: {'would drop' if exists else 'already gone'}")
        print("Nothing changed.")
        return

    repo.ensure_indexes()
    conversations.ensure_indexes()
    debounce.ensure_indexes()
    print("  clinic-scoped indexes ensured")

    for col, index in LEGACY_INDEXES:
        if index not in db[col].index_information():
            print(f"  {col}.{index}: already gone")
            continue
        try:
            db[col].drop_index(index)
            print(f"  {col}.{index}: dropped")
        except OperationFailure as exc:
            print(
                f"  {col}.{index}: could NOT drop ({exc.details.get('errmsg', exc)}). "
                "Drop it in Atlas (Collections → Indexes) with a user that has dropIndex."
            )
    print("Migration complete.")


if __name__ == "__main__":
    main()
