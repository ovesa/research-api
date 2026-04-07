# Find and merge duplicate papers in your local collection.

# A duplicate is defined as two records sharing the same DOI. This happens
# when the same paper is ingested once as an arXiv preprint and again as a
# published paper via ADS. The richer record is kept (ADS preferred), the
# weaker one is deleted.

# Usage (interactive):
#
#       python deduplicate.py
#
# Usage (non-interactive):
#
#       python deduplicate.py --dry-run
#       python deduplicate.py --merge


import argparse
import sys

import asyncpg
import asyncio

from app.config import settings


async def get_pool() -> asyncpg.Pool:
    """Create a direct asyncpg connection pool to Postgres.

    Args:
        None

    Returns:
        asyncpg.Pool: Connected pool ready for queries.
    """
    return await asyncpg.create_pool(settings.database_url)


async def find_duplicates(pool: asyncpg.Pool) -> list[dict]:
    """Find all pairs of records that share the same DOI.

    Queries Postgres for any DOI that appears in more than one row.
    Returns each duplicate group with both records so we can decide
    which one to keep.

    Args:
        pool (asyncpg.Pool): The database connection pool.

    Returns:
        list[dict]: Each entry contains the shared doi and both records
            as 'keeper' and 'duplicate', with the ADS record preferred
            as keeper.
    """
    async with pool.acquire() as conn:
        # Find all DOIs that appear more than once
        rows = await conn.fetch(
            """
            SELECT doi, array_agg(identifier) AS identifiers,
                   array_agg(identifier_type) AS types,
                   array_agg(source) AS sources,
                   array_agg(citation_count) AS citation_counts,
                   array_agg(abstract IS NOT NULL) AS has_abstract
            FROM papers
            WHERE doi IS NOT NULL AND doi != ''
            GROUP BY doi
            HAVING COUNT(*) > 1
            """
        )

    duplicates = []
    for row in rows:
        identifiers = row["identifiers"]
        types = row["types"]
        sources = row["sources"]
        citation_counts = row["citation_counts"]
        has_abstracts = row["has_abstract"]

        # Build a list of candidates with their metadata
        candidates = [
            {
                "identifier": identifiers[i],
                "type": types[i],
                "source": sources[i],
                "citation_count": citation_counts[i],
                "has_abstract": has_abstracts[i],
            }
            for i in range(len(identifiers))
        ]

        # Pick the default; prefer ADS, then whichever has citation count,
        # then whichever has an abstract
        def keeper_score(c: dict) -> tuple:
            return (
                c["source"] == "ads",
                c["citation_count"] is not None,
                c["has_abstract"],
            )

        candidates.sort(key=keeper_score, reverse=True)
        keeper = candidates[0]
        to_delete = candidates[1:]

        duplicates.append(
            {
                "doi": row["doi"],
                "keeper": keeper,
                "duplicates": to_delete,
            }
        )

    return duplicates


async def merge_duplicates(
    pool: asyncpg.Pool, groups: list[dict], dry_run: bool
) -> None:
    """Delete the weaker record in each duplicate group.

    The keeper record already has the best metadata. Deleting the weaker
    record is sufficient. No field merging needed because ADS records
    are always more complete than arXiv-only records for published papers.

    Args:
        pool (asyncpg.Pool): The database connection pool.
        groups (list[dict]): Duplicate groups from find_duplicates().
        dry_run (bool): If True, report what would be deleted without
            making any changes.
    """
    total_deleted = 0

    for group in groups:
        doi = group["doi"]
        keeper = group["keeper"]
        to_delete = group["duplicates"]

        print(f"\n  DOI: {doi}")
        print(
            f"  ✓ keeping  : {keeper['identifier']} ({keeper['source']})"
            f" — citations: {keeper['citation_count']}, abstract: {keeper['has_abstract']}"
        )

        for dup in to_delete:
            print(
                f"  ✗ removing : {dup['identifier']} ({dup['source']})"
                f" — citations: {dup['citation_count']}, abstract: {dup['has_abstract']}"
            )

            if not dry_run:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "DELETE FROM papers WHERE identifier = $1",
                        dup["identifier"],
                    )
                total_deleted += 1

    print(f"\n── Deduplication {'(dry run) ' if dry_run else ''}complete ─────────────")
    print(f"  Duplicate groups found : {len(groups)}")
    print(
        f"  Records {'would be ' if dry_run else ''}deleted  : "
        f"{sum(len(g['duplicates']) for g in groups)}"
    )
    if not dry_run:
        print(f"  Actually deleted       : {total_deleted}")
    print("───────────────────────────────────────────────────\n")


async def run(dry_run: bool) -> None:
    """Main entry point: find and optionally merge duplicates.

    Args:
        dry_run (bool): If True, report without making changes.
    """
    pool = await get_pool()

    try:
        print("\nScanning for duplicate DOIs...")
        groups = await find_duplicates(pool)

        if not groups:
            print("\n  No duplicates found — collection is clean.\n")
            return

        print(f"  Found {len(groups)} duplicate group(s).\n")
        await merge_duplicates(pool, groups, dry_run)

    finally:
        await pool.close()


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        argparse.ArgumentParser: Configured parser.
    """
    parser = argparse.ArgumentParser(
        description="Find and merge duplicate papers sharing the same DOI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be merged without making any changes.",
    )
    group.add_argument(
        "--merge",
        action="store_true",
        help="Find and merge duplicates immediately without prompting.",
    )
    return parser


def prompt(question: str, default: str) -> str:
    """Prompt the user with a default value shown in brackets.

    Args:
        question (str): The question to display.
        default (str): The default value if the user presses Enter.

    Returns:
        str: The user's input or the default value.
    """
    answer = input(f"{question} [{default}]: ").strip()
    return answer if answer else default


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.dry_run:
            asyncio.run(run(dry_run=True))
        elif args.merge:
            asyncio.run(run(dry_run=False))
        else:
            # Interactive mode
            print("\n══ Heliophysics Duplicate Detector ══════════════════")
            print("  Compares all stored papers by DOI.")
            print("  Keeps the richer record (ADS preferred), deletes the rest.\n")
            dry = prompt("Dry run first? (yes/no)", "yes").lower().startswith("y")
            asyncio.run(run(dry_run=dry))
            if dry:
                proceed = (
                    prompt("\nProceed with merge? (yes/no)", "no")
                    .lower()
                    .startswith("y")
                )
                if proceed:
                    asyncio.run(run(dry_run=False))
                else:
                    print("Cancelled — no changes made.")

    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(0)
