# Extract structured metadata for all papers in the database.
# Calls POST /papers/{identifier}/extract for every paper.
# Already-extracted papers are returned from cache instantly.
# New extractions call Claude and are cached in Postgres.
#
# Usage:
#    python extract_all.py
#    python extract_all.py --limit 50

import argparse
import time
import httpx

base_url = "http://localhost:8000"


def main():
    parser = argparse.ArgumentParser(description="Extract metadata for all papers.")
    parser.add_argument("--limit", type=int, default=50, help="Max papers to process.")
    args = parser.parse_args()

    # Fetch all papers
    print("\n Fetching papers from database...")
    r = httpx.get(f"{base_url}/papers/", params={"limit": args.limit}, timeout=30)
    r.raise_for_status()
    papers = r.json().get("papers", [])
    print(f"   Found {len(papers)} papers\n")

    if not papers:
        print("No papers found. Run ingest.py first.")
        return

    success = 0
    cached = 0
    failed = 0
    skipped = 0

    for i, paper in enumerate(papers, 1):
        identifier = paper["identifier"]
        title = paper.get("title", "")[:60]
        print(f"  [{i}/{len(papers)}] {identifier}")
        print(f"           {title}...")

        if not paper.get("abstract"):
            print("skipped (no abstract)\n")
            skipped += 1
            continue

        try:
            res = httpx.post(
                f"{base_url}/papers/{identifier}/extract",
                timeout=60,
            )
            res.raise_for_status()
            data = res.json()

            relevance = data.get("relevance_to_solar_inertial_modes", "?")
            data_type = data.get("data_type", "?")
            was_cached = data.get("cached", False)

            status = "cached" if was_cached else "extracted"
            print(f"           → {status} | {data_type} | {relevance}\n")

            if was_cached:
                cached += 1
            else:
                success += 1
                # Respect Anthropic rate limit for new extractions
                time.sleep(1)

        except Exception as e:
            print(f"           → FAILED: {e}\n")
            failed += 1

    print("Complete:")
    print(f"  Newly extracted : {success}")
    print(f"  From cache      : {cached}")
    print(f"  Skipped         : {skipped}")
    print(f"  Failed          : {failed}")
    print(f"  Total           : {len(papers)}")
    print("\n")


if __name__ == "__main__":
    main()
