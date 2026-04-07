# Find and fix incomplete records in the local paper collection.
# Scans all stored papers for missing fields and outdated citation counts,
# then re-fetches from the appropriate source to fill them in.

# Usage (interactive prompting):
#
#       python backfill.py

# Usage (non-interactive prompting):
#
#       python backfill.py --target all
#       python backfill.py --target urls
#       python backfill.py --target citations
#       python backfill.py --target missing_ids
#       python backfill.py --dry-run --target all


import argparse
import sys
from datetime import datetime, timezone

import httpx

BASE_URL = "http://localhost:8000/papers"

# Citation counts older than this many days are considered stale
CITATION_STALE_DAYS = 30


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


def fetch_all_papers(client: httpx.Client) -> list[dict]:
    """Retrieve every paper stored in Postgres via paginated list endpoint.

    Walks through all pages using limit/offset until no more papers
    are returned.

    Args:
        client (httpx.Client): The shared HTTP client.

    Returns:
        list[dict]: All stored papers as raw dicts.
    """
    papers = []
    offset = 0
    limit = 100

    while True:
        r = client.get(f"{BASE_URL}/", params={"limit": limit, "offset": offset})
        r.raise_for_status()
        data = r.json()
        batch = data["papers"]
        papers.extend(batch)
        if len(batch) < limit:
            break
        offset += limit

    return papers


def fix_url(paper: dict) -> str | None:
    """Derive a URL from known identifiers if the url field is missing.

    Constructs the URL locally without hitting any external API.
    arXiv and DOI URLs follow predictable patterns.

    Args:
        paper (dict): The raw paper dict from the API.

    Returns:
        str | None: The derived URL, or None if neither identifier is available.
    """
    if paper.get("arxiv_id"):
        return f"https://arxiv.org/abs/{paper['arxiv_id']}"
    if paper.get("doi"):
        return f"https://doi.org/{paper['doi']}"
    if paper.get("identifier_type") == "ads":
        return f"https://ui.adsabs.harvard.edu/abs/{paper['identifier']}"
    return None


def is_citation_stale(paper: dict) -> bool:
    """Check whether a paper's citation count is old enough to re-fetch.

    Args:
        paper (dict): The raw paper dict from the API.

    Returns:
        bool: True if the citation count should be refreshed.
    """
    fetched_at = paper.get("fetched_at")
    if not fetched_at:
        return True
    fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    age_days = (datetime.now(timezone.utc) - fetched).days
    return age_days >= CITATION_STALE_DAYS


def fetch_citation_count_from_ads(client: httpx.Client, paper: dict) -> int | None:
    """Try to get a citation count from NASA ADS as a fallback.

    Uses the paper's arxiv_id or doi to search ADS for a citation count
    when Semantic Scholar returns nothing. ADS indexes most published
    heliophysics papers and usually has citation data even for recent ones.

    Args:
        client (httpx.Client): The shared HTTP client.
        paper (dict): The raw paper dict from the API.

    Returns:
        int | None: The citation count if found, None otherwise.
    """
    from app.config import settings
    import urllib.parse

    arxiv_id = paper.get("arxiv_id")
    doi = paper.get("doi")
    identifier = paper.get("identifier")

    # Build the ADS query
    # try arxiv_id first, then doi, then bibcode
    if arxiv_id:
        query = f"arxiv:{arxiv_id}"
    elif doi:
        query = f"doi:{doi}"
    elif paper.get("identifier_type") == "ads":
        query = f"bibcode:{identifier}"
    else:
        return None

    encoded = urllib.parse.quote(query, safe="")
    url = (
        f"https://api.adsabs.harvard.edu/v1/search/query?q={encoded}&fl=citation_count"
    )

    try:
        r = client.get(
            url,
            headers={"Authorization": f"Bearer {settings.ads_api_token}"},
            timeout=15,
        )
        r.raise_for_status()
        docs = r.json().get("response", {}).get("docs", [])
        if docs and docs[0].get("citation_count") is not None:
            return docs[0]["citation_count"]
        return None
    except Exception:
        return None


def patch_paper(
    client: httpx.Client, identifier: str, identifier_type: str, dry_run: bool
) -> dict | None:
    """Re-fetch a single paper via the lookup endpoint to get fresh data.

    Args:
        client (httpx.Client): The shared HTTP client.
        identifier (str): The paper's identifier (DOI, arXiv ID, or ADS bibcode).
        identifier_type (str): One of 'doi', 'arxiv', 'ads'.
        dry_run (bool): If True, skip the actual API call.

    Returns:
        dict | None: The updated paper data, or None if dry run or failed.
    """
    if dry_run:
        return None
    r = client.post(
        f"{BASE_URL}/lookup",
        json={"identifier": identifier, "identifier_type": identifier_type},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def print_summary(
    label: str, checked: int, fixed: int, skipped: int, dry_run: bool
) -> None:
    """Print a clean summary for one backfill pass.

    Args:
        label (str): Name of the backfill target e.g. 'URLs'.
        checked (int): Total papers examined.
        fixed (int): Papers that were updated.
        skipped (int): Papers that already had the field populated.
        dry_run (bool): Whether this was a dry run.
    """
    tag = " (dry run)" if dry_run else ""
    print(f"\n── {label}{tag} ───────────────────────────────────────")
    print(f"  Checked  : {checked}")
    print(f"  Fixed    : {fixed}")
    print(f"  Skipped  : {skipped}")
    print("───────────────────────────────────────────────────\n")


def backfill_urls(papers: list[dict], dry_run: bool) -> None:
    """Fix papers that are missing a URL field.

    For arXiv and DOI papers the URL can be derived locally without
    an external API call. ADS papers get a link to their ADS abstract page.

    Args:
        papers (list[dict]): All stored papers.
        dry_run (bool): If True, report what would be fixed without patching.
    """
    missing = [p for p in papers if not p.get("url")]
    skipped = len(papers) - len(missing)
    fixed = 0

    print(f"\nChecking URLs — {len(missing)} missing out of {len(papers)} papers...")

    for paper in missing:
        url = fix_url(paper)
        if url:
            if not dry_run:
                print(f"  ✓ {paper['identifier']} → {url}")
            else:
                print(f"  [dry run] would fix: {paper['identifier']} → {url}")
            fixed += 1
        else:
            print(f"  ✗ {paper['identifier']} — cannot derive URL, no arxiv_id or doi")

    print_summary("URLs", len(papers), fixed, skipped, dry_run)


def backfill_missing_ids(
    papers: list[dict], client: httpx.Client, dry_run: bool
) -> None:
    """Re-fetch papers that are missing arxiv_id or doi.

    These fields are sometimes absent when a paper is ingested from a
    source that doesn't cross-reference other databases. Re-fetching
    via the lookup endpoint triggers the full enrichment pipeline.

    Args:
        papers (list[dict]): All stored papers.
        client (httpx.Client): The shared HTTP client.
        dry_run (bool): If True, report what would be fixed without patching.
    """
    missing = [p for p in papers if not p.get("arxiv_id") or not p.get("doi")]
    skipped = len(papers) - len(missing)
    fixed = 0

    print(
        f"\nChecking missing IDs — {len(missing)} incomplete out of {len(papers)} papers..."
    )

    for paper in missing:
        missing_fields = []
        if not paper.get("arxiv_id"):
            missing_fields.append("arxiv_id")
        if not paper.get("doi"):
            missing_fields.append("doi")

        print(f"  {paper['identifier']} — missing: {', '.join(missing_fields)}")

        result = patch_paper(
            client, paper["identifier"], paper["identifier_type"], dry_run
        )
        if result and result.get("arxiv_id") or result and result.get("doi"):
            print("Updated")
            fixed += 1
        elif not dry_run:
            print("Still missing after re-fetch (source may not provide these)")

    print_summary("Missing IDs", len(papers), fixed, skipped, dry_run)


def backfill_citations(papers: list[dict], client: httpx.Client, dry_run: bool) -> None:
    """Re-fetch citation counts for papers with missing or stale counts.

    Tries Semantic Scholar first via the lookup endpoint. If that returns
    nothing, falls back to NASA ADS which has citation data for most
    published heliophysics papers.

    Args:
        papers (list[dict]): All stored papers.
        client (httpx.Client): The shared HTTP client.
        dry_run (bool): If True, report what would be fixed without patching.
    """
    stale = [
        p for p in papers if p.get("citation_count") is None or is_citation_stale(p)
    ]
    skipped = len(papers) - len(stale)
    fixed = 0

    print(
        f"\nChecking citations — {len(stale)} stale or missing out of {len(papers)} papers..."
    )

    for paper in stale:
        current = paper.get("citation_count")
        print(f"  {paper['identifier']} — current count: {current}")

        if dry_run:
            ads_count = fetch_citation_count_from_ads(client, paper)
            if ads_count is not None:
                print(f" [dry run] ADS has citation count: {ads_count}")
            else:
                print(" [dry run] ADS also has no citation count")
            continue

        # Try Semantic Scholar first via lookup endpoint
        result = patch_paper(
            client, paper["identifier"], paper["identifier_type"], dry_run=False
        )
        if result and result.get("citation_count") is not None:
            print(f" Semantic Scholar: updated to {result['citation_count']}")
            fixed += 1
            continue

        # Fallback to ADS
        ads_count = fetch_citation_count_from_ads(client, paper)
        if ads_count is not None:
            print(f"ADS fallback: {ads_count}")
            fixed += 1
        else:
            print("Neither Semantic Scholar nor ADS has a citation count")

    print_summary("Citations", len(papers), fixed, skipped, dry_run)


def run_interactive(client: httpx.Client) -> None:
    """Walk the user through backfill choices interactively.

    Args:
        client (httpx.Client): The shared HTTP client.
    """
    print("\n══ Heliophysics Paper Backfiller ════════════════════")
    print("  Targets: urls | missing_ids | citations | all")
    print("  Press Enter to accept defaults shown in [brackets].\n")

    target = prompt("Target", "all").lower().strip()
    dry_run = prompt("Dry run? (yes/no)", "no").lower().startswith("y")

    if dry_run:
        print("\n  Dry run enabled — no changes will be made.\n")

    papers = fetch_all_papers(client)
    print(f"  Found {len(papers)} papers in collection.")

    if target in ("urls", "all"):
        backfill_urls(papers, dry_run)

    if target in ("missing_ids", "all"):
        backfill_missing_ids(papers, client, dry_run)

    if target in ("citations", "all"):
        backfill_citations(papers, client, dry_run)

    if target not in ("urls", "missing_ids", "citations", "all"):
        print(f"Unknown target '{target}'. Choose: urls, missing_ids, citations, all")
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for non-interactive mode.

    Returns:
        argparse.ArgumentParser: Configured parser with all flags.
    """
    parser = argparse.ArgumentParser(
        description="Backfill missing or stale fields in your heliophysics paper collection.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--target",
        choices=["urls", "missing_ids", "citations", "all"],
        help="Which fields to backfill. If omitted, interactive mode is used.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be fixed without making any changes.",
    )
    return parser


def run_cli(args: argparse.Namespace, client: httpx.Client) -> None:
    """Run backfill without prompts using parsed CLI arguments.

    Args:
        args (argparse.Namespace): Parsed arguments from build_parser().
        client (httpx.Client): The shared HTTP client.
    """
    if args.dry_run:
        print("\n  Dry run enabled — no changes will be made.\n")

    papers = fetch_all_papers(client)
    print(f"  Found {len(papers)} papers in collection.")

    if args.target in ("urls", "all"):
        backfill_urls(papers, args.dry_run)

    if args.target in ("missing_ids", "all"):
        backfill_missing_ids(papers, client, args.dry_run)

    if args.target in ("citations", "all"):
        backfill_citations(papers, client, args.dry_run)


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    try:
        with httpx.Client(timeout=60) as client:
            if args.target:
                run_cli(args, client)
            else:
                run_interactive(client)
    except httpx.ConnectError:
        print("\nERROR: Could not connect to the API. Is uvicorn running?")
        print("  Start it with: uvicorn app.main:app --reload\n")
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        print(f"\nERROR: API returned {e.response.status_code}")
        print(e.response.text)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(0)
