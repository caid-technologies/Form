#!/usr/bin/env python3
"""Backfill and reconcile canonical project data and compatibility projections."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forma_core.database import get_database_provider  # noqa: E402
from forma_core.persistence.project_reconciliation import (  # noqa: E402
    load_retry_project_ids,
    reconcile_sqlite,
    reconcile_supabase,
    write_reconciliation_report,
)
from forma_core.persistence.providers.supabase import SupabaseProvider  # noqa: E402
from forma_core.persistence.providers.sqlite import SQLiteProvider  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill canonical project identities/revisions and reconcile compatibility projections."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True, help="Report planned changes without writing (default).")
    mode.add_argument("--apply", dest="dry_run", action="store_false", help="Apply idempotent backfill and projection repairs.")
    parser.add_argument("--retry-failed", action="store_true", help="Retry only failed project IDs from --audit.")
    parser.add_argument("--audit", type=Path, required=True, help="Path for the JSON audit/reconciliation report.")
    parser.add_argument("--limit", type=int, default=None, help="Process at most this many project records.")
    parser.add_argument("--json", action="store_true", help="Print the full report as JSON.")
    args = parser.parse_args()

    retry_ids = None
    if args.retry_failed:
        retry_ids = load_retry_project_ids(args.audit)
    provider = get_database_provider()
    provider.initialize()
    if isinstance(provider, SQLiteProvider) and provider.session_factory is not None:
        report = reconcile_sqlite(
            provider.session_factory,
            dry_run=args.dry_run,
            retry_project_ids=retry_ids,
            limit=args.limit,
        )
    elif isinstance(provider, SupabaseProvider):
        report = reconcile_supabase(
            provider.client,
            dry_run=args.dry_run,
            retry_project_ids=retry_ids,
            limit=args.limit,
        )
    else:
        parser.error("The selected database provider does not support project reconciliation.")
    write_reconciliation_report(report, args.audit)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print(
            f"reconciliation run={report.run_id} dry_run={report.dry_run} "
            f"scanned={report.scanned} migrated={report.migrated} repaired={report.repaired} "
            f"mismatches={report.mismatches} skipped={report.skipped} failed={report.failed}"
        )
        print(f"audit={args.audit}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
