"""Bake the database into a self-contained page you can install on a phone.

    python build_static.py            # writes ./dist
    python build_static.py -o /tmp/x  # somewhere else

Output is one HTML file with the data inline, plus a manifest, a service worker,
and two icons. Nothing loads from a network at runtime, so it works in a store
with no signal.
"""

import argparse
import json
import os
import shutil
from datetime import datetime

import db
import metrics

HISTORY_POINTS = 60
DEFAULT_BIG = 1000.0


def _r(v, digits=4):
    return None if v is None else round(v, digits)


def collect(conn, history_points=HISTORY_POINTS):
    latest = db.latest_snapshot_date(conn)
    if not latest:
        raise SystemExit("No snapshots in the database. Run refresh.py first.")

    games = db.load_games(conn)
    snap = db.load_snapshot(conn, latest)

    all_dates = db.snapshot_dates(conn)
    keep = all_dates[-history_points:]
    hist_snaps = {d: db.load_snapshot(conn, d) for d in keep}

    out = []
    for gnum, s in snap.items():
        game = games.get(gnum)
        if not game or not s.get("total"):
            continue
        m = metrics.game_metrics(game, s, DEFAULT_BIG)
        if not m or not game.get("total_tickets"):
            continue

        tiers = sorted(s["levels"].items(), key=lambda kv: kv[0], reverse=True)

        hd, hs, hr, hb = [], [], [], []
        for d in keep:
            gs = hist_snaps[d].get(gnum)
            if not gs:
                continue
            hm = metrics.game_metrics(game, gs, DEFAULT_BIG)
            if not hm or hm["roi_now"] is None:
                continue
            hd.append(d[5:])  # MM-DD is enough on a phone
            hs.append(round(hm["pct_sold"] * 100, 1))
            hr.append(round(hm["roi_now"] * 100, 1))
            hb.append(round(hm["big_index"], 3) if hm["big_index"] is not None else None)

        out.append({
            "n": gnum,
            "g": game.get("game_name") or f"Game {gnum}",
            "p": game.get("ticket_price") or 0,
            "t": game["total_tickets"],
            "o": _r(game.get("overall_odds"), 2),
            "s": game.get("start_date"),
            "c": game.get("close_date"),
            "x": 1 if game.get("closing_soon") else 0,
            "a": 1 if game.get("active", 1) else 0,
            "T": list(s["total"]),
            "L": [[amt, p, c] for amt, (p, c) in tiers],
            "H": {"d": hd, "s": hs, "r": hr, "b": hb} if hd else None,
        })

    out.sort(key=lambda g: g["g"].lower())
    return {
        "as_of": latest,
        "built": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "snapshots": len(all_dates),
        "games": out,
    }


def build(outdir="dist", history_points=HISTORY_POINTS):
    conn = db.connect()
    db.init(conn)
    payload = collect(conn, history_points)
    conn.close()

    os.makedirs(outdir, exist_ok=True)
    here = os.path.dirname(os.path.abspath(__file__))

    shell = open(os.path.join(here, "static", "mobile.html"), encoding="utf-8").read()
    data_json = json.dumps(payload, separators=(",", ":"))
    html = shell.replace("/*__DATA__*/null", data_json)

    with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    manifest = {
        "name": "Texas Scratch-Offs",
        "short_name": "Scratch",
        "start_url": "./index.html",
        "scope": "./",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#e3e1dc",
        "theme_color": "#16181d",
        "icons": [
            {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    with open(os.path.join(outdir, "manifest.webmanifest"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    # Cache version keyed to the data date, so a new build supersedes the old one.
    sw = f"""const CACHE = 'txscratch-{payload['as_of']}-{payload['built'].replace(' ', '_')}';
const ASSETS = ['./', './index.html', './manifest.webmanifest', './icon-192.png', './icon-512.png'];
self.addEventListener('install', e => {{
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
}});
self.addEventListener('activate', e => {{
  e.waitUntil(caches.keys()
    .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
}});
self.addEventListener('fetch', e => {{
  if (e.request.method !== 'GET') return;
  // Network first so a fresh build lands, cache as the fallback in dead zones.
  e.respondWith(
    fetch(e.request)
      .then(r => {{
        const copy = r.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy)).catch(() => {{}});
        return r;
      }})
      .catch(() => caches.match(e.request).then(m => m || caches.match('./index.html')))
  );
}});
"""
    with open(os.path.join(outdir, "sw.js"), "w", encoding="utf-8") as f:
        f.write(sw)

    make_icons(outdir)

    size = os.path.getsize(os.path.join(outdir, "index.html"))
    print(f"Built {outdir}/ from snapshot {payload['as_of']}")
    print(f"  {len(payload['games'])} games, index.html {size / 1024:.0f} KB")
    return payload


def make_icons(outdir):
    """A scratch panel with one silver cell rubbed off. Drawn, not fetched, so
    the build has no asset dependencies."""
    from PIL import Image, ImageDraw

    for size in (192, 512):
        u = size / 192.0
        img = Image.new("RGB", (size, size), "#16181d")
        d = ImageDraw.Draw(img)
        pad, gap = 30 * u, 8 * u
        cell = (size - 2 * pad - 2 * gap) / 3

        for row in range(3):
            for col in range(3):
                x0 = pad + col * (cell + gap)
                y0 = pad + row * (cell + gap)
                revealed = (row, col) == (1, 1)
                d.rounded_rectangle(
                    [x0, y0, x0 + cell, y0 + cell],
                    radius=6 * u,
                    fill="#b8862b" if revealed else "#a9adb2",
                )
                if revealed:
                    # A bar mark to read as a won cell at 48px.
                    bx, by = x0 + cell * 0.28, y0 + cell * 0.42
                    d.rounded_rectangle(
                        [bx, by, x0 + cell * 0.72, by + cell * 0.16],
                        radius=3 * u, fill="#16181d",
                    )
        img.save(os.path.join(outdir, f"icon-{size}.png"), "PNG", optimize=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="dist")
    ap.add_argument("--history", type=int, default=HISTORY_POINTS,
                    help=f"snapshots of history to embed (default {HISTORY_POINTS})")
    ap.add_argument("--clean", action="store_true", help="wipe the output directory first")
    a = ap.parse_args()
    if a.clean and os.path.isdir(a.out):
        shutil.rmtree(a.out)
    build(a.out, a.history)
