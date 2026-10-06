# Repository Analysis Tool (RAT)

A Flask web application that analyzes Git repositories and turns their history
into churn, growth, volatility, ownership, and coupling metrics — rendered as
hand-built SVG visualisations with no external chart library.

You can analyze any repository in two ways: clone it from a remote URL, or
upload a ZIP archive that contains a `.git` directory. Analysis runs in
background threads, so the web interface stays responsive while history is
indexed.

## Requirements

| Dependency | Version | Why |
| --- | --- | --- |
| Python | 3.10+ | The code uses `X \| Y` type unions and `from __future__ import annotations` |
| git CLI | any recent | Cloning and reading history (`git log --numstat`) via subprocess |
| Flask | 3.0–3.x | Only Python dependency, installed from `requirements.txt` |

Network access is only needed when cloning a remote repository. ZIP uploads
work fully offline.

## Clone and run (step by step)

```bash
# 1. Clone the project
git clone https://github.com/SauravLall07/SDP-Test.git
cd SDP-Test

# 2. Create and activate a virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate          # Linux / macOS
# .venv\Scripts\activate           # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Start the development server
python3 app.py
```

Then open **http://127.0.0.1:5000** in your browser.

### Using the app

1. Click **Add repository**.
2. Either paste a clone URL (e.g. `https://github.com/psf/requests.git`) or
   upload a ZIP that contains a `.git` directory.
3. The list shows the status: `queued` → `analyzing` → `ready` (the page also
   polls automatically while a job runs).
4. Once ready, explore the dashboard. Clicking a table row, a scatter point,
   a heatmap cell, or a coupling row focuses that file or directory and
   recomputes every view for the selected scope.

### Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `5000` | HTTP port, e.g. `PORT=8000 python3 app.py` |
| `RAT_DATA_DIR` | `./data` | Directory for the SQLite database and cloned repositories |

### Run the tests

```bash
python3 -m unittest discover tests
```

The suite builds temporary Git repositories with real commits (including
renames, binary files, and `.mailmap` identity merging) and verifies the
metric formulas, the API endpoints, and the pure algorithms.

## Architecture

Metric computation is split into five layers, each with a single
responsibility:

```
Git repository
      │  git log --numstat -z (streamed)
      ▼
rat/git_analyzer.py ── streaming parser, constant memory
      │  normalized rows
      ▼
data/rat.sqlite3 ───── normalized schema + indexes (rat/db.py)
      │  SQL aggregation (GROUP BY, self-joins)
      ▼
rat/metrics.py ─────── pure distribution algorithms (unit tested)
      │  JSON
      ▼
app.py ─────────────── HTTP endpoints, shared filter builder
      │  fetch()
      ▼
static/app.js ──────── custom SVG chart renderer
```

Design decisions behind this layout:

- **Streaming analysis.** `git log` output is parsed incrementally
  (1 MiB chunks) into SQLite, so memory stays constant regardless of
  repository size. Directory rollups are aggregated in memory per commit and
  written once, which keeps one commit at O(files + directories) work.
- **Set-level work stays in SQL, distribution-level work stays in Python.**
  Filtering, grouping, and summation run inside SQLite next to the data and
  use the schema indexes (`commits(repository_id, committer_date)`,
  `changes(path, kind)`, plus primary-key prefixes for commit-join lookups).
  Rankings, inequality, regression, and association live in `rat/metrics.py`
  as dependency-free functions that are trivially unit-testable.
- **One shared filter builder.** `commit_filter()` and `object_filter()` in
  `app.py` translate the query string into SQL clauses once, so every
  endpoint applies author / date / commit-set / path / kind semantics
  identically.
- **Bounded work everywhere.** Object listings are capped at 1000 rows, the
  coupling self-join is pruned to the top-12 churn files before pairing
  (see below), and dense timelines are coalesced in the renderer instead of
  shipping thousands of DOM nodes.
- **Background workers.** Clone/extract/analyze jobs run on a thread pool
  (`rat/services.py`); the API only ever reads the database.

## Metric algorithms

All algorithms live in `rat/metrics.py` unless noted; each docstring states
its complexity. `n` is the number of objects in the distribution.

| Metric | Idea | Complexity |
| --- | --- | --- |
| Directory rollups | Sum file changes into every parent directory per commit during analysis | O(f + d) per commit |
| Weekly / monthly timeline | Integer week buckets, Monday-anchored (`(day + 3) / 7`); month buckets via `strftime`; empty buckets are filled so series are continuous | O(rows) with the date index |
| Ownership Gini | Rank formula `2·Σ i·xᵢ / (n·Σ xᵢ) − (n+1)/n` after one ascending sort | O(n log n) |
| HHI (concentration) | Sum of squared shares, 1/n (even) → 1 (single actor) | O(n) |
| Bus factor | Descending sort, then prefix scan from the top until 50% of churn is covered | O(n log n) |
| Pareto coverage | Same ranking, thresholds at 50% / 80% ("3 of 24 files carry 80% of churn") | O(n log n) |
| Pareto curve | Cumulative share sampled into ≤ 64 knots for the chart payload | O(n log n) |
| Churn trend | Least-squares slope + R² over bucket churn using running sums, no intermediate arrays | O(n) time, O(1) memory |
| Co-change coupling | Per candidate pair: `confidence = P(b \| a)`, `lift = confidence / P(b)`, `support = P(a ∧ b)` (association-rule style) | O(1) per pair |

**Why co-change coupling is efficient.** The naive approach compares every
pair of files inside every commit (Σ k² work, catastrophic on commits that
touch hundreds of files). Instead the endpoint first ranks files by churn,
prunes the candidate set to the top 12, and only then runs a canonical
ordered SQL self-join (`a.path < b.path`, `HAVING together >= 2`) over those
candidates. Pairs with `lift < 1` (co-changing *less* than chance) are
discarded, so every reported pair is a genuine coupling signal.

**Hotspot scoring** combines two commit-normalized factors —
`churn_rate × modification_frequency` — so the ranking is comparable across
scopes and time ranges; ties break on raw churn.

## Visualisation

Everything is rendered with a small SVG helper (`svgRoot` / `svgNode` in
`static/app.js`): no chart library, no CDN dependency, fully offline, and
every mark carries a native tooltip.

| View | What it shows |
| --- | --- |
| Insight chips | Bus factor, ownership Gini (tooltip: HHI, top share), authors and files needed for 80% of churn, trend direction |
| Change timeline | Added (green, up) and removed (red, down) bars around a zero axis, cumulative net growth line on the right scale, and the least-squares churn trend as a dashed gold line. Weekly / monthly toggle; histories longer than ~180 buckets are coalesced automatically |
| Volatility hotspots | Churn vs modification-frequency scatter with median crosshairs, green = net growth, red = net deletion; click a point to focus the file |
| Co-change coupling | Heatmap of shared commits among the top churn files (darker = stronger), with a top-pairs list including lift |
| Churn concentration | Cumulative Pareto curve with the 80% guide line and a marker at the crossing point |
| Most volatile objects / Ownership | The original bar ranking and author ownership list, now scoped by filters |

## HTTP API

All endpoints return JSON. `GET` endpoints accept the same filter query
parameters: `author_id`, `start` / `end` (epoch seconds), `commits`
(comma-separated SHAs, takes precedence over dates), and — where noted —
`path` and `kind` (`file` / `directory`).

| Method & path | Purpose | Extra parameters |
| --- | --- | --- |
| `GET /api/repositories` | List repositories and their status | — |
| `POST /api/repositories/clone` | Queue a clone + analysis (`{"url", "name"}`) | — |
| `POST /api/repositories/upload` | Queue an upload + analysis (multipart `archive`) | — |
| `POST /api/repositories/<id>/reanalyze` | Re-run analysis | — |
| `DELETE /api/repositories/<id>` | Delete repository and files | — |
| `GET /api/repositories/<id>/authors` | Authors with commit counts | — |
| `POST /api/repositories/<id>/authors/merge` | Merge two author identities | — |
| `GET /api/repositories/<id>/analytics` | Metric explorer rows, summary, ownership (honors `path` / `kind` focus) | `path`, `kind` |
| `GET /api/repositories/<id>/timeline` | Bucketed series + least-squares trend (honors `path` / `kind` focus) | `bucket=week\|month`, `path`, `kind` |
| `GET /api/repositories/<id>/insights` | Concentration report, Pareto curve, hotspot ranking | — |
| `GET /api/repositories/<id>/coupling` | Top churn files, co-change pairs with lift | `limit` (4–12) |

`insights` and `coupling` are repository-level views: they honor the
commit-level filters (author, dates, commit set) but intentionally ignore
`path` / `kind` focus.

## Project layout

```
app.py                  Flask app factory, filters, all HTTP endpoints
rat/db.py               SQLite schema (repositories, authors, commits, changes) + indexes
rat/git_analyzer.py     Streaming `git log` parser, directory rollups, analysis pipeline
rat/metrics.py          Pure metric algorithms (Gini, HHI, bus factor, Pareto, trend, association)
rat/services.py         Repository lifecycle: clone, ZIP extraction, thread-pool jobs
static/app.js           Dashboard logic + SVG chart renderer
static/style.css        Design system and chart styles
templates/index.html    Single-page dashboard markup
tests/test_rat.py       Unit tests for algorithms + integration tests over real Git fixtures
```
