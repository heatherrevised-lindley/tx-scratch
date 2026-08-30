"""Texas scratch-off tracker."""

import threading

from flask import Flask, jsonify, redirect, render_template, request, url_for

import db
import metrics
import refresh as refresh_mod

app = Flask(__name__)

_refresh_lock = threading.Lock()
_refresh_state = {"running": False, "last": None}

SORTS = {
    "combined": ("rank_combined", False),
    "big": ("big_index", True),
    "ev": ("roi_now", True),
    "sold": ("pct_sold", True),
    "top_left": ("top_left", True),
    "top_prize": ("top_prize", True),
    "value_left": ("value_left", True),
    "price": ("price", True),
}


def _snapshot_rows(conn, snapshot_date, big_threshold):
    games = db.load_games(conn)
    snap = db.load_snapshot(conn, snapshot_date)
    return metrics.rank_all(games, snap, big_threshold)


@app.route("/")
def index():
    conn = db.connect()
    db.init(conn)

    latest = db.latest_snapshot_date(conn)
    if not latest:
        conn.close()
        return render_template("empty.html", running=_refresh_state["running"])

    big_threshold = float(request.args.get("big", 1000))
    min_sold = float(request.args.get("min_sold", 0)) / 100.0
    price_filter = request.args.get("price", "")
    include_closed = request.args.get("closed") == "1"
    sort_key = request.args.get("sort", "combined")
    only_top = request.args.get("top_left") == "1"

    rows = _snapshot_rows(conn, latest, big_threshold)
    games = db.load_games(conn)

    shown = []
    for r in rows:
        g = games.get(r["game_number"], {})
        if not include_closed and not g.get("active", 1):
            continue
        if r["pct_sold"] < min_sold:
            continue
        if price_filter and float(price_filter) != r["price"]:
            continue
        if only_top and not r["top_left"]:
            continue
        shown.append(r)

    col, desc = SORTS.get(sort_key, SORTS["combined"])
    shown.sort(key=lambda r: (r.get(col) is None, r.get(col) if r.get(col) is not None else 0),
               reverse=desc)
    if not desc:
        shown = [r for r in shown if r.get(col) is not None] + \
                [r for r in shown if r.get(col) is None]

    prices = sorted({r["price"] for r in rows if r["price"]})
    history_dates = db.snapshot_dates(conn)
    conn.close()

    return render_template(
        "index.html",
        rows=shown,
        total=len(rows),
        snapshot_date=latest,
        snapshot_count=len(history_dates),
        big_threshold=int(big_threshold),
        min_sold=int(min_sold * 100),
        price_filter=price_filter,
        include_closed=include_closed,
        only_top=only_top,
        sort_key=sort_key,
        prices=prices,
        running=_refresh_state["running"],
    )


@app.route("/game/<int:game_number>")
def game_detail(game_number):
    conn = db.connect()
    latest = db.latest_snapshot_date(conn)
    big_threshold = float(request.args.get("big", 1000))

    games = db.load_games(conn)
    game = games.get(game_number)
    if not game or not latest:
        conn.close()
        return redirect(url_for("index"))

    snap = db.load_snapshot_filtered(conn, latest, game_number).get(game_number)
    m = metrics.game_metrics(game, snap, big_threshold) if snap else None

    tiers = []
    if snap:
        for amt in sorted(snap["levels"], reverse=True):
            printed, claimed = snap["levels"][amt]
            tiers.append({
                "amount": amt,
                "printed": printed,
                "claimed": claimed,
                "left": printed - claimed,
                "pct_left": (printed - claimed) / printed if printed else 0,
                "value_left": amt * (printed - claimed),
            })

    conn.close()
    return render_template("game.html", game=game, m=m, tiers=tiers,
                           snapshot_date=latest, big_threshold=int(big_threshold))


@app.route("/api/history/<int:game_number>")
def api_history(game_number):
    """Metric history for one game, one point per stored snapshot."""
    conn = db.connect()
    big_threshold = float(request.args.get("big", 1000))
    games = db.load_games(conn)
    game = games.get(game_number)
    if not game:
        conn.close()
        return jsonify({"error": "unknown game"}), 404

    series = []
    for d in db.snapshot_dates(conn, game_number):
        snap = db.load_snapshot_filtered(conn, d, game_number).get(game_number)
        m = metrics.game_metrics(game, snap, big_threshold)
        if not m:
            continue
        series.append({
            "date": d,
            "pct_sold": round(m["pct_sold"] * 100, 2),
            "roi_now": round(m["roi_now"] * 100, 2) if m["roi_now"] is not None else None,
            "big_index": round(m["big_index"], 3) if m["big_index"] is not None else None,
            "top_left": m["top_left"],
            "value_left": m["value_left"],
        })
    conn.close()
    return jsonify({"game_number": game_number, "name": game["game_name"], "series": series})


@app.route("/api/games")
def api_games():
    conn = db.connect()
    latest = db.latest_snapshot_date(conn)
    big_threshold = float(request.args.get("big", 1000))
    rows = _snapshot_rows(conn, latest, big_threshold) if latest else []
    conn.close()
    return jsonify({"snapshot_date": latest, "games": rows})


@app.route("/refresh", methods=["POST"])
def do_refresh():
    def worker():
        with _refresh_lock:
            _refresh_state["running"] = True
            try:
                _refresh_state["last"] = refresh_mod.run(verbose=False)
            except Exception as exc:  # noqa: BLE001
                _refresh_state["last"] = {"error": str(exc)}
            finally:
                _refresh_state["running"] = False

    if not _refresh_state["running"]:
        threading.Thread(target=worker, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/refresh/status")
def refresh_status():
    return jsonify(_refresh_state)


@app.template_filter("pct")
def fmt_pct(v, digits=1):
    return "—" if v is None else f"{v * 100:.{digits}f}%"


@app.template_filter("money")
def fmt_money(v):
    if v is None:
        return "—"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:,.0f}K"
    return f"${v:,.0f}"


@app.template_filter("num")
def fmt_num(v):
    return "—" if v is None else f"{v:,.0f}"


@app.template_filter("factor")
def fmt_factor(v):
    return "—" if v is None else f"{v:.2f}x"


@app.template_filter("odds")
def fmt_odds(v):
    return "—" if v is None else f"1 in {v:,.0f}"


if __name__ == "__main__":
    conn = db.connect()
    db.init(conn)
    conn.close()
    app.run(debug=True, port=5000)
