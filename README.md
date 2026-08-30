# Texas scratch-off tracker

Finds Texas Lottery scratch games that are deep into their print run but still holding
big unclaimed prizes. Stores a snapshot every time you pull, so you can watch a game's
position move over weeks.

## Setup

```
pip install -r requirements.txt
python refresh.py        # first pull
python app.py            # http://localhost:5000
```

The first pull is slow. It grabs the prize file, then one detail page per game to read
the printed ticket count. Those counts never change, so they get cached and every later
pull is just the one CSV, a few seconds. Use `python refresh.py --details` if you ever
want to re-scrape them.

`python selftest.py` runs the parser, metric, and route checks offline against a fixed
sample. It writes to `selftest.db` and leaves your real data alone.

## Daily snapshots

The lottery updates its file once a day. A cron entry keeps the history filling in:

```
30 7 * * * cd /path/to/tx-scratch && /usr/bin/python3 refresh.py >> refresh.log 2>&1
```

Snapshots are keyed on the lottery's own "as of" date, so running twice in a day
overwrites rather than duplicates.

## Putting it on your phone

The phone version is a static build: the day's data is baked into one HTML file
that installs to your home screen and works with no signal. This is not just a
convenience. A browser cannot fetch the lottery's CSV directly (the site sends no
CORS headers), so the data has to be baked in at build time regardless.

```
python build_static.py          # writes ./dist
```

`dist/` holds `index.html` with the data inline, a manifest, a service worker, and
icons. Roughly 250 KB with a full slate of games.

### Automatic, via GitHub Pages

This is the setup worth doing. Push the project to a GitHub repo, including
`scratch.db` so your history carries over between runs, then turn on Pages under
Settings, Pages, Source: GitHub Actions.

`.github/workflows/daily.yml` then runs every morning: it pulls the new file,
appends a snapshot, commits the database back, and publishes the site. Open the
Pages URL on your phone once, then Chrome menu, "Add to Home Screen." After that
it opens like any other app and the data refreshes in the background.

Pages on a private repo needs a paid GitHub plan. A public repo works on the free
tier, and there is nothing personal in this one.

### Manual, no hosting

Run `build_static.py`, copy `dist/` to your phone, and open `index.html`. It works,
but Android only grants the home-screen icon and offline caching over HTTPS, so
from local storage you get a bookmark rather than an installed app.

### What the phone build shows

Search by game name or number, since you are usually standing in front of the
ticket display. Price chips, a minimum-sold filter, and the same big-prize
threshold as the desktop app. Tap a card for the full tier table and a sparkline
of that game's history.

The header shows the date the claims data came from and how old it is, turning
amber past three days. Since the whole point is a page cached on your phone, that
counter is how you know whether you are looking at something stale.

The phone build and the Flask app compute identical numbers. `selftest.py` runs
both implementations over the same data and compares every value and rank.

## Where the numbers come from

`texaslottery.com/.../Scratch_Offs/scratchoff.csv` lists every prize tier of every game
with prizes printed and prizes claimed. Detail pages give the total tickets printed.
That is the whole input.

The lottery does not publish tickets sold, so percent sold is estimated:

```
pct_sold     = prizes claimed / prizes printed
tickets_left = total tickets * (1 - pct_sold)
```

Prizes are distributed evenly across a print run by design, so this holds up reasonably
well in aggregate. Two things to keep in mind when reading it:

- **Claims lag sales.** Winners sit on tickets, and small prizes often go unclaimed
  entirely. Percent sold reads low and tickets left reads high, especially on games
  near the end of their run.
- **Packs are not evenly distributed.** The even spread is across the whole print run,
  not across any one store. There is no public data on which packs shipped where.

## The two rankings

**Big-prize index.** How densely prizes at or above your threshold sit in the tickets
still on shelves, divided by how densely they sat at launch. Above 1.00x means the big
prizes are outrunning sales, which is the exact thing you are looking for. Adjustable
from $100 to $100,000 in the toolbar.

**Remaining ROI.** Total value of all unclaimed prizes divided by estimated tickets left,
compared against the ticket price. The detail page shows this next to the launch ROI so
you can see whether a game has drifted better or worse.

Sorting on Rank uses the average of the two ranks. Set a minimum percent sold to skip
fresh games, which always look good on the index simply because nothing has been won yet.

## One thing the tool cannot fix

Every Texas scratch game is designed to pay back roughly 60 to 80 cents per dollar, and
that number never crosses 1.00. A game at 1.30x on the big-prize index is better than it
used to be, not better than break-even. The screen is for picking among games you were
going to buy anyway.

## Files

| File | Purpose |
|---|---|
| `refresh.py` | Pull data, write a snapshot. Cron this. |
| `scraper.py` | CSV and detail-page fetching |
| `db.py` | SQLite schema and storage |
| `metrics.py` | Percent sold, EV, big-prize index |
| `app.py` | Flask routes and template filters |
| `build_static.py` | Bake the database into the installable phone app |
| `static/mobile.html` | Phone app shell; data gets injected at build time |
| `.github/workflows/daily.yml` | Daily pull, commit, and Pages deploy |
| `selftest.py` | Offline checks, including a calibration test against a real game |

## API

- `GET /api/games?big=1000` — every game with current metrics
- `GET /api/history/<game_number>?big=1000` — one point per stored snapshot
