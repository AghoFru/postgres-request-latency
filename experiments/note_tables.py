"""Write the paper's tables from results/note/summary.json and results/note/step.json.

Usage: uv run --no-project python experiments/note_tables.py
"""

import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SUMMARY = PROJECT / "results" / "note" / "summary.json"
STEP = PROJECT / "results" / "note" / "step.json"
TARGET = PROJECT / "paper" / "generated_tables.tex"
MAIN_SWEEPS = {"redmine": "2026-11-18-redmine-rtt-sweep-pinned",
               "aimeos": "2026-11-18-aimeos-rtt-sweep-pinned"}
CONTROL_RUNS = [
    ("Redmine", "all 11 settings, 20 runs", "2026-11-18-redmine-rtt-sweep-pinned"),
    ("Aimeos", "all 11 settings, 20 runs", "2026-11-18-aimeos-rtt-sweep-pinned"),
    ("Redmine", "control, default", "2026-11-18-redmine-control-pinned-default"),
    ("Redmine", "slow start after idle off", "2026-11-18-redmine-control-pinned-no-slow-start"),
    ("Redmine", "energy preference \\texttt{performance}",
     "2026-11-18-redmine-control-pinned-epp-performance"),
    ("Redmine", "deep idle states blocked", "2026-11-18-redmine-control-pinned-shallow-idle"),
]
STALL_TRACES = {"redmine": "2026-11-18-redmine-stall-trace",
                "aimeos": "2026-11-18-aimeos-stall-trace"}
LISTING = "/shop/search?f_sort={sort}&l_page={page}"
SLOPE_GROUPS = {
    "redmine": [
        ("route", "issue_list", "Issue list (4 offsets)"),
        ("route", "issue_detail", "Issue detail"),
        ("route", "project_list", "Project list"),
        ("all", "all", "Whole mix"),
    ],
    "aimeos": [
        *[("path", LISTING.format(sort=sort, page=page), f"Listing, page {page} ({sort})")
          for page in (0, 1, 2) for sort in ("price", "name")],
        ("route", "product_detail", "Product detail"),
        ("all", "all", "Whole mix"),
    ],
}
# A listing page has one count row. Both sort orders are traced, and main() checks that
# their counts agree before it writes the row.
TRIP_GROUPS = {
    "redmine": SLOPE_GROUPS["redmine"],
    "aimeos": [
        *[(("path", LISTING.format(sort="price", page=page)),
           ("path", LISTING.format(sort="name", page=page)),
           f"Listing, page {page} (price and name)") for page in (0, 1, 2)],
        ("route", "product_detail", "Product detail"),
        ("all", "all", "Whole mix"),
    ],
}


def tex(text):
    return text.replace("&", "\\&").replace("_", "\\_")


def find(items, kind, name):
    return next(item for item in items if item["kind"] == kind and item["name"] == name)


def trip_groups(trace, spec):
    """Return the trace groups for one table row. A tuple of keys must agree exactly."""
    if isinstance(spec[0], tuple):
        groups = [find(trace["groups"], *key) for key in spec[:-1]]
        fields = ("round_trips_by_rtt", "stalls_by_rtt")
        if any(g[f] != groups[0][f] for g in groups[1:] for f in fields):
            raise ValueError(f"Sort orders disagree for {spec[-1]}")
        return groups[0], None, spec[-1]
    kind, name, title = spec
    return find(trace["groups"], kind, name), (name if kind == "route" else None), title


def trip_rows(summary):
    traces = {t["run"]: t for t in summary["traces"]}
    rows = []
    for app in ("redmine", "aimeos"):
        trace = traces[STALL_TRACES[app]]
        for spec in TRIP_GROUPS[app]:
            group, route, title = trip_groups(trace, spec)
            trips = group["round_trips_by_rtt"]
            stalls = group["stalls_by_rtt"]
            statements = trace["statements_per_request"].get(route) if route else None
            rows.append(" & ".join([
                app.capitalize(), tex(title), str(trips["0"]["requests"]),
                f"{trips['0']['median']:g}", f"{trips['0']['min']} to {trips['0']['max']}",
                f"{trips['2']['median']:g}", f"{stalls['2']['median']:g}",
                "n/a" if statements is None else f"{statements:.1f}",
            ]) + r" \\")
    return rows


def slope_rows(summary):
    pairs = summary["slope_vs_round_trips"]
    rows = []
    for app in ("redmine", "aimeos"):
        sweep = next(s for s in summary["sweeps"] if s["run"] == MAIN_SWEEPS[app])
        for kind, name, title in SLOPE_GROUPS[app]:
            pair = find(pairs[MAIN_SWEEPS[app]]["all_netem_points"], kind, name)
            group = find(sweep["groups"], kind, name)
            fit = group["fits"]["all_netem_points"]
            p50 = {c["rtt_ms"]: c["p50_ms"] for c in group["conditions"]}
            rows.append(" & ".join([
                app.capitalize(), tex(title), f"{p50['0']:.1f}", f"{p50['2']:.1f}",
                f"{pair['round_trips_median_rtt0']:g}",
                (f"{fit['slope_ms_per_ms']:.1f} [{fit['slope_ci95'][0]:.1f}, "
                 f"{fit['slope_ci95'][1]:.1f}]"),
                f"{pair['slope_over_round_trips']:.2f}",
                f"{pair['slope_over_round_trips_plus_stalls']:.2f}", f"{fit['r_squared']:.3f}",
            ]) + r" \\")
    return rows


def control_rows(summary, step):
    """Accounting rows. The main sweeps use their runs without foreign load (step.json)."""
    clean = {item["run"]: item["cpu_accounting_clean_runs"] for item in step["apps"].values()}
    rows = []
    for app, setting, run in CONTROL_RUNS:
        sweep = next(s for s in summary["sweeps"] if s["run"] == run)
        name = "issue_list" if app == "Redmine" else "product_detail"
        fit = find(sweep["groups"], "route", name)["fits"]["all_netem_points"]
        if run in clean:
            acc = clean[run]
            cpu_slope = acc["cpu_slope_ms_per_ms"]
        else:
            acc = summary["mean_decomposition"][run]
            cpu_slope = acc["cpu_ms_per_request_slope"]
        low, high = fit["slope_ci95"]
        rows.append(" & ".join([
            app, setting, f"{fit['slope_ms_per_ms']:.1f} [{low:.1f}, {high:.1f}]",
            f"{acc['mean_latency_slope_ms_per_ms']:.1f}",
            f"{acc['mean_round_trips_per_request']:.1f}",
            f"{cpu_slope:.1f}", f"{acc['idle_cpu_slope']:.1f}",
            f"{acc['round_trips_plus_cpu_excluding_idle']:.1f}",
        ]) + r" \\")
    return rows


def step_rows(step):
    """Slopes over 0 to 2 ms and on each side of the step, divided by the traced count."""
    rows = []
    for app in ("redmine", "aimeos"):
        item = step["apps"][app]
        edge = item["mix_step"]
        for group in item["groups"]:
            own = (group["step"]["low_rtt_ms"], group["step"]["high_rtt_ms"])
            if own != (edge["low_rtt_ms"], edge["high_rtt_ms"]):
                # The off-step fits use the mix step, so a page with its own step elsewhere
                # would need its own fit ranges.
                raise ValueError(f"{group['title']} steps at {own}, not at the mix step")
            rows.append(" & ".join([
                app.capitalize(), tex(group["title"]), f"{group['round_trips']:g}",
                f"{edge['low_rtt_ms']:g} to {edge['high_rtt_ms']:g}",
                f"{group['step']['rise_above_trend']:.1f}",
                f"{group['ratio_0_to_2']:.2f}", f"{group['below_step_ratio']:.2f}",
                f"{group['above_step_ratio']:.2f}",
            ]) + r" \\")
    return rows


def main():
    summary = json.loads(SUMMARY.read_text())
    step = json.loads(STEP.read_text())
    lines = [
        "% Generated by experiments/note_tables.py from results/note/summary.json and step.json.",
        r"\newcommand{\TripRows}{",
        *trip_rows(summary),
        "}",
        r"\newcommand{\SlopeRows}{",
        *slope_rows(summary),
        "}",
        r"\newcommand{\ControlRows}{",
        *control_rows(summary, step),
        "}",
        r"\newcommand{\StepRows}{",
        *step_rows(step),
        "}",
    ]
    TARGET.write_text("\n".join(lines) + "\n")
    print(f"Wrote {TARGET.name}")


if __name__ == "__main__":
    main()
