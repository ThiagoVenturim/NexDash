"""Server-side HTML template for report model dashboards.

The page is plain HTML with inline SVG and no JavaScript, so it renders under the
dashboard Content-Security-Policy (``connect-src 'none'``) and in any mail client or
static host. Charts follow a small set of visualization rules:

- KPIs on top (at most five), each compared with the previous period when one is set.
- Time series as lines; rankings as horizontal bars sorted by value, starting at zero.
- The headline states what the data shows; the subtitle states what was measured.
- One element highlighted in a strong color, everything else neutral gray.
- Values labeled directly on the chart; units in every label.
- Color-blind safe palette (blue against gray), never color alone: comparisons are
  also written as text ("better" or "worse").
- Every chart has a text alternative and a data table.

Every value that comes from data or parameters is HTML-escaped.
"""

from __future__ import annotations

import html
import math
from typing import Any, Literal

from nexdash.report_models.base import ModelRun, Panel, PanelResult

Locale = Literal["en", "pt-BR"]

STRONG = "#1F5FAD"
NEUTRAL = "#A7B0BA"
TEXT = "#1A1A1A"
MUTED = "#55606C"
GRID = "#E3E6EA"

_UNIT_NAMES = {"count": "count", "hours": "hours", "minutes": "minutes", "percent": "%", "score": "score"}
_MAX_LABEL_CHARS = 24
_MAX_X_LABELS = 6


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def _fmt(value: float, decimals: int, locale: Locale) -> str:
    text = f"{value:,.{decimals}f}"
    if locale == "pt-BR":
        text = text.replace(",", "\0").replace(".", ",").replace("\0", ".")
    return text


def format_number(value: Any, unit: str = "count", locale: Locale = "en") -> str:
    """Format a value with its unit (``1,234``, ``12.5 h``, ``87.5%``; pt-BR uses ``1.234`` and ``87,5%``)."""
    number = _num(value)
    if number is None:
        return "n/a"
    if unit == "percent":
        return _fmt(number * 100, 1, locale) + "%"
    if unit == "hours":
        return _fmt(number, 1, locale) + " h"
    if unit == "minutes":
        return _fmt(number, 0, locale) + " min"
    if unit == "score":
        return _fmt(number, 2, locale)
    return _fmt(number, 0 if number.is_integer() else 1, locale)


def compare_text(
    current: Any,
    previous: Any,
    unit: str = "count",
    *,
    lower_is_better: bool = False,
    locale: Locale = "en",
) -> str:
    """Describe the change against the previous period, for example ``up 12% vs. previous period (better)``."""
    cur, prev = _num(current), _num(previous)
    if cur is None or prev is None:
        return ""
    if unit == "percent":
        delta = (cur - prev) * 100
        amount = _fmt(abs(delta), 1, locale) + " pp"
    elif prev == 0:
        return f"previous period: {format_number(prev, unit, locale)}"
    else:
        delta = (cur - prev) / abs(prev) * 100
        amount = _fmt(abs(delta), 0, locale) + "%"
    if abs(delta) < 0.5:
        return "no change vs. previous period"
    direction = "up" if delta > 0 else "down"
    improved = (delta < 0) if lower_is_better else (delta > 0)
    return f"{direction} {amount} vs. previous period ({'better' if improved else 'worse'})"


def _points(result: PanelResult) -> list[tuple[str, float | None]]:
    chart = result.chart
    if chart is None:
        return []
    pairs = zip(chart.labels, chart.values, strict=False)
    return [("none" if label is None else str(label), _num(value)) for label, value in pairs]


def _bar_points(result: PanelResult) -> list[tuple[str, float]]:
    points = [(label, value) for label, value in _points(result) if value is not None]
    if not result.panel.keep_order:
        points.sort(key=lambda item: (-item[1], item[0]))
    return points


def _highlight_index(panel: Panel, points: list[tuple[str, float]]) -> int:
    if panel.highlight is not None:
        for index, (label, _value) in enumerate(points):
            if label == panel.highlight:
                return index
    if not points:
        return -1
    best = max(range(len(points)), key=lambda i: points[i][1])
    return best


def headline(result: PanelResult, locale: Locale = "en") -> str:
    """One-sentence conclusion computed from the panel data (no model call)."""
    panel = result.panel
    unit = panel.unit
    if panel.kind == "kpi":
        return format_number(result.value, unit, locale)
    if panel.kind == "line":
        points = [(label, value) for label, value in _points(result) if value is not None]
        if not points:
            return ""
        if len(points) == 1:
            return f"{format_number(points[0][1], unit, locale)} in {points[0][0]}"
        (first_label, first), (last_label, last) = points[0], points[-1]
        if unit == "percent":
            delta: float | None = (last - first) * 100
            amount = _fmt(abs(delta), 1, locale) + " pp"
        elif first != 0:
            delta = (last - first) / abs(first) * 100
            amount = _fmt(abs(delta), 0, locale) + "%"
        else:
            delta = None
            amount = ""
        if delta is None:
            text = (
                f"From {format_number(first, unit, locale)} in {first_label} "
                f"to {format_number(last, unit, locale)} in {last_label}"
            )
        elif abs(delta) < 0.5:
            text = f"Flat from {first_label} to {last_label}"
        else:
            text = f"{'Up' if delta > 0 else 'Down'} {amount} from {first_label} to {last_label}"
        peak = max(range(len(points)), key=lambda i: points[i][1])
        if 0 < peak < len(points) - 1:
            text += f", peak of {format_number(points[peak][1], unit, locale)} in {points[peak][0]}"
        return text
    points = _bar_points(result)
    if not points:
        return ""
    index = _highlight_index(panel, points)
    label, value = points[index]
    if panel.highlight is not None and label == panel.highlight:
        total = sum(v for _l, v in points)
        if unit == "count" and total > 0:
            share = _fmt(value / total * 100, 0, locale)
            return f"{label}: {format_number(value, unit, locale)} of {format_number(total, unit, locale)} ({share}%)"
        return f"{label}: {format_number(value, unit, locale)}"
    verb = "leads with" if unit == "count" else "is highest at"
    return f"{label} {verb} {format_number(value, unit, locale)}"


def _nice_ceiling(value: float) -> float:
    if value <= 0:
        return 1.0
    exponent = 10 ** math.floor(math.log10(value))
    for multiple in (1, 2, 2.5, 5, 10):
        if multiple * exponent >= value:
            return multiple * exponent
    return 10 * exponent


def _short(label: str) -> str:
    return label if len(label) <= _MAX_LABEL_CHARS else label[: _MAX_LABEL_CHARS - 3] + "..."


def _svg_open(panel_id: str, width: int, height: int, title: str, desc: str) -> str:
    pid = _esc(panel_id)
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-labelledby="{pid}-title {pid}-desc" xmlns="http://www.w3.org/2000/svg">'
        f'<title id="{pid}-title">{_esc(title)}</title><desc id="{pid}-desc">{_esc(desc)}</desc>'
    )


def _describe_points(points: list[tuple[str, float | None]], unit: str, locale: Locale) -> str:
    shown = points[:24]
    text = "; ".join(f"{label}: {format_number(value, unit, locale)}" for label, value in shown)
    return text + ("; ..." if len(points) > len(shown) else "")


def line_svg(result: PanelResult, title: str, locale: Locale = "en") -> str:
    """Single-series line chart from zero, with the last value labeled and the peak annotated."""
    panel = result.panel
    points = _points(result)
    width, height = 640, 240
    left, right, top, bottom = 64, 104, 20, 32
    plot_w, plot_h = width - left - right, height - top - bottom
    values = [v for _l, v in points if v is not None]
    y_max = _nice_ceiling(max(values) if values else 1.0)
    count = len(points)

    def x_at(i: int) -> float:
        return left + (plot_w * i / (count - 1) if count > 1 else plot_w / 2)

    def y_at(v: float) -> float:
        return top + plot_h * (1 - v / y_max)

    parts = [_svg_open(panel.id, width, height, title, _describe_points(points, panel.unit, locale))]
    for fraction in (0.0, 0.5, 1.0):
        y = y_at(y_max * fraction)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" stroke="{GRID}"/>')
        parts.append(
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="11" fill="{MUTED}">'
            f"{_esc(format_number(y_max * fraction, panel.unit, locale))}</text>"
        )
    step = max(1, math.ceil(count / _MAX_X_LABELS))
    for i, (label, _v) in enumerate(points):
        if i % step == 0 or i == count - 1:
            parts.append(
                f'<text x="{x_at(i):.1f}" y="{height - 10}" text-anchor="middle" font-size="11" fill="{MUTED}">'
                f"{_esc(_short(label))}</text>"
            )
    segment: list[str] = []
    segments: list[list[str]] = []
    for i, (_label, value) in enumerate(points):
        if value is None:
            if segment:
                segments.append(segment)
            segment = []
            continue
        segment.append(f"{x_at(i):.1f},{y_at(value):.1f}")
    if segment:
        segments.append(segment)
    for seg in segments:
        if len(seg) == 1:
            x, y = seg[0].split(",")
            parts.append(f'<circle cx="{x}" cy="{y}" r="3" fill="{STRONG}"/>')
        else:
            parts.append(f'<polyline points="{" ".join(seg)}" fill="none" stroke="{STRONG}" stroke-width="2.5"/>')
    indexed = [(i, v) for i, (_l, v) in enumerate(points) if v is not None]
    if indexed:
        last_i, last_v = indexed[-1]
        parts.append(f'<circle cx="{x_at(last_i):.1f}" cy="{y_at(last_v):.1f}" r="4" fill="{STRONG}"/>')
        parts.append(
            f'<text x="{x_at(last_i) + 8:.1f}" y="{y_at(last_v) + 4:.1f}" font-size="12" font-weight="600" '
            f'fill="{TEXT}">{_esc(format_number(last_v, panel.unit, locale))}</text>'
        )
        peak_i, peak_v = max(indexed, key=lambda item: item[1])
        if peak_i != last_i and peak_v > last_v:
            parts.append(f'<circle cx="{x_at(peak_i):.1f}" cy="{y_at(peak_v):.1f}" r="3" fill="{STRONG}"/>')
            parts.append(
                f'<text x="{x_at(peak_i):.1f}" y="{max(y_at(peak_v) - 8, 12):.1f}" text-anchor="middle" '
                f'font-size="11" fill="{TEXT}">peak {_esc(format_number(peak_v, panel.unit, locale))}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def bar_svg(result: PanelResult, title: str, locale: Locale = "en") -> str:
    """Horizontal bars from zero, sorted by value unless the categories have a natural order."""
    panel = result.panel
    points = _bar_points(result)
    bar_h, gap, label_w, right, width = 22, 10, 160, 88, 640
    height = len(points) * (bar_h + gap) + gap
    x_max = max((v for _l, v in points), default=0.0) or 1.0
    span = width - label_w - right
    highlight = _highlight_index(panel, points)
    parts = [_svg_open(panel.id, width, height, title, _describe_points(list(points), panel.unit, locale))]
    for i, (label, value) in enumerate(points):
        y = gap + i * (bar_h + gap)
        bar_w = max(span * max(value, 0.0) / x_max, 0.0)
        color = STRONG if i == highlight else NEUTRAL
        parts.append(
            f'<text x="{label_w - 8}" y="{y + bar_h / 2 + 4:.1f}" text-anchor="end" font-size="12" fill="{TEXT}">'
            f"{_esc(_short(label))}</text>"
        )
        parts.append(f'<rect x="{label_w}" y="{y}" width="{bar_w:.1f}" height="{bar_h}" fill="{color}"/>')
        weight = ' font-weight="600"' if i == highlight else ""
        parts.append(
            f'<text x="{label_w + bar_w + 6:.1f}" y="{y + bar_h / 2 + 4:.1f}" font-size="12"{weight} '
            f'fill="{TEXT}">{_esc(format_number(value, panel.unit, locale))}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _humanize(field_name: str | None) -> str:
    return (field_name or "").replace("_", " ")


def _data_table(result: PanelResult, locale: Locale) -> str:
    panel = result.panel
    head = (
        f'<tr><th scope="col">{_esc(_humanize(panel.x))}</th>'
        f'<th scope="col">{_esc(_humanize(panel.y))}</th></tr>'
    )
    body = "".join(
        f"<tr><td>{_esc(label)}</td><td>{_esc(format_number(value, panel.unit, locale))}</td></tr>"
        for label, value in _points(result)
    )
    return f"<details><summary>Data table</summary><table>{head}{body}</table></details>"


def _subtitle(panel: Panel, result: PanelResult) -> str:
    text = f"{panel.title} ({_UNIT_NAMES.get(panel.unit, panel.unit)})"
    if result.snapshot:
        text += f", snapshot of {result.snapshot}"
    return text


def _kpi_card(result: PanelResult, locale: Locale) -> str:
    panel = result.panel
    if result.error:
        body = f'<p class="error">Could not load: {_esc(result.error)}</p>'
    elif result.is_empty:
        body = '<p class="empty">No data for the selected filters.</p>'
    else:
        compare = compare_text(
            result.value, result.previous_value, panel.unit, lower_is_better=panel.lower_is_better, locale=locale
        )
        body = f'<p class="kpi-value">{_esc(format_number(result.value, panel.unit, locale))}</p>'
        if compare:
            body += f'<p class="kpi-compare">{_esc(compare)}</p>'
    note = f'<p class="kpi-note">Snapshot of {_esc(result.snapshot)}</p>' if result.snapshot else ""
    return f'<section class="kpi" id="panel-{_esc(panel.id)}"><h2>{_esc(panel.title)}</h2>{body}{note}</section>'


def _chart_card(result: PanelResult, locale: Locale) -> str:
    panel = result.panel
    if result.error:
        title = panel.title
        body = f'<p class="error">Could not load this panel: {_esc(result.error)}</p>'
    elif result.is_empty:
        title = panel.title
        body = '<p class="empty">No data for the selected filters.</p>'
    else:
        title = headline(result, locale) or panel.title
        chart = line_svg(result, title, locale) if panel.kind == "line" else bar_svg(result, title, locale)
        body = chart + _data_table(result, locale)
    return (
        f'<section class="card" id="panel-{_esc(panel.id)}"><h2>{_esc(title)}</h2>'
        f'<p class="subtitle">{_esc(_subtitle(panel, result))}</p>{body}</section>'
    )


_STYLE = f"""
*{{box-sizing:border-box}}
body{{margin:0;padding:24px;font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;color:{TEXT};background:#fff}}
header h1{{margin:0 0 4px;font-size:22px}}
header p{{margin:0;color:{MUTED}}}
.filters{{margin-top:6px;font-weight:600;color:{TEXT}}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:20px 0}}
.kpi{{border:1px solid {GRID};border-radius:8px;padding:12px 14px}}
.kpi h2{{margin:0;font-size:13px;font-weight:500;color:{MUTED}}}
.kpi-value{{margin:6px 0 2px;font-size:28px;font-weight:700}}
.kpi-compare,.kpi-note{{margin:0;font-size:12px;color:{MUTED}}}
.charts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px}}
.card{{border:1px solid {GRID};border-radius:8px;padding:14px 16px}}
.card h2{{margin:0;font-size:16px}}
.subtitle{{margin:2px 0 10px;color:{MUTED};font-size:12px}}
.chart{{display:block;max-width:100%;height:auto}}
.empty,.error{{padding:24px 0;color:{MUTED}}}
.error{{color:#8A1C1C}}
details{{margin-top:8px;font-size:12px}}
table{{border-collapse:collapse;margin-top:6px}}
th,td{{padding:2px 12px 2px 0;text-align:left}}
td+td,th+th{{text-align:right}}
footer{{margin-top:20px;color:{MUTED};font-size:12px}}
"""


def render_dashboard(run: ModelRun, *, locale: Locale = "en") -> str:
    """Render a model run as a self-contained HTML dashboard (no scripts, no external assets)."""
    model = run.model
    kpis = "".join(_kpi_card(result, locale) for result in run.kpis)
    charts = "".join(_chart_card(result, locale) for result in run.charts)
    source = f"Source: {model.source_note}. " if model.source_note else ""
    lang = "pt-BR" if locale == "pt-BR" else "en"
    return (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(model.title)}</title><style>{_STYLE}</style></head><body>"
        f"<header><h1>{_esc(model.title)}</h1><p>{_esc(model.description)}</p>"
        f'<p class="filters">{_esc(run.params.describe())}</p></header>'
        f'<main><div class="kpis">{kpis}</div><div class="charts">{charts}</div></main>'
        f"<footer>{_esc(source)}Generated {_esc(run.generated_at)} by nexdash report model "
        f"'{_esc(model.name)}'.</footer></body></html>"
    )


def publish_dashboard(
    store: Any,
    run: ModelRun,
    *,
    owner_email: str,
    locale: Locale = "en",
    validity_days: int | None = None,
) -> dict[str, Any]:
    """Publish a rendered model run as a snapshot through ``DashboardStore.publish_snapshot``."""
    kwargs: dict[str, Any] = {}
    if validity_days is not None:
        kwargs["validity_days"] = validity_days
    return store.publish_snapshot(
        title=run.model.title,
        html=render_dashboard(run, locale=locale),
        owner_email=owner_email,
        description=run.params.describe(),
        origin=f"report-model:{run.model.name}",
        **kwargs,
    )
