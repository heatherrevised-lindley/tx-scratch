"""Turns a raw snapshot into the numbers the dashboard ranks on.

The core estimate
-----------------
The lottery publishes prizes printed and prizes claimed, not tickets sold. If
prizes are spread evenly through the print run then the share of prizes claimed
approximates the share of tickets sold:

    pct_sold        = total_prizes_claimed / total_prizes_printed
    tickets_left    = total_tickets * (1 - pct_sold)

Two caveats that matter when you read the output:
  - Claims lag sales. Winners sit on tickets, and small prizes often go
    unclaimed entirely, so pct_sold reads low and tickets_left reads high.
  - Prize distribution is even across the print run by design, but individual
    packs are not, and there is no public data on which packs shipped where.
"""

BIG_PRIZE_DEFAULT = 1000.0


def _safe_div(a, b):
    return a / b if b else None


def game_metrics(game, snap, big_threshold=BIG_PRIZE_DEFAULT):
    """game: row from the games table. snap: {'total': (p,c), 'levels': {amt: (p,c)}}"""
    if not snap or not snap.get("total"):
        return None

    printed_all, claimed_all = snap["total"]
    if not printed_all:
        return None

    price = game.get("ticket_price") or 0
    total_tickets = game.get("total_tickets")
    levels = snap["levels"]
    if not levels:
        return None

    pct_sold = claimed_all / printed_all
    tickets_left = total_tickets * (1 - pct_sold) if total_tickets else None
    tickets_sold = total_tickets - tickets_left if total_tickets else None

    value_start = sum(amt * p for amt, (p, c) in levels.items())
    value_left = sum(amt * (p - c) for amt, (p, c) in levels.items())
    value_cashed = value_start - value_left

    prizes_left = printed_all - claimed_all

    top_prize = max(levels)
    top_printed, top_claimed = levels[top_prize]
    top_left = top_printed - top_claimed

    big_printed = sum(p for amt, (p, c) in levels.items() if amt >= big_threshold)
    big_left = sum(p - c for amt, (p, c) in levels.items() if amt >= big_threshold)

    m = {
        "game_number": game["game_number"],
        "game_name": game.get("game_name"),
        "price": price,
        "start_date": game.get("start_date"),
        "close_date": game.get("close_date"),
        "closing_soon": bool(game.get("closing_soon")),
        "total_tickets": total_tickets,
        "overall_odds": game.get("overall_odds"),
        "pct_sold": pct_sold,
        "tickets_sold": tickets_sold,
        "tickets_left": tickets_left,
        "prizes_printed": printed_all,
        "prizes_claimed": claimed_all,
        "prizes_left": prizes_left,
        "value_start": value_start,
        "value_left": value_left,
        "value_cashed": value_cashed,
        "pct_value_left": _safe_div(value_left, value_start),
        "top_prize": top_prize,
        "top_printed": top_printed,
        "top_left": top_left,
        "big_threshold": big_threshold,
        "big_printed": big_printed,
        "big_left": big_left,
    }

    # Expected value per ticket, now vs at launch.
    if total_tickets and tickets_left and tickets_left > 0:
        m["ev_now"] = value_left / tickets_left
        m["ev_start"] = value_start / total_tickets
        m["roi_now"] = (m["ev_now"] / price - 1) if price else None
        m["roi_start"] = (m["ev_start"] / price - 1) if price else None
        m["roi_delta"] = (m["roi_now"] - m["roi_start"]) if price else None

        # Top prize odds. Lower is better, so the factor inverts them:
        # >1 means the top prize is easier to hit now than on day one.
        m["top_odds_start"] = _safe_div(total_tickets, top_printed)
        m["top_odds_now"] = _safe_div(tickets_left, top_left) if top_left else None
        m["top_odds_factor"] = (
            _safe_div(m["top_odds_start"], m["top_odds_now"]) if m["top_odds_now"] else None
        )

        # Big-prize index: density of prizes at or above the threshold in the
        # tickets still out there, relative to the density at launch.
        rate_start = _safe_div(big_printed, total_tickets)
        rate_now = _safe_div(big_left, tickets_left)
        m["big_rate_start"] = rate_start
        m["big_rate_now"] = rate_now
        m["big_index"] = _safe_div(rate_now, rate_start) if rate_start else None
    else:
        for k in ("ev_now", "ev_start", "roi_now", "roi_start", "roi_delta",
                  "top_odds_start", "top_odds_now", "top_odds_factor",
                  "big_rate_start", "big_rate_now", "big_index"):
            m[k] = None

    return m


def rank_all(games, snapshot, big_threshold=BIG_PRIZE_DEFAULT):
    """Compute metrics for every game and attach the two rankings side by side."""
    out = []
    for gnum, snap in snapshot.items():
        game = games.get(gnum)
        if not game:
            continue
        m = game_metrics(game, snap, big_threshold)
        if m:
            out.append(m)

    _attach_rank(out, "big_index", "rank_big", reverse=True)
    _attach_rank(out, "roi_now", "rank_ev", reverse=True)

    for m in out:
        rb, re_ = m.get("rank_big"), m.get("rank_ev")
        m["rank_combined"] = (rb + re_) / 2 if rb and re_ else None

    ranked = [m for m in out if m["rank_combined"]]
    # Ties broken on big index, then game number, so the web app and the phone
    # build always agree on ordering.
    ranked.sort(key=lambda m: (m["rank_combined"], -(m["big_index"] or 0), m["game_number"]))
    for i, m in enumerate(ranked, 1):
        m["rank_overall"] = i

    return out


def _attach_rank(rows, key, rank_key, reverse=True):
    scored = [r for r in rows if r.get(key) is not None]
    scored.sort(key=lambda r: r[key], reverse=reverse)
    for i, r in enumerate(scored, 1):
        r[rank_key] = i
    for r in rows:
        r.setdefault(rank_key, None)
