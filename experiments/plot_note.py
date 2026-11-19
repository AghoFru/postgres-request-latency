"""Draw the measurement-note figures from results/note/summary.json.

Usage: uv run --no-project --with matplotlib python experiments/plot_note.py
"""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

PROJECT = Path(__file__).resolve().parent.parent
SUMMARY = PROJECT / "results" / "note" / "summary.json"
FIGURES = PROJECT / "paper" / "figures"
BLUE, ORANGE = "#2a78d6", "#eb6834"
INK, MUTED, GRID = "#1f1f1e", "#6b6a64", "#e4e3dd"


def style(axis):
    axis.grid(color=GRID, linewidth=0.6)
    axis.set_axisbelow(True)
    for side in ("top", "right"):
        axis.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        axis.spines[side].set_color(MUTED)
    axis.tick_params(colors=MUTED, labelsize=8)


def fidelity_figure(summary):
    rows = summary["fidelity"]["rows"]
    series = [
        ("TCP echo probe", BLUE, "o",
         lambda r: r["probe"] == "tcp_echo" and r["arm"] == "default"
         and r["method"] == "container" and r["gap_ms"] == "0"),
        ("Rails SELECT 1", ORANGE, "s",
         lambda r: r["probe"] == "rails_select1" and r["method"] == "container"),
    ]
    figure, axis = plt.subplots(figsize=(3.4, 2.8))
    style(axis)
    axis.plot([0.015, 2.5], [0.015, 2.5], color=MUTED, linewidth=1, linestyle="--",
              label="Added = configured")
    for name, color, marker, keep in series:
        points = sorted((float(r["rtt_ms"]), r["added_p50_ms"]) for r in rows
                        if keep(r) and r["rtt_ms"] not in ("none", "0"))
        axis.plot([x for x, _ in points], [y for _, y in points], color=color, marker=marker,
                  markersize=4, linewidth=1.5, label=name)
    axis.set_xscale("log")
    axis.set_yscale("log")
    axis.set_xlabel("Configured RTT (ms)", color=INK, fontsize=9)
    axis.set_ylabel("Added p50 RTT (ms)", color=INK, fontsize=9)
    axis.legend(fontsize=7, frameon=False)
    figure.tight_layout()
    figure.savefig(FIGURES / "netem_fidelity.pdf")


def slope_figure(summary):
    figure, axis = plt.subplots(figsize=(3.4, 2.8))
    style(axis)
    top = 0
    main_runs = {"redmine": "2026-11-18-redmine-rtt-sweep-pinned",
                 "aimeos": "2026-11-18-aimeos-rtt-sweep-pinned"}
    for (app, run), color, marker in zip(sorted(main_runs.items()), (BLUE, ORANGE), ("s", "o")):
        rows = summary["slope_vs_round_trips"][run]
        kept = [r for r in rows["fit_range"] if r["kind"] in ("path", "route")]
        xs = [r["round_trips_median_rtt0"] for r in kept]
        ys = [r["slope_ms_per_ms"] for r in kept]
        errors = [[y - r["slope_ci95"][0] for y, r in zip(ys, kept)],
                  [r["slope_ci95"][1] - y for y, r in zip(ys, kept)]]
        axis.errorbar(xs, ys, yerr=errors, fmt=marker, color=color, markersize=4,
                      elinewidth=1, capsize=2, label=app.capitalize())
        top = max(top, *xs, *ys)
    axis.plot([0, top * 1.05], [0, top * 1.05], color=MUTED, linewidth=1, linestyle="--",
              label="Slope = round trips")
    axis.set_xlabel("Traced round trips per request", color=INK, fontsize=9)
    axis.set_ylabel("Fitted p50 slope (ms per ms RTT)", color=INK, fontsize=9)
    axis.legend(fontsize=7, frameon=False)
    figure.tight_layout()
    figure.savefig(FIGURES / "slope_vs_round_trips.pdf")


def main():
    summary = json.loads(SUMMARY.read_text())
    FIGURES.mkdir(parents=True, exist_ok=True)
    fidelity_figure(summary)
    slope_figure(summary)
    print("Wrote paper/figures/netem_fidelity.pdf and paper/figures/slope_vs_round_trips.pdf")


if __name__ == "__main__":
    main()
