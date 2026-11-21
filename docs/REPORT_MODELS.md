# Report Models

`nexdash.report_models` ships three ready-made reports. Each one is a cube on the
semantic layer, the table behind it, and a dashboard template made of panels. A panel
is a `QueryPlan` plus a visualization, so every number goes through the guarded SQL
compiler like any other query.

| Model | Cube / table | Input | Panels |
| --- | --- | --- | --- |
| `dev_productivity` | `dev_productivity` / `dev_activity` | Connector `WorkItem` objects (GitHub, Azure DevOps, Redmine, Jira, generic REST) | PRs merged, PRs still open, time to first review, PR lead time, issues closed; merged PRs and lead time per month; items delivered per person and per team |
| `security_posture` | `security_posture` / `security_snapshots` | Posture snapshots shaped like `SecurityPostureMetrics` from the nexcoder security package, or dicts with the same keys | Open critical and high findings, open findings, gate coverage, outdated dependencies, gate pass rate; findings by severity; critical and high trend; coverage trend; findings per project |
| `support_metrics` | `support_metrics` / `support_conversations` | Tickets shaped like the Support Engine `Ticket` model, or dicts with the same keys | Volume, time to first response, resolution rate, deflection rate, CSAT; volume, first response and CSAT per month; tickets by sentiment; hours to resolve per channel |

## Parameters

Every model takes the same `ModelParameters`:

- `start`, `end`: inclusive dates. Each panel applies them to its own date field
  (for example merged PRs use the merge date, open PRs the creation date).
- `team`, `project`: exact-match filters.

Values are bound as SQL parameters. When both dates are set, each KPI is also
computed for the previous period of the same length and shown as "up 12% vs.
previous period (better)".

## Usage

```python
from nexdash import SQLiteExecutor
from nexdash.connectors import GitHubConnector
from nexdash.report_models import ModelParameters, dev_rows_from_work_items, get_model, render_dashboard

model = get_model("dev_productivity")
with GitHubConnector("owner", "repo") as github, SQLiteExecutor("nexdash.db") as executor:
    items = list(github.fetch_items())  # reads NEXDASH_GITHUB_TOKEN
    rows = dev_rows_from_work_items(items, team_of={"alice": "platform"}, merged_keys=[...])
    model.load_rows(executor, rows)
    run = model.run(executor, ModelParameters(start="2026-04-01", end="2026-06-30", team="platform"))
    html = render_dashboard(run)  # or render_dashboard(run, locale="pt-BR")
```

The GitHub issues API does not say whether a closed pull request was merged, so pass
the merged keys with `merged_keys`. Azure DevOps (`completed`) and items with a
`merged` status or label are detected automatically. Review times are optional:
pass `review_hours={item_key: hours}`.

Other ways to use a model:

- `model.report_specs()` returns every panel as a `ReportSpec` for `ReportStore`
  (drafts, preview, publish, rollback).
- `publish_dashboard(DashboardStore(...), run, owner_email=...)` publishes the
  rendered page as a dashboard snapshot.
- `run.dataset()` returns the rows of every panel as JSON-friendly data.
- `nexdash --report-models` registers the three cubes for MCP and REST; with
  `--demo` their tables are filled with synthetic rows.

## Security snapshots

`security_rows_from_snapshots` writes five rows per snapshot (critical, high,
medium, low, info). Gate coverage and the gate result repeat on each row, so averages
over rows equal averages over snapshots. Periods may be `2026-03`, `2026-W12`,
`2026-Q1` or a date. Current-state panels (open findings, coverage, outdated
dependencies) read the most recent snapshot date inside the period, so record every
project on the same snapshot date.

## Support tickets

`support_rows_from_tickets` derives first response from the first public agent
comment, resolution from `solved_at`, deflection from `extra["deflected"]`, a
`deflected` tag, or a resolved ticket without agent comments, and sentiment from
`extra["sentiment"]` or `extra["sentiment_score"]` (-1 to 1, with +/-0.25 as the
neutral band). CSAT is the Support Engine value from 0 to 1, shown as a percentage.

## Dashboard template

The page is plain HTML with inline SVG and no JavaScript, so it works under the
dashboard Content-Security-Policy. It follows these rules:

- Up to five KPIs on top, the filter state written below the title.
- Lines for time, horizontal bars for rankings, sorted by value and starting at zero.
  Categories with a natural order (severity, sentiment) keep it.
- The chart headline states the finding ("Down 31% from 2026-04 to 2026-06"); the
  subtitle states what was measured and the unit.
- One element in a strong blue, the rest gray; values labeled on the chart.
- Every chart has a text alternative and a data table; empty and error states are drawn.
- Numbers are formatted for `en` (1,234.5) or `pt-BR` (1.234,5).

## Synthetic data

`nexdash.report_models.synthetic.seed_model_data(executor)` loads deterministic
synthetic rows (January to June 2026) for all three models. People, teams, projects
and numbers are invented. The offline tests in `tests/test_report_models.py` build
every dashboard from this data.
