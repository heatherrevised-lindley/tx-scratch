"""Offline check of the parser, metrics, and routes using a fixed CSV sample.

    python selftest.py

Writes to selftest.db so your real scratch.db is untouched.
"""

import os
import sys

os.environ["TXSCRATCH_DB"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selftest.db")
if os.path.exists(os.environ["TXSCRATCH_DB"]):
    os.remove(os.environ["TXSCRATCH_DB"])

import db  # noqa: E402
import metrics  # noqa: E402
import scraper  # noqa: E402

SAMPLE = '''"Scratch-Off Prizes as of 08/29/2026"
"Game Number","Game Name","Game Close Date","Ticket Price","Prize Level","Total Prizes in Level","Prizes Claimed"
2589,"500X","09/16/2026",50,"50","608239",537108
2589,"500X","09/16/2026",50,"100","760256",672981
2589,"500X","09/16/2026",50,"1000","509",440
2589,"500X","09/16/2026",50,"25000","46",39
2589,"500X","09/16/2026",50,"1000000","15",13
2589,"500X","09/16/2026",50,"TOTAL","1808109",1600120
2512,"Premier Winnings",,100,"150","764703",22291
2512,"Premier Winnings",,100,"1000","25787",581
2512,"Premier Winnings",,100,"100000","25",
2512,"Premier Winnings",,100,"10000000","4",
2512,"Premier Winnings",,100,"TOTAL","2626569",78225
2610,"Ultimate Millions",,50,"75","339468",120330
2610,"Ultimate Millions",,50,"2000","228",77
2610,"Ultimate Millions",,50,"25000","10",3
2610,"Ultimate Millions",,50,"1000000","4",1
2610,"Ultimate Millions",,50,"TOTAL","1262019",450658
'''

TICKET_COUNTS = {2589: 6082060, 2512: 8000000, 2610: 4000000}
ODDS = {2589: 3.36, 2512: 3.05, 2610: 3.17}


def main():
    import csv as _csv
    import io

    # Parser: reuse the real function against the sample by monkeypatching the fetch.
    scraper._get = lambda url, **kw: type("R", (), {"text": SAMPLE})()
    as_of, rows, meta = scraper.fetch_prize_csv()

    assert as_of == "2026-08-29", as_of
    assert len(meta) == 3, meta
    assert meta[2589]["ticket_price"] == 50
    assert meta[2589]["close_date"] == "09/16/2026"
    assert meta[2512]["close_date"] is None
    totals = [r for r in rows if r[1] == -1]
    assert len(totals) == 3
    # Blank claim counts must land as zero, not crash.
    assert (2512, 10000000.0, 4, 0) in rows
    print(f"parser ok: {len(rows)} rows, as of {as_of}")

    conn = db.connect()
    db.init(conn)
    for gnum, m in meta.items():
        db.upsert_game(conn, {**m, "total_tickets": TICKET_COUNTS[gnum],
                              "overall_odds": ODDS[gnum], "active": 1,
                              "start_date": "2025-01-01"}, as_of)
    db.write_snapshot(conn, as_of, rows)
    conn.commit()

    # A second snapshot with more claims, to exercise history.
    later = "2026-08-30"
    bumped = [(g, lvl, p, min(p, int(c * 1.05) + 1)) for (g, lvl, p, c) in rows]
    db.write_snapshot(conn, later, bumped)
    conn.commit()

    # upsert must not blank out a ticket count on a later pass that lacks one.
    db.upsert_game(conn, {"game_number": 2589, "game_name": "500X"}, later)
    conn.commit()
    assert db.load_games(conn)[2589]["total_tickets"] == 6082060
    print("upsert preserves cached ticket counts")

    games = db.load_games(conn)
    snap = db.load_snapshot(conn, as_of)
    ranked = metrics.rank_all(games, snap, big_threshold=1000)
    assert len(ranked) == 3

    by_num = {m["game_number"]: m for m in ranked}

    m500 = by_num[2589]
    assert abs(m500["pct_sold"] - 1600120 / 1808109) < 1e-9
    assert m500["top_prize"] == 1_000_000
    assert m500["top_left"] == 2
    assert m500["roi_now"] < 0, "no scratch-off should show a positive return"
    assert m500["big_index"] is not None

    prem = by_num[2512]
    assert prem["top_left"] == 4, "unclaimed top prizes with a blank claim cell"
    assert prem["pct_sold"] < 0.05, "brand new game should read as barely sold"
    assert prem["big_index"] > 1.0, "a fresh game keeps its big prizes ahead of sales"

    for m in ranked:
        assert m["rank_big"] and m["rank_ev"] and m["rank_overall"]
    print("metrics ok: " + ", ".join(
        f"{m['game_name']} sold {m['pct_sold']:.0%} roi {m['roi_now']:.1%} idx {m['big_index']:.2f}x"
        for m in sorted(ranked, key=lambda x: x["rank_overall"])))

    # Zero-ticket-count game must degrade instead of dividing by zero.
    db.upsert_game(conn, {"game_number": 2610, "total_tickets": None}, as_of)
    conn.commit()
    g = dict(db.load_games(conn)[2610])
    g["total_tickets"] = None
    degraded = metrics.game_metrics(g, snap[2610])
    assert degraded is not None and degraded["roi_now"] is None
    print("missing ticket count degrades cleanly")
    conn.close()

    import app as app_mod
    client = app_mod.app.test_client()

    r = client.get("/")
    assert r.status_code == 200 and b"Texas scratch-offs" in r.data
    assert b"500X" in r.data
    print("dashboard renders")

    for qs in ("?min_sold=80", "?big=100000", "?price=50", "?sort=ev", "?sort=sold",
               "?top_left=1", "?closed=1", "?min_sold=90&big=50000&sort=big"):
        rr = client.get("/" + qs)
        assert rr.status_code == 200, (qs, rr.status_code)
    print("filters and sorts render")

    r = client.get("/game/2589")
    assert r.status_code == 200 and b"Prize tiers" in r.data
    print("game page renders")

    h = client.get("/api/history/2589").get_json()
    assert len(h["series"]) == 2, h
    assert h["series"][0]["pct_sold"] < h["series"][1]["pct_sold"], "claims should rise over time"
    print(f"history api ok: {len(h['series'])} points")

    j = client.get("/api/games").get_json()
    assert j["snapshot_date"] and len(j["games"]) == 3
    print("games api ok")

    assert client.get("/game/9999").status_code == 302
    print("unknown game redirects")

    calibration()
    build_check()
    print("\nAll checks passed.")


def calibration():
    """Check the EV model against game 2589 (500X), whose full tier list and
    published ticket count and overall odds are all known."""
    levels = {50: (608239, 537108), 100: (760256, 672981), 200: (304103, 269777),
              300: (73944, 65646), 500: (60843, 53979), 1000: (509, 440),
              5000: (154, 137), 25000: (46, 39), 1000000: (15, 13)}
    snap = {"total": (1808109, 1600120), "levels": {float(k): v for k, v in levels.items()}}
    game = {"game_number": 2589, "game_name": "500X", "ticket_price": 50.0,
            "total_tickets": 6082060, "overall_odds": 3.36}
    m = metrics.game_metrics(game, snap, 1000)

    implied_odds = game["total_tickets"] / snap["total"][0]
    assert abs(implied_odds - game["overall_odds"]) < 0.01, implied_odds

    # Texas scratch games pay back roughly 60-80 cents on the dollar.
    assert -0.45 < m["roi_start"] < -0.15, m["roi_start"]
    assert -0.60 < m["roi_now"] < 0.0, m["roi_now"]
    assert 0.85 < m["pct_sold"] < 0.90, m["pct_sold"]
    assert m["top_left"] == 2 and m["top_prize"] == 1_000_000

    print(f"calibration ok: implied odds 1 in {implied_odds:.2f} vs published "
          f"1 in {game['overall_odds']}, launch ROI {m['roi_start']:+.1%}, "
          f"remaining ROI {m['roi_now']:+.1%}")




def build_check():
    """Build the phone app and confirm it computes the same numbers as Python.

    If node is present the page's own compute/rankAll run against the baked data
    and every value is compared. Without node, the build itself is still checked.
    """
    import json
    import shutil
    import subprocess
    import tempfile

    import build_static

    out = tempfile.mkdtemp(prefix="txbuild")
    try:
        payload = build_static.build(out, history_points=10)
        idx = os.path.join(out, "index.html")
        html = open(idx, encoding="utf-8").read()

        assert "/*__DATA__*/null" not in html, "data placeholder was not replaced"
        for name in ("manifest.webmanifest", "sw.js", "icon-192.png", "icon-512.png"):
            assert os.path.exists(os.path.join(out, name)), name
        assert payload["games"], "no games in the build"
        assert all(g["t"] for g in payload["games"]), "a game shipped without a ticket count"
        print(f"build ok: {len(payload['games'])} games, "
              f"{os.path.getsize(idx) / 1024:.0f} KB, icons and worker present")

        if not shutil.which("node"):
            print("node not found, skipping the JavaScript parity check")
            return

        script = r"""
const fs=require('fs');
const html=fs.readFileSync(process.argv[2],'utf8');
const data=JSON.parse(html.match(/const DATA = (\{.*?\});\n/s)[1]);
eval(html.match(/function compute\(g, thr\)\{[\s\S]*?\n\}/)[0]+'\n'+
     html.match(/function rankAll\(rows\)\{[\s\S]*?\n\}/)[0]);
const out={};
for(const thr of [500,1000,10000]){
  const rows=data.games.map(g=>({g,m:compute(g,thr)}));
  rankAll(rows);
  out[thr]=rows.map(r=>({n:r.g.n,sold:r.m.soldPct,roi:r.m.roiNow,big:r.m.bigIndex,
    left:r.m.left,val:r.m.valueLeft,topLeft:r.m.topLeft,overall:r.overall??null}));
}
process.stdout.write(JSON.stringify(out));
"""
        sp = os.path.join(out, "_check.js")
        open(sp, "w").write(script)
        raw = subprocess.run(["node", sp, idx], capture_output=True, text=True, timeout=90)
        assert raw.returncode == 0, raw.stderr[:400]
        js = json.loads(raw.stdout)

        conn = db.connect()
        games, snap = db.load_games(conn), db.load_snapshot(conn, db.latest_snapshot_date(conn))
        compared = 0
        for thr_s, rows in js.items():
            py = {m["game_number"]: m for m in metrics.rank_all(games, snap, float(thr_s))}
            for r in rows:
                p = py[r["n"]]
                for jk, pk in (("sold", "pct_sold"), ("roi", "roi_now"), ("big", "big_index"),
                               ("left", "tickets_left"), ("val", "value_left"),
                               ("topLeft", "top_left")):
                    a, b = r[jk], p[pk]
                    if a is None and b is None:
                        continue
                    assert a is not None and b is not None, (thr_s, r["n"], jk, a, b)
                    assert abs(a - b) / max(abs(b), 1e-9) < 1e-9, (thr_s, r["n"], jk, a, b)
                    compared += 1
                assert r["overall"] == p.get("rank_overall"), (thr_s, r["n"], "rank")
        conn.close()
        print(f"javascript parity ok: {compared} values and every rank match "
              f"across 3 thresholds")
    finally:
        shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
