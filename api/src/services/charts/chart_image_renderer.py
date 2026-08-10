"""Server-side chart-to-PNG rendering using matplotlib.

Converts Chart.js / Recharts / Plotly-style JSON configs produced by the
agent into PNG bytes for channels that can't render interactive charts —
originally built for Slack (`files_upload_v2`), reused as-is for Telegram
(`send_photo`) and WhatsApp (Cloud API media upload) rather than building a
separate renderer per channel (§18 of the trading domain spec: "Do not
maintain two independent trading-data pipelines").

Also renders trading candlestick charts (`render_trading_chart_to_png`) —
the static-image fallback for the interactive klinecharts view used on web
(see web/lib/types/trading.ts for the shared `TradingChartData` shape this
mirrors on the Python side).
"""

from __future__ import annotations

import io
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Colour palette — matches the web UI primary teal + a set of complementary colours
_PALETTE = [
    "#0d9488",  # teal-600 (primary)
    "#6366f1",  # indigo-500
    "#f59e0b",  # amber-500
    "#ef4444",  # red-500
    "#10b981",  # emerald-500
    "#3b82f6",  # blue-500
    "#8b5cf6",  # violet-500
    "#f97316",  # orange-500
    "#ec4899",  # pink-500
    "#14b8a6",  # teal-500
]


def _get_colors(n: int) -> list[str]:
    return [_PALETTE[i % len(_PALETTE)] for i in range(n)]


def _setup_figure(title: str, figsize: tuple[float, float] = (10, 5)):
    """Create a styled figure + axes pair."""
    import matplotlib

    matplotlib.use("Agg")  # non-interactive backend — no display required
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=figsize)
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#f9fafb")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#e5e7eb")
    ax.spines["bottom"].set_color("#e5e7eb")
    ax.tick_params(colors="#6b7280", labelsize=9)
    ax.yaxis.grid(True, color="#e5e7eb", linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    if title:
        fig.suptitle(title, fontsize=13, fontweight="bold", color="#111827", y=0.98)
    return fig, ax


def _to_png(fig) -> bytes:
    import matplotlib.pyplot as plt

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ── Chart.js bar / line ───────────────────────────────────────────────────────


def _render_chartjs_bar(chart: dict[str, Any]) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    cfg = chart.get("config") or chart.get("data") or {}
    data = cfg.get("data") or chart.get("data") or {}
    labels = data.get("labels", [])
    datasets = data.get("datasets", [])
    title = chart.get("title", "")

    if not labels or not datasets:
        raise ValueError("No labels or datasets")

    n_groups = len(labels)
    n_series = len(datasets)
    colors = _get_colors(n_series)
    bar_width = 0.8 / max(n_series, 1)
    x = np.arange(n_groups)

    fig, ax = _setup_figure(title, figsize=(max(8, n_groups * 0.8 + 2), 5))

    for i, ds in enumerate(datasets):
        values = [float(v) if v is not None else 0 for v in ds.get("data", [])]
        offset = (i - (n_series - 1) / 2) * bar_width
        bars = ax.bar(
            x + offset, values, bar_width * 0.9, label=ds.get("label", f"Series {i + 1}"), color=colors[i], zorder=3
        )
        # Value labels on bars
        for bar, val in zip(bars, values, strict=False):
            if val != 0:
                ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_height(),
                    _compact_num(val),
                    ha="center",
                    va="bottom",
                    fontsize=8,
                    color="#374151",
                )

    ax.set_xticks(x)
    ax.set_xticklabels(
        [str(lb) for lb in labels],
        rotation=30 if n_groups > 6 else 0,
        ha="right" if n_groups > 6 else "center",
        fontsize=9,
    )
    if n_series > 1:
        ax.legend(fontsize=9, framealpha=0.7)

    fig.tight_layout()
    return _to_png(fig)


def _render_chartjs_line(chart: dict[str, Any]) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import numpy as np

    cfg = chart.get("config") or chart.get("data") or {}
    data = cfg.get("data") or chart.get("data") or {}
    labels = data.get("labels", [])
    datasets = data.get("datasets", [])
    title = chart.get("title", "")

    if not labels or not datasets:
        raise ValueError("No labels or datasets")

    colors = _get_colors(len(datasets))
    x = list(range(len(labels)))

    fig, ax = _setup_figure(title, figsize=(max(8, len(labels) * 0.6 + 2), 5))

    for i, ds in enumerate(datasets):
        values = [float(v) if v is not None else 0 for v in ds.get("data", [])]
        ax.plot(
            x,
            values,
            marker="o",
            markersize=5,
            linewidth=2,
            color=colors[i],
            label=ds.get("label", f"Series {i + 1}"),
            zorder=3,
        )
        ax.fill_between(x, values, alpha=0.08, color=colors[i])

    ax.set_xticks(x)
    ax.set_xticklabels(
        [str(lb) for lb in labels],
        rotation=30 if len(labels) > 6 else 0,
        ha="right" if len(labels) > 6 else "center",
        fontsize=9,
    )
    if len(datasets) > 1:
        ax.legend(fontsize=9, framealpha=0.7)

    fig.tight_layout()
    return _to_png(fig)


def _render_chartjs_pie(chart: dict[str, Any], donut: bool = False) -> bytes:
    import matplotlib

    matplotlib.use("Agg")

    cfg = chart.get("config") or chart.get("data") or {}
    data = cfg.get("data") or chart.get("data") or {}
    labels = data.get("labels", [])
    datasets = data.get("datasets", [])
    title = chart.get("title", "")

    if not datasets:
        raise ValueError("No datasets")

    values = [float(v) if v is not None else 0 for v in datasets[0].get("data", [])]
    colors = _get_colors(len(values))

    fig, ax = _setup_figure(title, figsize=(7, 5))
    ax.set_facecolor("#ffffff")
    ax.spines["left"].set_visible(False)
    ax.spines["bottom"].set_visible(False)
    ax.yaxis.set_visible(False)
    ax.xaxis.set_visible(False)
    ax.yaxis.grid(False)

    wedges, texts, autotexts = ax.pie(
        values,
        labels=labels,
        colors=colors,
        autopct="%1.1f%%",
        startangle=90,
        pctdistance=0.8,
        wedgeprops={"linewidth": 1.5, "edgecolor": "white"},
    )
    for t in texts:
        t.set_fontsize(9)
    for at in autotexts:
        at.set_fontsize(8)
        at.set_color("white")
        at.set_fontweight("bold")

    if donut:
        centre = __import__("matplotlib.patches", fromlist=["Circle"]).Circle((0, 0), 0.55, fc="white")
        ax.add_patch(centre)

    fig.tight_layout()
    return _to_png(fig)


# ── Trading candlestick (Telegram/WhatsApp static fallback for klinecharts) ──


def _render_candlestick(trading_chart: dict[str, Any]) -> bytes:
    """Render a TradingChartData-shaped dict (bars + overlays) to a static candlestick PNG.

    Mirrors web/lib/types/trading.ts's TradingChartData: {symbol, timeframe,
    source, bars: [{time,open,high,low,close,volume}], overlays: {entry,
    stopLoss, takeProfit[], supportResistance[], trendlines[]}}.
    """
    import matplotlib
    import matplotlib.dates as mdates

    matplotlib.use("Agg")
    from datetime import datetime

    from matplotlib.lines import Line2D
    from matplotlib.patches import Rectangle

    bars = trading_chart.get("bars") or []
    if not bars:
        raise ValueError("No bars in trading chart data")

    symbol = trading_chart.get("symbol", "")
    timeframe = trading_chart.get("timeframe", "")
    source = trading_chart.get("source", "")
    overlays = trading_chart.get("overlays") or {}

    times = [datetime.fromisoformat(str(b["time"]).replace("Z", "+00:00")) for b in bars]
    x = mdates.date2num(times)
    # Half a bar-width in x-axis units, for candle body/wick rendering.
    half_width = (x[1] - x[0]) * 0.35 if len(x) > 1 else 0.3

    fig, ax = _setup_figure(f"{symbol} · {timeframe}  ({source})", figsize=(11, 6))

    for xi, bar in zip(x, bars, strict=False):
        o, h, low, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        up = c >= o
        color = "#16a34a" if up else "#dc2626"
        ax.add_line(Line2D([xi, xi], [low, h], color=color, linewidth=1, zorder=2))
        body_bottom, body_height = (o, c - o) if up else (c, o - c)
        body_height = max(body_height, (h - low) * 0.01)  # ensure a visible sliver on doji bars
        ax.add_patch(
            Rectangle(
                (xi - half_width, body_bottom), half_width * 2, body_height, facecolor=color, edgecolor=color, zorder=3
            )
        )

    def _hline(value: float, label: str, color: str) -> None:
        ax.axhline(value, color=color, linestyle="--", linewidth=1, zorder=1)
        ax.text(x[-1], value, f" {label} {value:g}", color=color, fontsize=8, va="center", fontweight="bold")

    if overlays.get("entry") is not None:
        _hline(float(overlays["entry"]), "Entry", "#2563eb")
    if overlays.get("stopLoss") is not None:
        _hline(float(overlays["stopLoss"]), "SL", "#dc2626")
    for i, tp in enumerate(overlays.get("takeProfit") or []):
        _hline(float(tp), f"TP{i + 1}", "#16a34a")
    for level in overlays.get("supportResistance") or []:
        _hline(float(level), "S/R", "#9333ea")

    for tl in overlays.get("trendlines") or []:
        start, end = tl.get("start") or {}, tl.get("end") or {}
        if "time" not in start or "time" not in end:
            continue
        sx = mdates.date2num(datetime.fromisoformat(str(start["time"]).replace("Z", "+00:00")))
        ex = mdates.date2num(datetime.fromisoformat(str(end["time"]).replace("Z", "+00:00")))
        ax.add_line(
            Line2D(
                [sx, ex],
                [float(start["price"]), float(end["price"])],
                color=tl.get("color") or "#f59e0b",
                linewidth=1.5,
                zorder=2,
            )
        )

    ax.xaxis_date()
    fig.autofmt_xdate()
    ax.set_xlim(x[0] - half_width * 3, x[-1] + half_width * 8)  # leave room for overlay labels on the right

    fig.tight_layout()
    return _to_png(fig)


def render_trading_chart_to_png(trading_chart: dict[str, Any]) -> bytes | None:
    """Render a TradingChartData dict (candles + entry/SL/TP/S-R/trendline overlays) to PNG.

    Returns None on failure (e.g. malformed bar data) rather than raising —
    callers (Telegram/WhatsApp senders) should treat this the same as
    render_chart_to_png returning None: skip the image, never fabricate one.
    """
    try:
        return _render_candlestick(trading_chart)
    except Exception as e:
        logger.warning(f"[ChartImageRenderer] Failed to render trading chart '{trading_chart.get('symbol', '')}': {e}")
        return None


# ── Recharts / generic bar / line (table_data path) ──────────────────────────


def _render_from_table_data(chart: dict[str, Any]) -> bytes:
    """Fallback: render from table_data rows when the config format is unclear."""
    import matplotlib

    matplotlib.use("Agg")
    import numpy as np

    table_data = chart.get("table_data") or []
    if not table_data:
        raise ValueError("No table_data")

    title = chart.get("title", "")
    chart_type = (chart.get("chart_type") or "bar").lower()

    # Detect columns: first string column = x-axis labels, numeric cols = series
    sample = table_data[0] if table_data else {}
    x_col = None
    y_cols = []
    for k, v in sample.items():
        try:
            float(str(v).replace(",", ""))
            y_cols.append(k)
        except (ValueError, TypeError):
            if x_col is None:
                x_col = k

    if not y_cols:
        raise ValueError("No numeric columns in table_data")

    labels = (
        [str(row.get(x_col, i)) for i, row in enumerate(table_data)]
        if x_col
        else [str(i) for i in range(len(table_data))]
    )
    colors = _get_colors(len(y_cols))
    x = list(range(len(labels)))

    fig, ax = _setup_figure(title, figsize=(max(8, len(labels) * 0.8 + 2), 5))

    if chart_type in ("line", "area"):
        for i, col in enumerate(y_cols):
            vals = [float(str(row.get(col, 0)).replace(",", "")) for row in table_data]
            ax.plot(x, vals, marker="o", markersize=5, linewidth=2, color=colors[i], label=col, zorder=3)
            if chart_type == "area":
                ax.fill_between(x, vals, alpha=0.08, color=colors[i])
    else:  # bar (default)
        n_series = len(y_cols)
        bar_width = 0.8 / max(n_series, 1)
        for i, col in enumerate(y_cols):
            vals = [float(str(row.get(col, 0)).replace(",", "")) for row in table_data]
            offset = (i - (n_series - 1) / 2) * bar_width
            ax.bar(np.array(x) + offset, vals, bar_width * 0.9, label=col, color=colors[i], zorder=3)

    ax.set_xticks(x)
    ax.set_xticklabels(
        labels, rotation=30 if len(labels) > 6 else 0, ha="right" if len(labels) > 6 else "center", fontsize=9
    )
    if len(y_cols) > 1:
        ax.legend(fontsize=9, framealpha=0.7)

    fig.tight_layout()
    return _to_png(fig)


# ── Public API ────────────────────────────────────────────────────────────────


def _compact_num(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"{v / 1_000:.1f}K"
    if v == int(v):
        return str(int(v))
    return f"{v:.2f}"


def render_chart_to_png(chart: dict[str, Any]) -> bytes | None:
    """Render a chart dict to PNG bytes.

    Accepts two shapes:
      1. generate_chart_from_data / inline:
         { chart_type, library, title, data, config, table_data }
      2. internal_generate_chart SSE event:
         { chart_id, chart_type, library, chart_config, chart_data }

    Returns PNG bytes on success, None on failure.
    """
    try:
        # Normalise internal_generate_chart format → standard format
        # chart_config is a Chart.js config object: {"type":..., "data": {"labels":[], "datasets":[]}}
        # chart_data is the raw data: {"labels":[], "datasets":[]}
        if "chart_config" in chart or "chart_data" in chart:
            chart_config_raw = chart.get("chart_config") or {}
            chart_data_raw = chart.get("chart_data") or {}
            chart = {
                "chart_type": chart.get("chart_type") or chart_config_raw.get("type") or "bar",
                "library": chart.get("library") or "chartjs",
                "title": chart.get("title") or chart_config_raw.get("title") or "",
                # data may live at chart_data directly or nested inside chart_config.data
                "data": chart_data_raw
                if chart_data_raw.get("labels") or chart_data_raw.get("datasets")
                else chart_config_raw.get("data") or {},
                "config": chart_config_raw,
                "table_data": chart.get("table_data"),
            }

        library = (chart.get("library") or "chartjs").lower()
        chart_type = (chart.get("chart_type") or chart.get("type") or "bar").lower()
        title = chart.get("title", "")

        logger.info(f"[ChartRenderer] Rendering {library}/{chart_type}: '{title}'")

        # Chart.js types
        if library == "chartjs":
            if chart_type in ("bar", "horizontalbar"):
                return _render_chartjs_bar(chart)
            if chart_type == "line":
                return _render_chartjs_line(chart)
            if chart_type == "pie":
                return _render_chartjs_pie(chart, donut=False)
            if chart_type == "doughnut":
                return _render_chartjs_pie(chart, donut=True)
            if chart_type == "scatter":
                return _render_chartjs_line(chart)  # close enough

        # Recharts / Plotly / unknown — try table_data fallback
        if chart.get("table_data"):
            return _render_from_table_data(chart)

        # Last resort: treat data as Chart.js-like and try bar
        return _render_chartjs_bar(chart)

    except Exception as e:
        logger.warning(f"[ChartRenderer] Failed to render chart '{chart.get('title', '')}': {e}")
        return None
