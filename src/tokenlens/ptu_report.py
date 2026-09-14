"""PTU Advisor dashboard rendering.

The renderer consumes :class:`~tokenlens.ptu.PtuDashboardData` only. It never
reaches back into raw records and never recomputes a rate, a token count, or a
recommendation: everything shown here is produced by the analysis engine.

Charts are native inline SVG with no external JavaScript, font, or network
dependency. The server renders the full observed window so the report is
complete without JavaScript; the embedded payload additionally powers
synchronized zoom, expansion, and a JSON export when scripting is available.
"""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime
from typing import Any, Sequence

from .ptu import (
    DASHBOARD_STATE_LABELS,
    MINIMUM_ACTIVE_BUCKETS,
    PtuCostCurve,
    PtuDashboardData,
    PtuDeploymentAssessment,
    PtuPortfolioAssessment,
    PtuThroughputSeries,
)

DISPLAY_MAX_POINTS = 360
TABLE_MAX_ROWS = 24

_COLORS = {
    "input": "#73c7ff",
    "cached": "#ffc857",
    "output": "#57d68b",
    "total": "#b9a7ff",
    "success": "#57d68b",
    "throttled": "#ffc857",
    "failed": "#ff8fa3",
    "neutral": "#d2deee",
}

_GEOMETRY = {"w": 720, "h": 260, "l": 62, "r": 700, "t": 20, "b": 206}


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _slug(value: str, index: int) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value).casefold()).strip("-")
    return f"{cleaned or 'deployment'}-{index}"


def _epoch(value: datetime) -> int:
    stamp = value if value.tzinfo else value.replace(tzinfo=UTC)
    return int(stamp.timestamp())


def _iso(value: datetime) -> str:
    stamp = value if value.tzinfo else value.replace(tzinfo=UTC)
    return stamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _number(value: float | int | None, *, precision: int = 0) -> str:
    if value is None:
        return "Unavailable"
    return f"{value:,.{precision}f}"


def money(value: float | None, currency: str = "USD", *, precision: int = 2, unavailable: str = "Unavailable") -> str:
    """Format a monetary estimate with adaptive precision.

    A genuinely nonzero estimate must never render as ``$0.00``, so the decimal
    count widens until the value is visibly distinguishable from zero.
    """
    if value is None:
        return unavailable
    prefix = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency.upper(), f"{currency.upper()} ")
    if value == 0:
        # An exact zero is a real measurement (for example, a day with no
        # cached input). Render it as an unambiguous zero rather than a padded
        # decimal that could be mistaken for a rounded-down micro-cost.
        return f"{prefix}0"
    if round(value, precision) == 0:
        adaptive = precision
        while adaptive < 10 and round(value, adaptive) == 0:
            adaptive += 1
        return f"{prefix}{value:,.{adaptive}f}"
    return f"{prefix}{value:,.{precision}f}"


def _money(value: float | None, currency: str = "USD", *, precision: int = 2) -> str:
    return money(value, currency, precision=precision)


def _formatter(kind: str, currency: str = "USD"):
    if kind == "money":
        return lambda value: _money(float(value), currency, precision=2)
    if kind == "requests":
        return lambda value: f"{float(value):,.0f} requests"
    if kind == "events":
        return lambda value: f"{float(value):,.0f} events"
    return lambda value: f"{float(value):,.0f} tokens"


def _bind_formats(series: list[dict[str, Any]], currency: str = "USD") -> list[dict[str, Any]]:
    for item in series:
        item.setdefault("fmt", "tokens")
        item["format"] = _formatter(item["fmt"], currency)
    return series


def _client_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Serializable mirror of the chart spec used for client-side zoom redraw."""
    return {
        "key": spec["key"],
        "id": spec["id"],
        "source": spec["source"],
        "primary": spec["primary"],
        "geometry": spec["geometry"],
        "x": spec["x_kind"],
        "currency": spec.get("currency", "USD"),
        "yfmt": spec.get("y_kind", "number"),
        "series": [
            {
                "index": item["index"],
                "label": item["label"],
                "color": item["color"],
                "fmt": item["fmt"],
                "stack": bool(item.get("stack")),
                "dashed": bool(item.get("dashed")),
                "width": item.get("width", 2),
            }
            for item in spec["series"]
        ],
        "references": [
            {"value": reference["value"], "color": reference["color"], "label": reference["label"]}
            for reference in spec.get("references", [])
            if reference["value"] is not None
        ],
    }


def _lttb(rows: list[list[float | None]], threshold: int, primary: int) -> list[list[float | None]]:
    """Largest-Triangle-Three-Buckets downsampling for display only.

    Exact values are retained in the embedded payload, tables, and exports; only
    the plotted geometry is reduced.
    """
    count = len(rows)
    if threshold >= count or threshold < 3:
        return rows
    sampled = [rows[0]]
    every = (count - 2) / (threshold - 2)
    a = 0
    for i in range(threshold - 2):
        start = int((i + 1) * every) + 1
        end = min(int((i + 2) * every) + 1, count)
        bucket = rows[start:end] or [rows[-1]]
        avg_x = sum(float(row[0]) for row in bucket) / len(bucket)
        avg_y = sum(float(row[primary] or 0) for row in bucket) / len(bucket)
        range_start = int(i * every) + 1
        range_end = min(int((i + 1) * every) + 1, count)
        point_a = rows[a]
        best, best_area = None, -1.0
        for candidate in rows[range_start:range_end]:
            area = abs(
                (float(point_a[0]) - avg_x) * (float(candidate[primary] or 0) - float(point_a[primary] or 0))
                - (float(point_a[0]) - float(candidate[0])) * (avg_y - float(point_a[primary] or 0))
            )
            if area > best_area:
                best, best_area = candidate, area
        chosen = best if best is not None else rows[range_start]
        sampled.append(chosen)
        a = rows.index(chosen, range_start, max(range_start + 1, range_end))
    sampled.append(rows[-1])
    return sampled


def _scale_max(rows: Sequence[Sequence[float | None]], series: list[dict[str, Any]]) -> float:
    stacked: list[float] = []
    plain: list[float] = []
    for row in rows:
        column_total = 0.0
        for item in series:
            value = row[item["index"]]
            if value is None:
                continue
            if item.get("stack"):
                column_total += float(value)
            else:
                plain.append(float(value))
        stacked.append(column_total)
    return max([*stacked, *plain, 1.0])


def _series_markup(spec: dict[str, Any], rows: list[list[float | None]]) -> str:
    """Render the plotted geometry for one chart (shared shape with the client)."""
    geometry = spec["geometry"]
    left, right, top, bottom = geometry["l"], geometry["r"], geometry["t"], geometry["b"]
    if not rows:
        return (
            f'<text x="{(left + right) / 2:.0f}" y="{(top + bottom) / 2:.0f}" text-anchor="middle" '
            f'class="axis-label">No observations in the selected range</text>'
        )
    xs = [float(row[0]) for row in rows]
    min_x, max_x = min(xs), max(xs)
    span = (max_x - min_x) or 1.0
    max_y = _scale_max(rows, spec["series"])
    width = right - left

    def sx(value: float) -> float:
        return left + (value - min_x) / span * width

    def sy(value: float) -> float:
        return bottom - max(0.0, value) / max_y * (bottom - top)

    parts: list[str] = []
    column_series = [item for item in spec["series"] if item.get("stack")]
    if column_series:
        slot = max(2.0, width / max(1, len(rows)) * 0.62)
        for row in rows:
            base = bottom
            x = sx(float(row[0])) - slot / 2
            for item in column_series:
                value = row[item["index"]]
                if not value:
                    continue
                height = float(value) / max_y * (bottom - top)
                base -= height
                label = f"{spec['point_label'](row)} · {item['label']}: {item['format'](value)}"
                parts.append(
                    f'<rect x="{x:.1f}" y="{base:.1f}" width="{slot:.1f}" height="{height:.1f}" fill="{item["color"]}" '
                    f'tabindex="0" role="img" aria-label="{_escape(label)}"><title>{_escape(label)}</title></rect>'
                )
    for item in spec["series"]:
        if item.get("stack"):
            continue
        points = [(sx(float(row[0])), sy(float(row[item["index"]]))) for row in rows if row[item["index"]] is not None]
        if not points:
            continue
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        dash = ' stroke-dasharray="6 4"' if item.get("dashed") else ""
        parts.append(
            f'<polyline points="{path}" fill="none" stroke="{item["color"]}" stroke-width="{item.get("width", 2)}"{dash}></polyline>'
        )
    marker_series = next((item for item in spec["series"] if not item.get("stack")), None)
    if marker_series is not None:
        stride = max(1, len(rows) // 18)
        for row in rows[::stride]:
            value = row[marker_series["index"]]
            if value is None:
                continue
            label = f"{spec['point_label'](row)} · {marker_series['label']}: {marker_series['format'](value)}"
            parts.append(
                f'<circle cx="{sx(float(row[0])):.1f}" cy="{sy(float(value)):.1f}" r="3.2" fill="{marker_series["color"]}" '
                f'tabindex="0" role="img" aria-label="{_escape(label)}"><title>{_escape(label)}</title></circle>'
            )
    for reference in spec.get("references", []):
        value = reference["value"]
        if value is None:
            continue
        y = sy(float(value))
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="{reference["color"]}" '
            f'stroke-width="1.2" stroke-dasharray="4 4"></line>'
            f'<text x="{right}" y="{y - 5:.1f}" text-anchor="end" class="axis-value">{_escape(reference["label"])}</text>'
        )
    parts.append(
        f'<text x="{left}" y="{bottom + 18:.0f}" class="axis-label">{_escape(spec["x_label"](min_x))}</text>'
        f'<text x="{right}" y="{bottom + 18:.0f}" text-anchor="end" class="axis-label">{_escape(spec["x_label"](max_x))}</text>'
        f'<text x="{left - 8}" y="{top + 6}" text-anchor="end" class="axis-value">{_escape(spec["y_format"](max_y))}</text>'
        f'<text x="{left - 8}" y="{bottom}" text-anchor="end" class="axis-value">0</text>'
    )
    return "".join(parts)


def _chart_svg(spec: dict[str, Any], rows: list[list[float | None]]) -> str:
    geometry = spec["geometry"]
    title_id, desc_id = f"{spec['id']}-title", f"{spec['id']}-desc"
    display = _lttb(rows, DISPLAY_MAX_POINTS, spec["primary"])
    return f"""<svg class="ptu-chart-svg" viewBox="0 0 {geometry['w']} {geometry['h']}" role="img"
      aria-labelledby="{title_id} {desc_id}" data-chart-svg="{_escape(spec['key'])}">
      <title id="{title_id}">{_escape(spec['title'])}</title>
      <desc id="{desc_id}">{_escape(spec['description'])}</desc>
      <line x1="{geometry['l']}" y1="{geometry['b']}" x2="{geometry['r']}" y2="{geometry['b']}" class="axis"></line>
      <line x1="{geometry['l']}" y1="{geometry['t']}" x2="{geometry['l']}" y2="{geometry['b']}" class="axis"></line>
      <g data-series-layer>{_series_markup(spec, display)}</g>
    </svg>"""


def _legend(spec: dict[str, Any]) -> str:
    return '<div class="legend">' + "".join(
        f'<span class="legend-row"><span class="swatch" style="background:{item["color"]}"></span>{_escape(item["label"])}</span>'
        for item in spec["series"]
    ) + "</div>"


def _table(spec: dict[str, Any], rows: list[list[float | None]]) -> str:
    stride = max(1, len(rows) // TABLE_MAX_ROWS) if rows else 1
    sampled = rows[::stride]
    header = "".join(f'<th scope="col">{_escape(item["label"])}</th>' for item in spec["series"])
    body = "".join(
        "<tr>"
        + f'<th scope="row">{_escape(spec["point_label"](row))}</th>'
        + "".join(
            f"<td>{_escape(item['format'](row[item['index']]) if row[item['index']] is not None else 'Unavailable')}</td>"
            for item in spec["series"]
        )
        + "</tr>"
        for row in sampled
    )
    caption = (
        f"{spec['title']} data ({len(rows):,} observed point(s)"
        + (f", sampled every {stride}" if stride > 1 else "")
        + ")"
    )
    return f"""<details class="chart-table"><summary>Data table</summary>
      <div class="table-scroll"><table class="data-table"><caption>{_escape(caption)}</caption>
      <thead><tr><th scope="col">{_escape(spec['x_title'])}</th>{header}</tr></thead>
      <tbody>{body or f'<tr><td colspan="{len(spec["series"]) + 1}">No observations.</td></tr>'}</tbody></table></div></details>"""


def _chart_card(spec: dict[str, Any], rows: list[list[float | None]], *, state_note: str | None = None) -> str:
    unavailable = state_note is not None
    body = (
        f'<p class="chart-unavailable">{_escape(state_note)}</p>'
        if unavailable
        else _legend(spec) + _chart_svg(spec, rows) + _range_controls(spec) + _table(spec, rows)
    )
    badge = f'<span class="chart-badge">{_escape(spec["badge"])}</span>' if spec.get("badge") else ""
    expand = (
        ""
        if unavailable
        else f'<button type="button" class="chart-button" data-ptu-expand="{_escape(spec["id"])}">Expand</button>'
    )
    return f"""<article class="chart-card ptu-chart" id="{_escape(spec['id'])}" data-ptu-chart="{_escape(spec['key'])}"
      data-ptu-spec="{_escape(json.dumps(_client_spec(spec), separators=(',', ':')))}">
      <div class="chart-head"><div><h3>{_escape(spec['title'])}</h3><small>{_escape(spec['subtitle'])}</small></div>
      <div class="chart-actions">{badge}{expand}</div></div>
      {body}
      <p class="chart-summary" data-chart-summary>{_escape(spec['description'])}</p>
    </article>"""


def _range_controls(spec: dict[str, Any]) -> str:
    return f"""<div class="chart-range" role="group" aria-label="{_escape(spec['title'])} range">
      <label><span>Range start</span><input type="range" min="0" max="1000" value="0" step="1"
        data-ptu-range="start" aria-label="{_escape(spec['title'])} range start"></label>
      <label><span>Range end</span><input type="range" min="0" max="1000" value="1000" step="1"
        data-ptu-range="end" aria-label="{_escape(spec['title'])} range end"></label>
      <button type="button" class="chart-button" data-ptu-reset>Reset zoom</button>
      <span class="range-readout" data-ptu-range-readout>Full observed window</span>
    </div>"""


def _tokens_spec(data: PtuDashboardData, slug: str, cached_available: bool) -> dict[str, Any]:
    series = _bind_formats(
        [
            {"key": "input_tokens", "index": 1, "label": "Prompt tokens", "color": _COLORS["input"]},
            *(
                [{"key": "cached_tokens", "index": 2, "label": "Cached input tokens", "color": _COLORS["cached"]}]
                if cached_available
                else []
            ),
            {"key": "output_tokens", "index": 3, "label": "Completion tokens", "color": _COLORS["output"]},
            {"key": "total_tokens", "index": 4, "label": "Total tokens", "color": _COLORS["total"], "width": 2.4},
        ]
    )
    summary = data.summary
    return {
        "key": "tokens",
        "id": f"ptu-chart-tokens-{slug}",
        "source": "evidence",
        "title": "Token Volume Over Time",
        "subtitle": "Tokens per five-minute bucket · gaps are preserved, never zero-filled",
        "description": (
            f"Prompt, cached, and completion tokens across {len(data.evidence):,} observed five-minute buckets. "
            f"Total observed tokens {_number(summary.total_tokens)}."
            + ("" if cached_available else " Cached-token metric unavailable.")
        ),
        "series": series,
        "primary": 4,
        "geometry": _GEOMETRY,
        "x_kind": "time",
        "y_kind": "number",
        "x_title": "Bucket (UTC)",
        "x_label": lambda value: _iso(datetime.fromtimestamp(float(value), tz=UTC)),
        "y_format": lambda value: f"{value:,.0f}",
        "point_label": lambda row: _iso(datetime.fromtimestamp(float(row[0]), tz=UTC)),
    }


def _outcomes_spec(data: PtuDashboardData, slug: str) -> dict[str, Any]:
    series = _bind_formats(
        [
            {"key": "successful_requests", "index": 5, "label": "Successful requests", "color": _COLORS["success"], "fmt": "requests"},
            {"key": "rate_limited_requests", "index": 6, "label": "Rate-limited (429)", "color": _COLORS["throttled"], "fmt": "requests"},
            {"key": "failed_requests", "index": 7, "label": "Other failures", "color": _COLORS["failed"], "fmt": "requests"},
            {"key": "total_requests", "index": 8, "label": "Total requests", "color": _COLORS["neutral"], "dashed": True, "fmt": "requests"},
        ]
    )
    return {
        "key": "outcomes",
        "id": f"ptu-chart-outcomes-{slug}",
        "source": "evidence",
        "title": "Request Outcomes",
        "subtitle": "Requests per five-minute bucket · success is never derived from total minus 429",
        "description": (
            f"Successful, rate-limited, and failed request counts per bucket. "
            f"Total observed requests {_number(data.summary.total_requests)}, "
            f"rate-limited {_number(data.summary.rate_limited_requests)}."
        ),
        "series": series,
        "primary": 8,
        "geometry": _GEOMETRY,
        "x_kind": "time",
        "y_kind": "number",
        "x_title": "Bucket (UTC)",
        "x_label": lambda value: _iso(datetime.fromtimestamp(float(value), tz=UTC)),
        "y_format": lambda value: f"{value:,.0f}",
        "point_label": lambda row: _iso(datetime.fromtimestamp(float(row[0]), tz=UTC)),
    }


def _cost_spec(data: PtuDashboardData, slug: str) -> dict[str, Any]:
    summary = data.summary
    currency = summary.pricing_currency
    series = _bind_formats(
        [
            {"key": "input_cost", "index": 1, "label": "Input cost", "color": _COLORS["input"], "stack": True, "fmt": "money"},
            {"key": "cached_input_cost", "index": 2, "label": "Cached input cost", "color": _COLORS["cached"], "stack": True, "fmt": "money"},
            {"key": "output_cost", "index": 3, "label": "Output cost", "color": _COLORS["output"], "stack": True, "fmt": "money"},
            {"key": "total_cost", "index": 4, "label": "Total daily cost", "color": _COLORS["total"], "width": 2.2, "fmt": "money"},
        ],
        currency,
    )
    partial = summary.pricing_coverage_tokens_percent < 100
    return {
        "key": "cost",
        "id": f"ptu-chart-cost-{slug}",
        "source": "cost",
        "title": "Daily Deployment Cost",
        "subtitle": (
            f"{currency} · same pricing engine and provenance as Cost analysis"
            + (" · partial estimate" if partial else "")
        ),
        "badge": f"Total: {_money(summary.total_cost, currency)}",
        "description": (
            f"Daily input, cached-input, and output cost for this deployment. "
            f"Total {_money(summary.total_cost, currency)} across {len(data.daily_cost):,} observed day(s), "
            f"average {_money(summary.average_daily_cost, currency)} per priced day. "
            f"Pricing covers {summary.pricing_coverage_tokens_percent:.1f}% of observed tokens."
        ),
        "series": series,
        "primary": 4,
        "geometry": _GEOMETRY,
        "x_kind": "date",
        "y_kind": "money",
        "currency": currency,
        "x_title": "Day (UTC)",
        "x_label": lambda value: datetime.fromtimestamp(float(value), tz=UTC).strftime("%Y-%m-%d"),
        "y_format": lambda value: _money(value, currency, precision=2),
        "point_label": lambda row: datetime.fromtimestamp(float(row[0]), tz=UTC).strftime("%Y-%m-%d"),
        "references": [
            {
                "value": summary.average_daily_cost,
                "color": _COLORS["failed"],
                "label": f"Average {_money(summary.average_daily_cost, currency)}",
            }
        ],
    }


def _rate_limit_spec(data: PtuDashboardData, slug: str) -> dict[str, Any]:
    series = _bind_formats(
        [
            {
                "key": "rate_limited_requests",
                "index": 6,
                "label": "HTTP 429 events",
                "color": _COLORS["throttled"],
                "stack": True,
                "fmt": "events",
            }
        ]
    )
    summary = data.summary
    return {
        "key": "ratelimit",
        "id": f"ptu-chart-ratelimit-{slug}",
        "source": "evidence",
        "title": "Rate-Limit Events (429)",
        "subtitle": "HTTP 429 responses per five-minute bucket",
        "badge": (
            f"{_number(summary.rate_limited_requests)} events · {summary.rate_limit_percent:.2f}%"
            if summary.rate_limit_percent is not None
            else f"{_number(summary.rate_limited_requests)} events · rate unavailable"
            if summary.rate_limited_requests is not None
            else "Metric unavailable"
        ),
        "description": (
            f"HTTP 429 responses per bucket. {_number(summary.rate_limited_requests)} total events, "
            f"{summary.rate_limit_percent:.2f}% of observed requests."
            if summary.rate_limit_percent is not None
            else f"HTTP 429 responses per bucket. {_number(summary.rate_limited_requests)} total events; "
            "the rate is withheld because request totals are incomplete."
            if summary.rate_limited_requests is not None
            else "Request-outcome telemetry was not provided by this source, so 429 events cannot be shown."
        ),
        "series": series,
        "primary": 6,
        "geometry": _GEOMETRY,
        "x_kind": "time",
        "y_kind": "number",
        "x_title": "Bucket (UTC)",
        "x_label": lambda value: _iso(datetime.fromtimestamp(float(value), tz=UTC)),
        "y_format": lambda value: f"{value:,.0f}",
        "point_label": lambda row: _iso(datetime.fromtimestamp(float(row[0]), tz=UTC)),
    }


def _evidence_rows(data: PtuDashboardData) -> list[list[float | None]]:
    return [
        [
            _epoch(point.timestamp),
            point.input_tokens,
            point.cached_tokens,
            point.output_tokens,
            point.total_tokens,
            point.successful_requests,
            point.rate_limited_requests,
            point.failed_requests,
            point.total_requests,
        ]
        for point in data.evidence
    ]


def _cost_rows(data: PtuDashboardData) -> list[list[float | None]]:
    return [
        [
            _epoch(datetime(point.date.year, point.date.month, point.date.day, tzinfo=UTC)),
            point.input_cost,
            point.cached_input_cost,
            point.output_cost,
            point.total_cost,
            point.pricing_coverage_tokens_percent,
            point.total_tokens,
            point.unpriced_tokens,
        ]
        for point in data.daily_cost
    ]


def _glance_cards(data: PtuDashboardData) -> str:
    summary = data.summary
    partial = " · partial window" if summary.partial_days else ""
    cards = [
        (
            "Scope",
            summary.deployment_name,
            f"{summary.model_name} · {summary.deployment_mode.title()} deployment",
            None,
        ),
        (
            "Typical Throughput",
            _number(summary.average_weighted_tpm),
            f"weighted tokens / min avg · {summary.weighted_basis}",
            "Weighted TPM applies the model's output-token weighting so a PTU size can be compared against observed demand.",
        ),
        (
            "Busy-Hour Throughput",
            _number(summary.p95_weighted_tpm),
            "P95 weighted tokens / min",
            "P95 is the busy-hour reference: 95% of observed buckets were at or below this throughput.",
        ),
        (
            "Total Tokens",
            _number(summary.total_tokens),
            f"{_number(summary.total_input_tokens)} input · {_number(summary.total_cached_tokens)} cached · {_number(summary.total_output_tokens)} output",
            None,
        ),
        (
            "Daily Average Tokens",
            _number(summary.daily_average_tokens),
            f"{summary.complete_days} complete · {summary.partial_days} partial day(s){partial}",
            "The daily average divides observed tokens by observed days. Partial days are marked because they understate a full day.",
        ),
        (
            "Rate-Limit Events",
            f"{summary.rate_limit_percent:.2f}%"
            if summary.rate_limit_percent is not None
            else _number(summary.rate_limited_requests)
            if summary.rate_limited_requests is not None
            else "Unavailable",
            (
                f"{_number(summary.rate_limited_requests)} of {_number(summary.total_requests)} requests"
                if summary.rate_limit_percent is not None
                else "429 events observed · rate unavailable (incomplete request totals)"
                if summary.rate_limited_requests is not None
                else "Request-outcome metric unavailable"
            ),
            "The 429 rate is rate-limited requests divided by requests whose outcome and total the source reported.",
        ),
    ]
    rendered = []
    for label, value, sub, tip in cards:
        tooltip = (
            f'<span class="info-tip" tabindex="0" role="note" aria-label="{_escape(tip)}"><span aria-hidden="true">i</span>'
            f'<span class="tip-body">{_escape(tip)}</span></span>'
            if tip
            else ""
        )
        rendered.append(
            f'<div class="card glance-card"><div class="label">{_escape(label)}{tooltip}</div>'
            f'<div class="value glance-value">{_escape(value)}</div><div class="sub">{_escape(sub)}</div></div>'
        )
    return f'<section class="glance-grid" aria-label="At a glance">{"".join(rendered)}</section>'


def _banner(data: PtuDashboardData, slug: str) -> str:
    summary = data.summary
    confidence = (
        f'<span class="confidence-score">Confidence score: {summary.confidence_percent:.1f}%'
        f'<small>{_escape(summary.confidence_label or "")}</small></span>'
        if summary.confidence_percent is not None
        else f'<span class="confidence-score missing">Confidence withheld<small>{_escape(_missing_evidence_line(data))}</small></span>'
    )
    return f"""<section class="ptu-banner state-{_escape(summary.state)}" aria-label="Recommendation">
      <div class="banner-copy">
        <div class="banner-badges"><span class="state-badge">{_escape(DASHBOARD_STATE_LABELS[summary.state])}</span>{confidence}</div>
        <h2>Recommendation: {_escape(summary.recommendation)}</h2>
        <p>{_escape(summary.summary)}</p>
        <small>{_escape(summary.deployment_name)} · {_escape(summary.model_name)} · {_escape(summary.deployment_mode.title())} deployment
        · {_escape(_window_label(summary))}</small>
      </div>
      <div class="banner-actions" role="group" aria-label="Export report">
        <button type="button" class="chart-button primary" data-ptu-print="{_escape(slug)}">Export report (print / PDF)</button>
        <button type="button" class="chart-button" data-ptu-json="{_escape(slug)}">Download JSON</button>
      </div>
    </section>"""


def _window_label(summary) -> str:
    if summary.window_start is None or summary.window_end is None:
        return "No timestamped evidence"
    label = f"{_iso(summary.window_start)} to {_iso(summary.window_end)}"
    return f"{label} · smoke-test window" if summary.smoke_test_window else label


def _missing_evidence_line(data: PtuDashboardData) -> str:
    summary = data.summary
    if summary.state == "insufficient_evidence":
        return f"{summary.active_buckets:,} of {MINIMUM_ACTIVE_BUCKETS} active five-minute buckets observed"
    if summary.state == "pricing_unavailable":
        return "Exact PAYG pricing required"
    if summary.state == "capacity_unavailable":
        return "Exact model PTU capacity required"
    return "Azure PTU purchasing does not apply to this model"


def _list_panel(title: str, items: list[str], *, empty: str) -> str:
    body = "".join(f"<li>{_escape(item)}</li>" for item in items) or f'<li class="empty">{_escape(empty)}</li>'
    return f'<section class="panel rationale-panel"><h3>{_escape(title)}</h3><ul>{body}</ul></section>'


def _confidence_panel(data: PtuDashboardData) -> str:
    rows = "".join(
        f"<tr><th scope=\"row\">{_escape(item.name)}</th><td>{item.weight:.2f}</td>"
        f"<td>{item.score * 100:.1f}%</td><td>{_escape(item.detail)}</td></tr>"
        for item in data.confidence_components
    )
    score = (
        f"{data.summary.confidence_percent:.1f}% · {data.summary.confidence_label}"
        if data.summary.confidence_percent is not None
        else f"Withheld · {_missing_evidence_line(data)}"
    )
    return f"""<section class="panel rationale-panel"><h3>Confidence and data quality</h3>
      <p class="muted">Confidence measures the quality of the evidence behind the classification, not the probability that PTU saves money. Score: {_escape(score)}.</p>
      <div class="table-scroll"><table class="data-table"><caption>Deterministic confidence components</caption>
      <thead><tr><th scope="col">Component</th><th scope="col">Weight</th><th scope="col">Score</th><th scope="col">Evidence</th></tr></thead>
      <tbody>{rows or '<tr><td colspan="4">No components evaluated.</td></tr>'}</tbody></table></div>
      <ul>{''.join(f'<li>{_escape(item)}</li>' for item in data.data_quality_notes) or '<li class="empty">No data-quality limitations were detected.</li>'}</ul>
    </section>"""


def _payload_script(data: PtuDashboardData, slug: str) -> str:
    """Embed the typed payload as compact, aggregate-only JSON.

    Evidence is stored column-wise purely to keep the self-contained report
    small; the exported values are the exact analyzed numbers.
    """
    payload = {
        "schema": "tokenlens.ptu_dashboard/1",
        "summary": data.summary.model_dump(mode="json"),
        "evidence_columns": [
            "timestamp",
            "input_tokens",
            "cached_tokens",
            "output_tokens",
            "total_tokens",
            "successful_requests",
            "rate_limited_requests",
            "failed_requests",
            "total_requests",
        ],
        "evidence_rows": _evidence_rows(data),
        "daily_cost": [point.model_dump(mode="json") for point in data.daily_cost],
        "throughput": _compact_throughput(data.throughput),
        "cost_curve": data.cost_curve.model_dump(mode="json") if data.cost_curve else None,
        "confidence_components": [item.model_dump(mode="json") for item in data.confidence_components],
        "assumptions": data.assumptions,
        "missing_metrics": data.missing_metrics,
        "recommendation_reasons": data.recommendation_reasons,
        "what_would_change": data.what_would_change,
        "next_steps": data.next_steps,
        "data_quality_notes": data.data_quality_notes,
    }
    serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    # ``</script>`` cannot appear inside a JSON island; the payload is numeric
    # and template text only, but escape defensively anyway.
    serialized = serialized.replace("</", "<\\/")
    return f'<script type="application/json" data-ptu-payload="{_escape(slug)}">{serialized}</script>'


def _compact_throughput(series: PtuThroughputSeries | None) -> dict[str, Any] | None:
    if series is None:
        return None
    return {
        "bucket_minutes": series.bucket_minutes,
        "average_tpm": series.average_tpm,
        "reference_tpm": series.reference_tpm,
        "reference_label": series.reference_label,
        "ptu_capacity_tpm": series.ptu_capacity_tpm,
        "tpm": [point.tpm for point in series.points],
    }


def _explanation_body(label: str, explanation: str) -> str:
    """Avoid repeating the eligibility label that already prefixes the sentence."""
    prefix = f"{label}:"
    return explanation[len(prefix):].strip() if explanation.startswith(prefix) else explanation


def _collapsed_table(markup: str) -> str:
    """Keep long accessible tables available without dominating the layout."""
    if not markup:
        return ""
    return f'<details class="chart-table"><summary>Data table</summary><div class="table-scroll">{markup}</div></details>'


def _capacity_section(
    assessment: PtuDeploymentAssessment,
    throughput_svg: str,
    throughput_table: str,
    cost_svg: str,
    cost_table: str,
) -> str:
    if not throughput_svg and not cost_svg:
        return ""
    cards = []
    if throughput_svg:
        cards.append(
            '<article class="chart-card"><div class="chart-head"><div><h3>Weighted TPM and capacity</h3>'
            "<small>Five-minute weighted TPM with average, P95 busy-hour reference, and the selected PTU capacity</small></div></div>"
            f"{throughput_svg}{_collapsed_table(throughput_table)}</article>"
        )
    if cost_svg:
        difference = (
            assessment.payg_monthly_usd - assessment.hybrid_monthly_usd
            if assessment.payg_monthly_usd is not None and assessment.hybrid_monthly_usd is not None
            else None
        )
        label = (
            f"Monthly difference: {_money(abs(difference))} {'in favour of PTU' if difference and difference > 0 else 'in favour of PAYG'}"
            if difference is not None
            else "Monthly difference unavailable"
        )
        cards.append(
            '<article class="chart-card"><div class="chart-head"><div><h3>PAYG vs PTU + Spillover Cost Explorer</h3>'
            "<small>Monthly cost across sustained TPM with break-even, selected capacity, and current cost points</small></div>"
            f'<div class="chart-actions"><span class="chart-badge">{_escape(label)}</span></div></div>'
            f"{cost_svg}{_collapsed_table(cost_table)}</article>"
        )
    return f'<section class="chart-grid ptu-graphs" aria-label="Capacity and cost decision">{"".join(cards)}</section>'


def render_deployment(
    assessment: PtuDeploymentAssessment,
    slug: str,
    *,
    selected: bool,
    throughput_svg: str,
    throughput_table: str,
    cost_svg: str,
    cost_table: str,
    eligibility_label: str = "",
    eligibility_explanation: str = "",
) -> str:
    data = assessment.dashboard
    if data is None:
        return ""
    summary = data.summary
    evidence_rows = _evidence_rows(data)
    cost_rows = _cost_rows(data)
    cached_available = "cached_tokens" not in data.missing_metrics
    outcomes_available = "request_outcomes" not in data.missing_metrics
    priced = any(point.total_cost is not None for point in data.daily_cost)

    charts = [
        _chart_card(
            _tokens_spec(data, slug, cached_available),
            evidence_rows,
            state_note=None if evidence_rows else "No timestamped token evidence was observed for this deployment.",
        ),
        _chart_card(
            _outcomes_spec(data, slug),
            evidence_rows,
            state_note=(
                None
                if outcomes_available and evidence_rows
                else "Request-outcome metrics (success, HTTP 429, other failures) were not provided by this telemetry source."
            ),
        ),
        _chart_card(
            _cost_spec(data, slug),
            cost_rows,
            state_note=(
                None
                if priced
                else "Pricing required: no exact price resolved for this model and deployment mode, so no cost is plotted. "
                f"Observed tokens remain available as evidence ({_number(summary.total_tokens)} tokens across {len(data.daily_cost):,} day(s))."
            ),
        ),
        _chart_card(
            _rate_limit_spec(data, slug),
            evidence_rows,
            state_note=(
                None
                if outcomes_available and evidence_rows
                else "Metric unavailable: this telemetry source did not report request outcomes, so HTTP 429 events cannot be charted."
            ),
        ),
    ]
    partial_pricing = (
        f'<p class="pricing-partial">Partial estimate: pricing covers {summary.pricing_coverage_tokens_percent:.1f}% of observed tokens. '
        f"The plotted cost excludes {_number(sum(point.unpriced_tokens for point in data.daily_cost))} unpriced token(s) and is not the full deployment cost.</p>"
        if priced and summary.pricing_coverage_tokens_percent < 100
        else ""
    )
    return f"""<section class="ptu-deployment{' selected' if selected else ''}" id="ptu-deployment-{_escape(slug)}"
      data-ptu-deployment="{_escape(slug)}" data-deployment-name="{_escape(summary.deployment_name)}"{'' if selected else ' hidden'}>
      {_banner(data, slug)}
      {_glance_cards(data)}
      <p class="ptu-evidence-note"><strong>{_escape(eligibility_label)}</strong> {_escape(_explanation_body(eligibility_label, eligibility_explanation))} {_escape(assessment.note)}</p>
      {partial_pricing}
      <section class="chart-grid evidence-grid" aria-label="Evidence">{''.join(charts)}</section>
      {_capacity_section(assessment, throughput_svg, throughput_table, cost_svg, cost_table)}
      <section class="rationale-grid">
        {_list_panel("Why this recommendation", data.recommendation_reasons, empty="No scored dimensions are available yet.")}
        {_list_panel("What would change it", data.what_would_change, empty="No change conditions apply.")}
        {_list_panel("Assumptions", data.assumptions, empty="No assumptions recorded.")}
        {_confidence_panel(data)}
        {_list_panel("Recommended next steps", data.next_steps, empty="No next steps.")}
      </section>
      {_payload_script(data, slug)}
    </section>"""


def deployment_slugs(analysis: PtuPortfolioAssessment) -> list[tuple[PtuDeploymentAssessment, str]]:
    """Order deployments by observed token volume; the largest is the default."""
    ordered = sorted(
        analysis.deployments,
        key=lambda item: (item.dashboard.summary.total_tokens or 0) if item.dashboard else 0,
        reverse=True,
    )
    return [(item, _slug(item.deployment_name, index)) for index, item in enumerate(ordered)]


def selector(pairs: list[tuple[PtuDeploymentAssessment, str]]) -> str:
    if len(pairs) < 2:
        return ""
    options = "".join(
        f'<option value="{_escape(slug)}"{" selected" if index == 0 else ""}>'
        f'{_escape(item.deployment_name)} — {_escape(item.model_name)}</option>'
        for index, (item, slug) in enumerate(pairs)
    )
    return f"""<div class="ptu-selector"><label for="ptu-deployment-select">Deployment</label>
      <select id="ptu-deployment-select" data-ptu-select>{options}</select>
      <small>Every recommendation, metric, chart, and export below applies to the selected deployment only.</small></div>"""


PTU_DASHBOARD_CSS = """
.ptu-selector{display:flex;flex-wrap:wrap;align-items:center;gap:9px;margin:12px 0}
.ptu-selector label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.ptu-selector select{background:var(--surface-2);color:var(--text);border:1px solid var(--border);border-radius:7px;padding:6px 9px;font-size:13px;min-width:min(320px,80vw)}
.ptu-selector small{flex:1 1 240px;color:var(--muted);font-size:12px}
.ptu-deployment[hidden]{display:none}
.ptu-banner{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-start;gap:14px;width:100%;padding:16px 18px;margin-bottom:14px;border:1px solid var(--border);border-left:6px solid var(--accent);border-radius:11px;background:var(--surface);box-shadow:var(--shadow)}
.ptu-banner.state-ptu_recommended{border-left-color:var(--green)}
.ptu-banner.state-borderline,.ptu-banner.state-pricing_unavailable{border-left-color:var(--amber)}
.ptu-banner.state-payg_recommended{border-left-color:var(--accent)}
.ptu-banner.state-insufficient_evidence,.ptu-banner.state-capacity_unavailable,.ptu-banner.state-ptu_not_applicable{border-left-color:var(--pink)}
.banner-copy{flex:1 1 420px;display:grid;gap:6px}
.banner-badges{display:flex;flex-wrap:wrap;align-items:center;gap:9px}
.state-badge{font-size:12px;font-weight:700;padding:3px 9px;border-radius:6px;background:var(--surface-2);border:1px solid var(--border)}
.state-ptu_recommended .state-badge{color:var(--green)}
.state-borderline .state-badge,.state-pricing_unavailable .state-badge{color:var(--amber)}
.state-payg_recommended .state-badge{color:var(--accent)}
.state-insufficient_evidence .state-badge,.state-capacity_unavailable .state-badge,.state-ptu_not_applicable .state-badge{color:var(--pink)}
.confidence-score{font-size:12px;color:var(--soft)}
.confidence-score small{display:inline;margin-left:6px;color:var(--muted)}
.confidence-score.missing{color:var(--amber)}
.ptu-banner h2{font-size:17px}
.ptu-banner p{font-size:13px;color:var(--soft)}
.banner-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.chart-button{font:inherit;font-size:12px;color:var(--text);background:var(--surface-2);border:1px solid var(--border);border-radius:7px;padding:6px 11px;cursor:pointer}
.chart-button:hover{border-color:var(--accent)}
.chart-button.primary{border-color:var(--accent);color:var(--accent)}
.chart-button:focus-visible,.ptu-selector select:focus-visible,.chart-range input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.glance-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:14px}
.glance-card .label{display:flex;align-items:center;gap:6px}
.glance-value{font-size:22px;line-height:1.15;overflow-wrap:anywhere}
.info-tip{position:relative;display:inline-flex;align-items:center;justify-content:center;width:15px;height:15px;border-radius:50%;border:1px solid var(--border);background:var(--surface-2);color:var(--muted);font-size:10px;cursor:help}
.info-tip .tip-body{display:none;position:absolute;z-index:6;top:130%;left:0;width:max(220px,14vw);padding:8px 10px;border:1px solid var(--border);border-radius:7px;background:var(--surface-2);color:var(--soft);font-size:11px;text-transform:none;letter-spacing:0;box-shadow:var(--shadow)}
.info-tip:hover .tip-body,.info-tip:focus .tip-body,.info-tip:focus-within .tip-body{display:block}
.evidence-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.ptu-chart{display:flex;flex-direction:column;gap:8px;min-width:0}
.chart-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap}
.chart-head h3{font-size:14px}
.chart-actions{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.chart-badge{font-size:11px;font-weight:700;color:var(--soft);background:var(--surface-2);border:1px solid var(--border);border-radius:6px;padding:3px 8px}
.ptu-chart-svg{width:100%;height:auto;display:block}
.chart-range{display:flex;flex-wrap:wrap;align-items:center;gap:9px;font-size:11px;color:var(--muted)}
.chart-range label{display:flex;align-items:center;gap:5px}
.chart-range input[type=range]{width:min(170px,32vw);accent-color:var(--accent)}
.range-readout{font-size:11px;color:var(--soft)}
.chart-summary{font-size:11px;color:var(--muted)}
.chart-unavailable{font-size:12px;color:var(--amber);padding:12px;border:1px dashed var(--border);border-radius:8px;background:var(--surface-2)}
.chart-table summary{font-size:11px;color:var(--accent);cursor:pointer}
.table-scroll{max-width:100%;overflow-x:auto}
.glance-grid>*,.evidence-grid>*,.rationale-grid>*,.ptu-graphs>*{min-width:0}
.ptu-deployment table,.ptu-portfolio table{min-width:0}
.ptu-deployment .data-table,.ptu-portfolio .data-table{table-layout:auto}
.pricing-partial{font-size:12px;color:var(--amber);margin-bottom:12px;padding:10px 12px;border:1px dashed var(--border);border-radius:8px;background:var(--surface-2)}
.rationale-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:14px}
.rationale-panel{padding:14px}
.rationale-panel h3{font-size:14px;margin-bottom:7px}
.rationale-panel ul{margin:0;padding-left:18px;display:grid;gap:5px}
.rationale-panel li{font-size:13px;color:var(--soft)}
.rationale-panel li.empty{color:var(--muted);list-style:none;margin-left:-18px}
.ptu-modal{position:fixed;inset:0;z-index:20;display:none;align-items:center;justify-content:center;padding:clamp(10px,3vw,40px);background:rgba(5,12,24,.78)}
.ptu-modal.open{display:flex}
.ptu-modal-inner{width:min(1200px,100%);max-height:92vh;overflow:auto;background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px}
.ptu-modal-close{position:sticky;top:0;float:right}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
@media(max-width:1180px){.glance-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:900px){.evidence-grid,.rationale-grid{grid-template-columns:1fr}}
@media(max-width:620px){.glance-grid{grid-template-columns:1fr}.ptu-banner{flex-direction:column}.banner-actions{width:100%}.chart-range input[type=range]{width:100%}.chart-range label{flex:1 1 100%}}
@media print{
  body{background:#fff;color:#000}
  .tab-list,.ptu-selector,.banner-actions,.chart-range,.chart-button{display:none!important}
  .tab-panel{display:none!important}
  .tab-panel.ptu,body.ptu-print .tab-panel.ptu{display:block!important}
  .ptu-deployment[hidden]{display:none!important}
  .evidence-grid,.rationale-grid,.ptu-graphs{grid-template-columns:1fr!important}
  .chart-card,.panel,.card,.ptu-banner{break-inside:avoid;box-shadow:none}
  .chart-table[open] .table-scroll,.chart-table{display:block}
  .ptu-chart-svg{max-width:100%}
}
"""

PTU_DASHBOARD_JS = """
(function(){
  var panel = document.getElementById("ptu-panel");
  if (!panel) return;
  var payloads = {};
  panel.querySelectorAll("[data-ptu-payload]").forEach(function(node){
    try { payloads[node.dataset.ptuPayload] = JSON.parse(node.textContent); } catch (error) { /* payload stays unavailable */ }
  });
  var ranges = {};

  function fmt(kind, value, currency){
    var number = Number(value);
    if (kind === "money") return (currency === "USD" ? "$" : currency + " ") + number.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2});
    if (kind === "requests") return number.toLocaleString() + " requests";
    if (kind === "events") return number.toLocaleString() + " events";
    return number.toLocaleString() + " tokens";
  }
  function stamp(kind, value){
    var when = new Date(Number(value) * 1000);
    if (kind === "date") return when.toISOString().slice(0, 10);
    return when.toISOString().slice(0, 16).replace("T", " ") + " UTC";
  }
  function lttb(rows, threshold, primary){
    var count = rows.length;
    if (threshold >= count || threshold < 3) return rows;
    var sampled = [rows[0]], every = (count - 2) / (threshold - 2), a = 0;
    for (var i = 0; i < threshold - 2; i++) {
      var start = Math.floor((i + 1) * every) + 1, end = Math.min(Math.floor((i + 2) * every) + 1, count);
      var bucket = rows.slice(start, end); if (!bucket.length) bucket = [rows[count - 1]];
      var avgX = 0, avgY = 0;
      bucket.forEach(function(row){ avgX += Number(row[0]); avgY += Number(row[primary] || 0); });
      avgX /= bucket.length; avgY /= bucket.length;
      var rangeStart = Math.floor(i * every) + 1, rangeEnd = Math.min(Math.floor((i + 1) * every) + 1, count);
      var best = null, bestArea = -1, bestIndex = rangeStart;
      for (var j = rangeStart; j < rangeEnd; j++) {
        var area = Math.abs((Number(rows[a][0]) - avgX) * (Number(rows[j][primary] || 0) - Number(rows[a][primary] || 0))
          - (Number(rows[a][0]) - Number(rows[j][0])) * (avgY - Number(rows[a][primary] || 0)));
        if (area > bestArea) { best = rows[j]; bestArea = area; bestIndex = j; }
      }
      sampled.push(best || rows[rangeStart]); a = bestIndex;
    }
    sampled.push(rows[count - 1]);
    return sampled;
  }
  function escapeText(value){
    return String(value).replace(/[&<>"]/g, function(character){
      return {"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"}[character];
    });
  }
  function draw(spec, rows){
    var g = spec.geometry, left = g.l, right = g.r, top = g.t, bottom = g.b;
    if (!rows.length) {
      return '<text x="' + ((left + right) / 2) + '" y="' + ((top + bottom) / 2) + '" text-anchor="middle" class="axis-label">No observations in the selected range</text>';
    }
    var display = lttb(rows, 360, spec.primary);
    var xs = display.map(function(row){ return Number(row[0]); });
    var minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs), span = (maxX - minX) || 1;
    var maxY = 1;
    display.forEach(function(row){
      var stackTotal = 0;
      spec.series.forEach(function(item){
        var value = row[item.index];
        if (value === null || value === undefined) return;
        if (item.stack) stackTotal += Number(value); else maxY = Math.max(maxY, Number(value));
      });
      maxY = Math.max(maxY, stackTotal);
    });
    var width = right - left;
    function sx(value){ return left + (value - minX) / span * width; }
    function sy(value){ return bottom - Math.max(0, value) / maxY * (bottom - top); }
    var parts = [];
    var columns = spec.series.filter(function(item){ return item.stack; });
    if (columns.length) {
      var slot = Math.max(2, width / Math.max(1, display.length) * 0.62);
      display.forEach(function(row){
        var base = bottom, x = sx(Number(row[0])) - slot / 2;
        columns.forEach(function(item){
          var value = row[item.index];
          if (!value) return;
          var height = Number(value) / maxY * (bottom - top);
          base -= height;
          var label = stamp(spec.x, row[0]) + " · " + item.label + ": " + fmt(item.fmt, value, spec.currency);
          parts.push('<rect x="' + x.toFixed(1) + '" y="' + base.toFixed(1) + '" width="' + slot.toFixed(1) + '" height="' + height.toFixed(1)
            + '" fill="' + item.color + '" tabindex="0" role="img" aria-label="' + escapeText(label) + '"><title>' + escapeText(label) + '</title></rect>');
        });
      });
    }
    spec.series.forEach(function(item){
      if (item.stack) return;
      var points = [];
      display.forEach(function(row){
        var value = row[item.index];
        if (value === null || value === undefined) return;
        points.push(sx(Number(row[0])).toFixed(1) + "," + sy(Number(value)).toFixed(1));
      });
      if (!points.length) return;
      parts.push('<polyline points="' + points.join(" ") + '" fill="none" stroke="' + item.color + '" stroke-width="' + item.width
        + '"' + (item.dashed ? ' stroke-dasharray="6 4"' : "") + "></polyline>");
    });
    var marker = spec.series.filter(function(item){ return !item.stack; })[0];
    if (marker) {
      var stride = Math.max(1, Math.floor(display.length / 18));
      for (var i = 0; i < display.length; i += stride) {
        var row = display[i], value = row[marker.index];
        if (value === null || value === undefined) continue;
        var label = stamp(spec.x, row[0]) + " · " + marker.label + ": " + fmt(marker.fmt, value, spec.currency);
        parts.push('<circle cx="' + sx(Number(row[0])).toFixed(1) + '" cy="' + sy(Number(value)).toFixed(1) + '" r="3.2" fill="' + marker.color
          + '" tabindex="0" role="img" aria-label="' + escapeText(label) + '"><title>' + escapeText(label) + '</title></circle>');
      }
    }
    (spec.references || []).forEach(function(reference){
      var y = sy(Number(reference.value));
      parts.push('<line x1="' + left + '" y1="' + y.toFixed(1) + '" x2="' + right + '" y2="' + y.toFixed(1) + '" stroke="' + reference.color
        + '" stroke-width="1.2" stroke-dasharray="4 4"></line><text x="' + right + '" y="' + (y - 5).toFixed(1)
        + '" text-anchor="end" class="axis-value">' + escapeText(reference.label) + "</text>");
    });
    parts.push('<text x="' + left + '" y="' + (bottom + 18) + '" class="axis-label">' + escapeText(stamp(spec.x, minX)) + "</text>");
    parts.push('<text x="' + right + '" y="' + (bottom + 18) + '" text-anchor="end" class="axis-label">' + escapeText(stamp(spec.x, maxX)) + "</text>");
    parts.push('<text x="' + (left - 8) + '" y="' + (top + 6) + '" text-anchor="end" class="axis-value">' + escapeText(fmt(spec.yfmt === "money" ? "money" : "plain", maxY, spec.currency).replace(" tokens", "")) + "</text>");
    parts.push('<text x="' + (left - 8) + '" y="' + bottom + '" text-anchor="end" class="axis-value">0</text>');
    return parts.join("");
  }
  function rowsFor(slug, spec){
    var payload = payloads[slug];
    if (!payload) return [];
    if (spec.source === "cost") {
      return (payload.daily_cost || []).map(function(point){
        return [Date.parse(point.date + "T00:00:00Z") / 1000, point.input_cost, point.cached_input_cost, point.output_cost, point.total_cost];
      });
    }
    return payload.evidence_rows || [];
  }
  function apply(slug){
    var range = ranges[slug] || {start: 0, end: 1000};
    var section = panel.querySelector('[data-ptu-deployment="' + slug + '"]');
    if (!section) return;
    var bounds = null;
    section.querySelectorAll("[data-ptu-spec]").forEach(function(card){
      var spec = JSON.parse(card.dataset.ptuSpec);
      var rows = rowsFor(slug, spec);
      if (!rows.length) return;
      var xs = rows.map(function(row){ return Number(row[0]); });
      var minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs);
      if (!bounds) bounds = {min: minX, max: maxX};
      bounds.min = Math.min(bounds.min, minX);
      bounds.max = Math.max(bounds.max, maxX);
    });
    if (!bounds) return;
    var lower = bounds.min + (bounds.max - bounds.min) * (range.start / 1000);
    var upper = bounds.min + (bounds.max - bounds.min) * (range.end / 1000);
    section.querySelectorAll("[data-ptu-spec]").forEach(function(card){
      var spec = JSON.parse(card.dataset.ptuSpec);
      var layer = card.querySelector("[data-series-layer]");
      if (!layer) return;
      var rows = rowsFor(slug, spec).filter(function(row){
        var x = Number(row[0]);
        return x >= lower - 1 && x <= upper + 1;
      });
      layer.innerHTML = draw(spec, rows);
      card.querySelectorAll("[data-ptu-range]").forEach(function(input){
        input.value = input.dataset.ptuRange === "start" ? range.start : range.end;
      });
      var readout = card.querySelector("[data-ptu-range-readout]");
      if (readout) {
        readout.textContent = (range.start === 0 && range.end === 1000)
          ? "Full observed window · " + rows.length.toLocaleString() + " point(s)"
          : stamp(spec.x, lower) + " to " + stamp(spec.x, upper) + " · " + rows.length.toLocaleString() + " point(s)"
            + (rows.length > 360 ? " · display downsampled" : "");
      }
    });
  }
  panel.addEventListener("input", function(event){
    var input = event.target.closest ? event.target.closest("[data-ptu-range]") : null;
    if (!input) return;
    var section = input.closest("[data-ptu-deployment]");
    if (!section) return;
    var slug = section.dataset.ptuDeployment;
    var range = ranges[slug] || {start: 0, end: 1000};
    var value = Number(input.value);
    if (input.dataset.ptuRange === "start") range.start = Math.min(value, range.end - 1);
    else range.end = Math.max(value, range.start + 1);
    ranges[slug] = range;
    apply(slug);
  });
  panel.addEventListener("click", function(event){
    var target = event.target.closest ? event.target : null;
    if (!target) return;
    var reset = target.closest("[data-ptu-reset]");
    if (reset) {
      var section = reset.closest("[data-ptu-deployment]");
      ranges[section.dataset.ptuDeployment] = {start: 0, end: 1000};
      apply(section.dataset.ptuDeployment);
      return;
    }
    var expand = target.closest("[data-ptu-expand]");
    if (expand) { openModal(expand); return; }
    var print = target.closest("[data-ptu-print]");
    if (print) {
      document.body.classList.add("ptu-print");
      window.addEventListener("afterprint", function once(){ document.body.classList.remove("ptu-print"); window.removeEventListener("afterprint", once); });
      window.print();
      return;
    }
    var download = target.closest("[data-ptu-json]");
    if (download) { exportJson(download.dataset.ptuJson); return; }
  });
  var modal, modalBody, lastFocus, placeholder;
  function ensureModal(){
    if (modal) return modal;
    modal = document.createElement("div");
    modal.className = "ptu-modal";
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");
    modal.setAttribute("aria-label", "Expanded chart");
    modal.innerHTML = '<div class="ptu-modal-inner"><button type="button" class="chart-button ptu-modal-close">Close</button><div data-modal-body></div></div>';
    document.body.appendChild(modal);
    modalBody = modal.querySelector("[data-modal-body]");
    modal.querySelector(".ptu-modal-close").addEventListener("click", closeModal);
    modal.addEventListener("click", function(event){ if (event.target === modal) closeModal(); });
    document.addEventListener("keydown", function(event){ if (event.key === "Escape" && modal.classList.contains("open")) closeModal(); });
    return modal;
  }
  function openModal(button){
    var card = document.getElementById(button.dataset.ptuExpand);
    if (!card) return;
    ensureModal();
    lastFocus = button;
    placeholder = document.createElement("div");
    placeholder.setAttribute("data-modal-placeholder", "");
    card.parentNode.insertBefore(placeholder, card);
    modalBody.appendChild(card);
    modal.classList.add("open");
    modal.querySelector(".ptu-modal-close").focus();
  }
  function closeModal(){
    if (!modal || !modal.classList.contains("open")) return;
    var card = modalBody.firstElementChild;
    if (card && placeholder && placeholder.parentNode) placeholder.parentNode.replaceChild(card, placeholder);
    modal.classList.remove("open");
    if (lastFocus) lastFocus.focus();
  }
  function exportJson(slug){
    var payload = payloads[slug];
    if (!payload) return;
    var evidence = (payload.evidence_rows || []).map(function(row){
      var item = {};
      (payload.evidence_columns || []).forEach(function(name, index){
        item[name] = name === "timestamp" ? new Date(Number(row[index]) * 1000).toISOString() : row[index];
      });
      return item;
    });
    var exported = {
      schema: payload.schema,
      generated_by: "TokenLens for Azure",
      summary: payload.summary,
      evidence: evidence,
      daily_cost: payload.daily_cost,
      throughput: payload.throughput,
      cost_curve: payload.cost_curve,
      confidence_components: payload.confidence_components,
      assumptions: payload.assumptions,
      missing_metrics: payload.missing_metrics,
      recommendation_reasons: payload.recommendation_reasons,
      what_would_change: payload.what_would_change,
      next_steps: payload.next_steps,
      data_quality_notes: payload.data_quality_notes
    };
    var blob = new Blob([JSON.stringify(exported, null, 2)], {type: "application/json"});
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "tokenlens-ptu-" + slug + ".json";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(function(){ URL.revokeObjectURL(link.href); }, 0);
  }
  function select(slug, moveFocus){
    var sections = panel.querySelectorAll("[data-ptu-deployment]");
    var matched = false;
    sections.forEach(function(section){
      var active = section.dataset.ptuDeployment === slug;
      section.hidden = !active;
      section.classList.toggle("selected", active);
      if (active) matched = true;
    });
    if (!matched && sections.length) {
      sections[0].hidden = false;
      sections[0].classList.add("selected");
      slug = sections[0].dataset.ptuDeployment;
    }
    var picker = panel.querySelector("[data-ptu-select]");
    if (picker && picker.value !== slug) picker.value = slug;
    if (history.replaceState) history.replaceState(null, "", "#ptu=" + slug);
    apply(slug);
    if (moveFocus && picker) picker.focus();
  }
  var picker = panel.querySelector("[data-ptu-select]");
  if (picker) picker.addEventListener("change", function(){ select(picker.value, false); });
  var initial = (location.hash.match(/^#ptu=(.+)$/) || [])[1];
  var first = panel.querySelector("[data-ptu-deployment]");
  if (first) select(initial ? decodeURIComponent(initial) : first.dataset.ptuDeployment, false);
})();
"""
