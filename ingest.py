# Interactive CLI for bulk ingestion of heliophysics papers.
#
# Usage (for interactive prompting):
#
#       python ingest.py
#
# Usage (for non-interactive prompts):
#
#       python ingest.py --source ads --start 2025-01 --end 2026-04
#       python ingest.py --source arxiv --max 50
#       python ingest.py --source ads --start 2025-01 --end 2026-04 --keywords "inertial modes,rossby waves"
#       python ingest.py --source daterange --start 20250101 --end 20250131 --max 100
#       python ingest.py --identifier 10.1000/j.jastp.2025.01.001

import argparse
import sys
from datetime import datetime

import httpx

from keywords import DEFAULT_KEYWORDS

base_url = "http://localhost:8000/papers"


def today_ads() -> str:
    """Return today's date in YYYY-MM format for ADS endpoints."""
    return datetime.today().strftime("%Y-%m")


def today_arxiv() -> str:
    """Return today's date in YYYYMMDD format for arXiv daterange endpoint."""
    return datetime.today().strftime("%Y%m%d")


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


def print_result(result: dict, source: str) -> None:
    """Print a clean ingestion summary.

    Args:
        result (dict): The JSON response from the ingestion endpoint.
        source (str): Human-readable label for the source used.
    """
    print("\n── Ingestion complete ─────────────────────────────")
    print(f"  Source          : {source}")
    print(f"  Total found     : {result.get('total_found', 0)}")
    print(f"  Newly ingested  : {result.get('newly_ingested', 0)}")
    print(f"  Already stored  : {result.get('already_stored', 0)}")
    print(f"  Rejected        : {result.get('rejected', 0)}")
    print(f"  Failed          : {result.get('failed', 0)}")
    ids = result.get("arxiv_ids") or result.get("bibcodes") or []
    if ids:
        print(f"  New IDs         : {', '.join(ids)}")
    print("───────────────────────────────────────────────────\n")

def ingest_single(identifier: str) -> None:
    """Fetch and store a single paper by DOI, arXiv ID, or ADS bibcode.
    Hits POST /papers/lookup which fetches from the appropriate
    source and stores the result. Safe to rerun. Already stored papers
    are returned without duplication.

    Args:
        identifier (str): A DOI, arXiv ID, or ADS bibcode.
    """
    print(f"\nFetching single paper: {identifier}")
    with httpx.Client(timeout=60) as client:
        r = client.post(
            f"{base_url}/lookup",
            json={"identifier": identifier, "identifier_type": "ads"},
        )
        if r.status_code == 404:
            print(f"\nERROR: Paper not found: '{identifier}'")
            sys.exit(1)
        r.raise_for_status()
    data = r.json()
    print("\n── Paper fetched ──────────────────────────────────")
    print(f"  Title    : {data.get('title', 'N/A')}")
    print(f"  Journal  : {data.get('journal', 'N/A')}")
    print(f"  Published: {data.get('published_date', 'N/A')}")
    print("───────────────────────────────────────────────────\n")

def ingest_arxiv(max_per_category: int) -> None:
    """Fetch the latest heliophysics papers from arXiv. Hits
    POST /papers/ingest/arxiv. Pulls from all heliophysics arXiv
    categories up to max_per_category papers each.

    Args:
        max_per_category (int): Maximum papers to fetch per category.
    """
    print(
        f"\nFetching latest papers from arXiv (max {max_per_category} per category)..."
    )
    with httpx.Client(timeout=120) as client:
        r = client.post(
            f"{base_url}/ingest/arxiv",
            params={"max_per_category": max_per_category},
        )
        r.raise_for_status()
    print_result(r.json(), "arXiv")


def ingest_daterange(start: str, end: str, max_per_category: int) -> None:
    """Fetch arXiv papers submitted within a specific date range. Hits
    POST /papers/ingest/daterange. Useful for backfilling months of papers
    you haven't ingested yet. Safe to re-run as already stored papers are
    skipped automatically.

    Args:
        start (str): Start date in YYYYMMDD format e.g. '20250101'.
        end (str): End date in YYYYMMDD format e.g. '20250131'.
        max_per_category (int): Maximum papers to fetch per category.
    """
    print(f"\nFetching arXiv papers from {start} to {end}...")
    with httpx.Client(timeout=300) as client:
        r = client.post(
            f"{base_url}/ingest/daterange",
            params={
                "start_date": start,
                "end_date": end,
                "max_per_category": max_per_category,
            },
        )
        r.raise_for_status()
    print_result(r.json(), "arXiv date range")


def ingest_ads(
    start: str, end: str, keywords: str, max_results: int, mode: str = "keyword"
) -> None:
    """Fetch heliophysics papers from NASA ADS within a date range. Hits
    POST /papers/ingest/ads. ADS is preferred over arXiv for published papers
    because it has explicit journal coverage and richer metadata including
    citation counts.

    Args:
        start (str): Start date in YYYY-MM format e.g. '2025-01'.
        end (str): End date in YYYY-MM format e.g. '2026-04'.
        keywords (str): OR-joined keyword string sent to ADS.
        max_results (int): Maximum total papers to retrieve.
        mode (str): Ingestion mode. Either "keyword" or "broad".
    """

    if mode == "broad":
        print(
            f"\nFetching ALL solar physics papers from ADS journals ({start} to {end})..."
        )
        print("Mode: broad (no keyword filter: sweeping ApJ, A&A, SoPh, JGRA, etc.)\n")
    else:
        print(f"\nFetching ADS papers from {start} to {end}...")
        print(f"Keywords: {keywords}\n")
    with httpx.Client(timeout=300) as client:
        r = client.post(
            f"{base_url}/ingest/ads",
            params={
                "start_date": start,
                "end_date": end,
                "keywords": keywords,
                "max_results": max_results,
                "mode": mode,
            },
        )
        r.raise_for_status()
    print_result(r.json(), f"NASA ADS ({mode} mode)")


def run_interactive() -> None:
    """Walk the user through ingestion choices interactively. Prompts
    for source, date range, max results, and keywords. All prompts show
    a default in brackets. Pressing Enter to accept it.
    """
    print("\n══ Heliophysics Paper Ingester ══════════════════════")
    print("  Sources: arxiv | daterange | ads")
    print("  Press Enter to accept defaults shown in [brackets].\n")

    source = prompt("Source", "ads").lower().strip()

    if source == "arxiv":
        max_pc = int(prompt("Max papers per category", "25"))
        ingest_arxiv(max_pc)

    elif source == "daterange":
        start = prompt("Start date (YYYYMMDD)", today_arxiv())
        end = prompt("End date   (YYYYMMDD)", today_arxiv())
        max_pc = int(prompt("Max papers per category", "100"))
        ingest_daterange(start, end, max_pc)

    elif source == "ads":
        start = prompt("Start date (YYYY-MM)", today_ads())
        end = prompt("End date   (YYYY-MM)", today_ads())
        max_r = int(prompt("Max results", "100"))
        mode = prompt("Mode (keyword/broad)", "keyword").lower().strip()
        if mode not in ("keyword", "broad"):
            mode = "keyword"

        print(f"\nDefault keywords ({len(DEFAULT_KEYWORDS)} terms from keywords.py).")
        use_default = prompt("Use default keywords? (yes/no)", "yes").lower()

        if use_default.startswith("y"):
            keywords = " OR ".join(f'abs:"{k}"' for k in DEFAULT_KEYWORDS)
        else:
            raw = input("Enter keywords separated by commas: ").strip()
            keywords = " OR ".join(
                f'abs:"{k.strip()}"' for k in raw.split(",") if k.strip()
            )

        ingest_ads(start, end, keywords, max_r, mode=mode)

    else:
        print(f"Unknown source '{source}'. Choose: arxiv, daterange, ads")
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for non-interactive mode.

    Returns:
        argparse.ArgumentParser: Configured parser with all flags.
    """
    parser = argparse.ArgumentParser(
        description="Bulk ingest heliophysics papers into your local API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--source",
        choices=["arxiv", "daterange", "ads"],
        help="Ingestion source. If omitted, interactive mode is used.",
    )
    parser.add_argument(
        "--start",
        help="Start date. YYYY-MM for ADS, YYYYMMDD for daterange.",
    )
    parser.add_argument(
        "--end",
        help="End date. YYYY-MM for ADS, YYYYMMDD for daterange.",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=100,
        help="Max results (ADS) or max per category (arXiv). Default 100.",
    )

    parser.add_argument(
        "--keywords",
        help="Comma-separated keywords for ADS. Defaults to keywords.py.",
    )
    

    parser.add_argument(
        "--mode",
        choices=["keyword", "broad"],
        default="keyword",
        help="'keyword': filter by keywords (default). 'broad': sweep all core journals.",
    )
    
    parser.add_argument(
    "--identifier",
    help="Single DOI, arXiv ID, or ADS bibcode to fetch and store.",
)
        
    return parser


def run_cli(args: argparse.Namespace) -> None:
    """Run ingestion without prompts using parsed CLI arguments.

    Args:
        args (argparse.Namespace): Parsed arguments from build_parser().
    """
    
    if args.identifier:
        ingest_single(args.identifier)
        return

    if args.source == "arxiv":
        ingest_arxiv(args.max)

    elif args.source == "daterange":
        start = args.start or today_arxiv()
        end = args.end or today_arxiv()
        ingest_daterange(start, end, args.max)

    elif args.source == "ads":
        start = args.start or today_ads()
        end = args.end or today_ads()
        if args.keywords:
            keywords = " OR ".join(
                f'abs:"{k.strip()}"' for k in args.keywords.split(",") if k.strip()
            )
        else:
            keywords = " OR ".join(f'abs:"{k}"' for k in DEFAULT_KEYWORDS)
        ingest_ads(start, end, keywords, args.max, mode=args.mode)


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.identifier:
            ingest_single(args.identifier)
        elif args.source:
            run_cli(args)
        else:
            run_interactive()
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
