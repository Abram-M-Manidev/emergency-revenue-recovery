"""Operator-only export of early-access requests, as CSV on stdout.

    python -m app.cli.leads [--limit N]

Read-only. The output is personal data: write it only to somewhere you are
allowed to keep it, and never into a ticket, chat or log. There is no
tenant-facing way to read leads; they belong to the platform operator.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys

from app.infrastructure.database.repositories import SqlAlchemyLeadRepository
from app.infrastructure.database.session import AsyncSessionLocal

_COLUMNS = (
    "created_at",
    "name",
    "business_name",
    "email",
    "business_type",
    "consent_version",
    "consented_at",
    "request_count",
)


async def _export(limit: int) -> int:
    async with AsyncSessionLocal() as session:
        leads = await SqlAlchemyLeadRepository(session).list_all(limit=limit)
    writer = csv.writer(sys.stdout)
    writer.writerow(_COLUMNS)
    for lead in leads:
        writer.writerow(
            [
                lead.created_at.isoformat(),
                lead.name,
                lead.business_name,
                lead.email,
                lead.business_type,
                lead.consent_version,
                lead.consented_at.isoformat(),
                lead.request_count,
            ]
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args(argv)
    return asyncio.run(_export(args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
