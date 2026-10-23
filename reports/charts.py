"""Plotly figure helpers with one house style.

Figures are drawn with light-mode colors and a transparent background. ``static/site.js`` re-inks
fonts, gridlines and series (via ``trace.meta.slot``) from the page's CSS tokens, so the same HTML
reads correctly in light and dark mode. Chart titles and captions live in the HTML card, not the
figure, so they stay real text for screen readers.
"""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd
import plotly.graph_objects as go

# Categorical slots (validated reference palette, light steps). Dark steps live in site.css.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
INK_2, INK_3, GRID, AXIS, SURFACE = "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"
FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'

CONFIG = {
    "displaylogo": False,
    "responsive": True,
    "modeBarButtonsToRemove": ["select2d", "lasso2d", "autoScale2d", "zoomIn2d", "zoomOut2d"],
    "toImageButtonOptions": {"format": "png", "scale": 2},
}


def layout(fig: go.Figure, *, yfmt: str = ",.0f", ysuffix: str = "", height: int = 320,
           legend: bool = True, xfmt: str | None = None) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=12, t=8, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, size=12, color=INK_2),
        hovermode="x unified",
        hoverlabel=dict(bgcolor=SURFACE, bordercolor=AXIS, font=dict(color="#0b0b0b", family=FONT)),
        showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0, title=None, traceorder="normal"),
        bargap=0.25,
        bargroupgap=0.08,
    )
    fig.update_xaxes(showgrid=False, linecolor=AXIS, ticks="", automargin=True, tickformat=xfmt)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor=AXIS, tickformat=yfmt, ticksuffix=ysuffix,
                     automargin=True, rangemode="tozero")
    return fig


def _slot(i: int) -> dict:
    return {"slot": (i % len(SERIES)) + 1}


def lines(df: pd.DataFrame, x: str, ys: dict[str, str], *, yfmt: str = ",.0f", hover: str = ",.0f",
          ref: tuple[float, str] | None = None, height: int = 320) -> go.Figure:
    """One line per column in ``ys`` ({column: label}), shared y-axis (same unit only)."""
    fig = go.Figure()
    for i, (col, label) in enumerate(ys.items()):
        fig.add_trace(go.Scatter(
            x=df[x], y=df[col], name=label, mode="lines", meta=_slot(i),
            line=dict(color=SERIES[i], width=2), hovertemplate=f"%{{y:{hover}}}<extra>{label}</extra>",
        ))
    if ref is not None:
        value, text = ref
        fig.add_hline(y=value, line=dict(color=INK_3, width=1, dash="dot"), name="ref",
                      annotation=dict(text=text, font=dict(color=INK_3, size=11), xanchor="left", x=0))
    return layout(fig, yfmt=yfmt, height=height, legend=len(ys) > 1)


def bars(categories: Sequence, series: dict[str, Sequence[float]], *, yfmt: str = ",.0f", hover: str = ",.0f",
         stacked: bool = False, horizontal: bool = False, height: int = 320,
         ref: tuple[float, str] | None = None, slots: Sequence[int] | None = None) -> go.Figure:
    """Grouped (or stacked) bars; one trace per entry of ``series``."""
    fig = go.Figure()
    for i, (label, values) in enumerate(series.items()):
        slot = slots[i] if slots else i
        kw = dict(y=list(categories), x=list(values), orientation="h") if horizontal else dict(
            x=list(categories), y=list(values))
        fig.add_trace(go.Bar(
            name=label, meta=_slot(slot), marker=dict(color=SERIES[slot % len(SERIES)],
                                                      line=dict(color=SURFACE, width=2),
                                                      cornerradius=4),
            hovertemplate=f"%{{{'x' if horizontal else 'y'}:{hover}}}<extra>{label}</extra>", **kw,
        ))
    if stacked:
        fig.update_layout(barmode="stack")
    if ref is not None:
        value, text = ref
        shape = fig.add_vline if horizontal else fig.add_hline
        shape(value, line=dict(color=INK_3, width=1, dash="dot"), name="ref",
              annotation=dict(text=text, font=dict(color=INK_3, size=11)))
    fig = layout(fig, yfmt=yfmt, height=height, legend=len(series) > 1)
    fig.update_layout(hovermode="y unified" if horizontal else "x unified")
    if horizontal:
        fig.update_xaxes(tickformat=yfmt, gridcolor=GRID, showgrid=True)
        fig.update_yaxes(tickformat=None, showgrid=False, autorange="reversed", rangemode=None)
    return fig


def to_div(fig: go.Figure, div_id: str, label: str) -> str:
    html = fig.to_html(full_html=False, include_plotlyjs=False, config=CONFIG, div_id=div_id)
    return f'<div class="chart" role="img" aria-label="{label}">{html}</div>'
