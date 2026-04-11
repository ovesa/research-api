# Export heliophysics papers from the database to a BibTeX .bib file.
# Fetches official BibTeX entries directly from NASA ADS for accuracy.
# Falls back to generated entries if ADS is unavailable.
#
#Usage:
#
# Export everything
#    python export_bibtex.py --output refs.bib
#
# Filter by keywords (matches title or abstract)
#    python export_bibtex.py --keywords "inertial modes,rossby waves" --output inertial_modes.bib
#
# Filter by Claude extraction relevance
#    python export_bibtex.py --relevance primary --output primary.bib
#
# Filter by date range
#    python export_bibtex.py --start 2023-01 --end 2026-05 --output recent.bib
#
# Combine filters
#    python export_bibtex.py --keywords "inertial modes" --relevance primary --start 2023-01 --output recent_primary.bib

import argparse
import os
import re
import sys
import time
from datetime import datetime

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

ADS_TOKEN = os.getenv("ADS_API_TOKEN", "")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://researchapi:researchapi@localhost:5432/researchapi",
)

####################################################
##################### Database #####################
####################################################

def _get_connection():
    """Connect to Postgres."""
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def _fetch_papers(
    conn,
    keywords: list[str] | None = None,
    relevance: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """Fetch papers from Postgres with optional filters.
    Joins with extractions table so relevance filter works even
    if not all papers have been extracted yet.

    Args:
        conn: Postgres connection.
        keywords (list[str] | None): Filter by keywords in title or abstract.
        relevance (str | None): Filter by Claude extraction relevance.
        start_date (str | None): Filter papers published on or after this date.
        end_date (str | None): Filter papers published on or before this date.

    Returns:
        list[dict]: Papers matching all filters, newest first.
    """
    conditions = []
    params = []

    if keywords:
        keyword_conditions = []
        for keyword in keywords:
            params.append(f"%{keyword.lower()}%")
            params.append(f"%{keyword.lower()}%")
            keyword_conditions.append(
                "(LOWER(p.title) LIKE %s OR LOWER(p.abstract) LIKE %s)"
            )
        conditions.append("(" + " OR ".join(keyword_conditions) + ")")

    if relevance:
        params.append(relevance)
        conditions.append("e.relevance_to_solar_inertial_modes = %s")

    if start_date:
        params.append(start_date)
        conditions.append("p.published_date >= %s")

    if end_date:
        params.append(end_date)
        conditions.append("p.published_date <= %s")

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

    query = f"""
        SELECT
            p.identifier,
            p.title,
            p.authors,
            p.abstract,
            p.published_date,
            p.journal,
            p.doi,
            p.arxiv_id,
            p.url,
            e.relevance_to_solar_inertial_modes,
            e.researcher_summary
        FROM papers p
        LEFT JOIN extractions e ON p.identifier = e.identifier
        {where_clause}
        ORDER BY p.published_date DESC
    """

    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()

####################################################
################# ADS BibTeX fetch #################
####################################################

def _fetch_bibtex_from_ads(bibcode: str) -> str | None:
    """Fetch official BibTeX entry from NASA ADS. Uses the 
    ADS export API which returns the same BibTeX you see on 
    the ADS abstract page. This is the gold standard format,
    which has correct journal abbreviations, volume, page 
    numbers, and all metadata exactly as ADS has it.

    Args:
        bibcode (str): ADS bibcode e.g. '2025ApJ...989...26D'

    Returns:
        str | None: Raw BibTeX string, or None if fetch failed.
    """
    if not ADS_TOKEN:
        return None

    url = f"https://api.adsabs.harvard.edu/v1/export/bibtex/{bibcode}"
    try:
        response = httpx.get(
            url,
            headers={
                "Authorization": f"Bearer {ADS_TOKEN}",
                "User-Agent": "research-api/0.1 (heliophysics paper lookup)",
            },
            timeout=15,
        )
        if response.status_code == 200:
            data = response.json()
            return data.get("export", "").strip()
        elif response.status_code == 429:
            print("  WARNING: ADS rate limited — waiting 10 seconds...")
            time.sleep(10)
            return _fetch_bibtex_from_ads(bibcode)
        else:
            print(f"  WARNING: ADS returned {response.status_code} for {bibcode}")
            return None
    except Exception as e:
        print(f"  WARNING: Failed to fetch BibTeX for {bibcode}: {e}")
        return None

####################################################
############# Fallback BibTeX generator ############
####################################################

def _make_cite_key(paper: dict) -> str:
    """Generate a citation key from bibcode and first author.

    Args:
        paper (dict): Paper row from Postgres.

    Returns:
        str: Clean citation key safe for LaTeX.
    """
    identifier = paper["identifier"]
    year_match = re.match(r"(\d{4})", identifier)
    year = year_match.group(1) if year_match else "0000"

    authors = paper.get("authors") or []
    if authors and isinstance(authors, list):
        first_author = authors[0]
        name = (
            first_author.get("name", "")
            if isinstance(first_author, dict)
            else str(first_author)
        )
        last_name = name.split(",")[0].split()[-1] if name else "Unknown"
        last_name = re.sub(r"[^a-zA-Z]", "", last_name)
    else:
        last_name = "Unknown"

    journal_match = re.match(r"\d{4}([A-Za-z&]+)", identifier)
    journal_abbr = (
        re.sub(r"[^a-zA-Z]", "", journal_match.group(1)) if journal_match else "Unk"
    )

    return f"{last_name}{year}{journal_abbr}"


def _format_authors(authors: list) -> str:
    """Format author list for BibTeX.

    Args:
        authors (list): List of author dicts with 'name' key.

    Returns:
        str: Authors joined by ' and ' for BibTeX.
    """
    if not authors:
        return "Unknown"

    formatted = []
    for author in authors:
        name = author.get("name", "") if isinstance(author, dict) else str(author)
        if name:
            formatted.append(name)

    if not formatted:
        return "Unknown"

    if len(formatted) > 8:
        return " and ".join(formatted[:8]) + " and others"

    return " and ".join(formatted)


def _generate_bibtex_entry(paper: dict) -> str:
    """Generate a BibTeX entry from paper metadata.
    Used as fallback when ADS fetch fails.

    Args:
        paper (dict): Paper row from Postgres.

    Returns:
        str: BibTeX entry string.
    """
    cite_key = _make_cite_key(paper)
    journal = paper.get("journal") or ""
    entry_type = "misc" if "arxiv" in journal.lower() else "article"

    published_date = paper.get("published_date") or ""
    year = published_date[:4] if published_date else "0000"
    month = published_date[5:7] if len(published_date) >= 7 else ""

    title = paper.get("title") or "Untitled"
    title = title.replace("&", r"\&").replace("_", r"\_").replace("%", r"\%")

    lines = [f"@{entry_type}{{{cite_key},"]
    lines.append(f"  author  = {{{_format_authors(paper.get('authors') or [])}}},")
    lines.append(f"  title   = {{{{{title}}}}},")

    if journal and "arxiv" not in journal.lower():
        lines.append(f"  journal = {{{journal}}},")

    lines.append(f"  year    = {{{year}}},")

    if month:
        lines.append(f"  month   = {{{month}}},")
    if paper.get("doi"):
        lines.append(f"  doi     = {{{paper['doi']}}},")
    if paper.get("url"):
        lines.append(f"  url     = {{{paper['url']}}},")
    if paper.get("arxiv_id"):
        lines.append(f"  eprint  = {{{paper['arxiv_id']}}},")
        lines.append("  archivePrefix = {{arXiv}},")

    lines.append(f"  note    = {{ADS: {paper['identifier']}}},")
    lines.append("}")
    return "\n".join(lines)

####################################################
######################## CLI #######################
####################################################

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export heliophysics papers to BibTeX using official ADS entries.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--output",
        default="refs.bib",
        help="Output .bib filename. Defaults to refs.bib.",
    )
    parser.add_argument(
        "--keywords",
        help="Comma-separated keywords to filter by title or abstract. "
        "e.g. 'inertial modes,rossby waves'",
    )
    parser.add_argument(
        "--relevance",
        choices=["primary", "secondary", "peripheral"],
        help="Filter by Claude extraction relevance to solar inertial modes.",
    )
    parser.add_argument(
        "--start",
        help="Start date in YYYY-MM format. e.g. 2023-01",
    )
    parser.add_argument(
        "--end",
        help="End date in YYYY-MM format. e.g. 2026-05",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    keywords = (
        [k.strip() for k in args.keywords.split(",") if k.strip()]
        if args.keywords
        else None
    )

    print("\n BibTeX Exporter:")
    if keywords:
        print(f"  Keywords   : {', '.join(keywords)}")
    if args.relevance:
        print(f"  Relevance  : {args.relevance}")
    if args.start:
        print(f"  From       : {args.start}")
    if args.end:
        print(f"  To         : {args.end}")
    print(f"  Output     : {args.output}")
    if not ADS_TOKEN:
        print("  WARNING    : ADS_API_TOKEN not set; will use generated entries")
    print()

    try:
        conn = _get_connection()
    except Exception as e:
        print(f"ERROR: Could not connect to database: {e}")
        sys.exit(1)

    papers = _fetch_papers(
        conn,
        keywords=keywords,
        relevance=args.relevance,
        start_date=args.start,
        end_date=args.end,
    )

    if not papers:
        print("No papers found matching your filters.")
        conn.close()
        sys.exit(0)

    print(f"Found {len(papers)} papers. Fetching BibTeX from ADS...\n")

    entries = []
    ads_success = 0
    fallback_count = 0

    for i, paper in enumerate(papers, 1):
        bibcode = paper["identifier"]
        print(f"  [{i}/{len(papers)}] {bibcode}")

        bibtex = _fetch_bibtex_from_ads(bibcode)

        if bibtex:
            entries.append(bibtex)
            ads_success += 1
        else:
            print("using generated fallback entry")
            entries.append(_generate_bibtex_entry(dict(paper)))
            fallback_count += 1

        # Respect ADS rate limit
        time.sleep(0.25)

    # Write .bib file
    header = f"""% Generated by Heliophysics Paper API
% Date       : {datetime.now().strftime("%Y-%m-%d %H:%M")}
% Papers     : {len(papers)}
% ADS fetch  : {ads_success} succeeded, {fallback_count} used fallback
% Filters    : keywords={args.keywords or "none"} | relevance={args.relevance or "none"} | start={args.start or "none"} | end={args.end or "none"}

"""

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n\n".join(entries))
        f.write("\n")

    conn.close()

    print("\nExport complete:")
    print(f"  Papers exported : {len(papers)}")
    print(f"  From ADS        : {ads_success}")
    print(f"  Fallback        : {fallback_count}")
    print(f"  Output file     : {args.output}")
    print("\n")


if __name__ == "__main__":
    main()
