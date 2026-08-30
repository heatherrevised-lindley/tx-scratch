"""Pull today's data and store it as a snapshot.

    python refresh.py              # normal daily run
    python refresh.py --details    # also re-scrape detail pages for every game
"""

import argparse
import sys
import time
from datetime import datetime

import db
import scraper


def run(force_details=False, detail_delay=0.7, verbose=True):
    def say(msg):
        if verbose:
            print(msg, flush=True)

    conn = db.connect()
    db.init(conn)

    say("Fetching prize CSV...")
    as_of, rows, csv_meta = scraper.fetch_prize_csv()
    say(f"  {len(rows)} prize rows, {len(csv_meta)} games, dated {as_of}")

    say("Fetching current games list...")
    try:
        index = scraper.fetch_game_index()
        say(f"  {len(index)} active games")
    except Exception as exc:  # noqa: BLE001
        say(f"  games list failed ({exc}); keeping previously stored game info")
        index = {}

    known = db.load_games(conn)

    # Merge CSV metadata with the index. Games in the CSV but not in the
    # current list are closed or closing out, so they get flagged inactive.
    for gnum, meta in csv_meta.items():
        record = dict(meta)
        if gnum in index:
            record.update({k: v for k, v in index[gnum].items() if v is not None})
        elif index:
            record["active"] = 0
        db.upsert_game(conn, record, as_of)
    conn.commit()

    # Detail pages give total tickets printed. Static per game, so fetch once.
    needs_details = []
    for gnum in csv_meta:
        row = known.get(gnum, {})
        has_tickets = row.get("total_tickets")
        url = (index.get(gnum) or {}).get("details_url") or row.get("details_url")
        if url and (force_details or not has_tickets):
            needs_details.append((gnum, url))

    if needs_details:
        say(f"Fetching {len(needs_details)} detail pages for ticket counts...")
        for i, (gnum, url) in enumerate(needs_details, 1):
            try:
                info = scraper.fetch_game_details(url)
                if info["total_tickets"]:
                    db.upsert_game(conn, {"game_number": gnum, **info}, as_of)
                    say(f"  [{i}/{len(needs_details)}] {gnum}: {info['total_tickets']:,} tickets")
                else:
                    say(f"  [{i}/{len(needs_details)}] {gnum}: no ticket count found")
            except Exception as exc:  # noqa: BLE001
                say(f"  [{i}/{len(needs_details)}] {gnum}: {exc}")
            if i % 10 == 0:
                conn.commit()
            time.sleep(detail_delay)
        conn.commit()

    written = db.write_snapshot(conn, as_of, rows)
    db.log_refresh(conn, datetime.now().isoformat(timespec="seconds"), as_of,
                   written, len(csv_meta), "ok")
    conn.commit()

    missing = conn.execute(
        "SELECT COUNT(*) FROM games WHERE active = 1 AND total_tickets IS NULL"
    ).fetchone()[0]

    say(f"Snapshot {as_of} saved: {written} rows.")
    if missing:
        say(f"{missing} active games still missing a ticket count. "
            f"Run with --details to retry.")

    conn.close()
    return {"snapshot_date": as_of, "rows": written, "games": len(csv_meta), "missing": missing}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--details", action="store_true",
                    help="re-scrape every detail page, not just games missing a ticket count")
    ap.add_argument("--delay", type=float, default=0.7,
                    help="seconds between detail page requests (default 0.7)")
    args = ap.parse_args()
    try:
        run(force_details=args.details, detail_delay=args.delay)
    except Exception as exc:  # noqa: BLE001
        print(f"Refresh failed: {exc}", file=sys.stderr)
        sys.exit(1)
