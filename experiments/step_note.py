"""Locate the latency step in the pinned sweeps and fit slopes on each side of it.

The 0 to 2 ms fit in analyze_note.py puts one line through a curve that has a step. This
script finds the step, fits the settings below and above it separately, and redoes the CPU
accounting on the runs without foreign load on the pinned CPUs.

Usage: uv run --no-project python experiments/step_note.py
The output goes to results/note/step.json.
"""

import csv
import itertools
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze_note
import note_tables

PROJECT = analyze_note.PROJECT
OUTPUT = PROJECT / "results" / "note" / "step.json"
# A run is "loaded" when processes other than the app and database containers kept more
# than this many hardware threads of the pinned CPUs busy on average.
LOAD_LIMIT_THREADS = 2.0


def read_runs(run_dir):
    """Return {run_index: run record} for the measured netem runs of one sweep."""
    runs = {}
    with (run_dir / "run_cpu.csv").open() as handle:
        for row in csv.DictReader(handle):
            if row["rtt_ms"] == "none":
                continue
            container_us = int(row["app_cpu_usec"]) + int(row["db_cpu_usec"])
            other = (float(row["pinned_cpus_busy_core_s"]) - container_us / 1e6) / float(
                row["wall_s"])
            runs[row["run_index"]] = {
                "rtt_ms": float(row["rtt_ms"]), "other_threads": other,
                "cpu_ms_per_request": container_us / int(row["requests_with_warmup"]) / 1000,
                "latencies": defaultdict(list),
            }
    with (run_dir / "sweep_raw.csv").open() as handle:
        for row in csv.DictReader(handle):
            run = runs.get(row["run_index"])
            if row["phase"] != "measured" or run is None:
                continue
            value = float(row["latency_ms"])
            for key in (("all", "all"), ("route", row["route"]), ("path", row["path"])):
                run["latencies"][key].append(value)
    return runs


def group_p50(key):
    return lambda run: statistics.median(run["latencies"][key])


def condition_medians(runs, value):
    """Median over runs of value(run) for each RTT setting, sorted by RTT."""
    by_rtt = defaultdict(list)
    for run in runs:
        by_rtt[run["rtt_ms"]].append(value(run))
    return [(rtt, statistics.median(values)) for rtt, values in sorted(by_rtt.items())]


def largest_rise(points):
    """The adjacent pair of settings with the largest rise per ms of RTT above the trend.

    The rise is compared with the median rise per ms over all adjacent pairs, so that a wide
    gap between two settings does not count as a step only because the gap is wide.
    """
    rates = [(y1 - y0) / (x1 - x0) for (x0, y0), (x1, y1) in itertools.pairwise(points)]
    trend = statistics.median(rates)
    excess = [(y1 - y0) - trend * (x1 - x0)
              for (x0, y0), (x1, y1) in itertools.pairwise(points)]
    index = max(range(len(excess)), key=excess.__getitem__)
    return {"low_rtt_ms": points[index][0], "high_rtt_ms": points[index + 1][0],
            "low_value": round(points[index][1], 4), "high_value": round(points[index + 1][1], 4),
            "rise_above_trend": round(excess[index], 4)}


def slope_between(points, low, high):
    selected = [(x, y) for x, y in points if low <= x <= high]
    slope, _ = analyze_note.fit_line(selected)
    return slope


def off_step_fits(points, step, count):
    """Fit the settings strictly below the step and those from its upper edge to the top."""
    last_below = max(x for x, _ in points if x < step["low_rtt_ms"])
    below = slope_between(points, points[0][0], last_below)
    above = slope_between(points, step["high_rtt_ms"], points[-1][0])
    whole = slope_between(points, points[0][0], points[-1][0])
    return {
        "round_trips": count,
        "slope_0_to_2_ms_per_ms": round(whole, 4), "ratio_0_to_2": round(whole / count, 4),
        "below_step_slope_ms_per_ms": round(below, 4), "below_step_ratio": round(below / count, 4),
        "above_step_slope_ms_per_ms": round(above, 4), "above_step_ratio": round(above / count, 4),
    }


def cpu_accounting(runs, mean_round_trips, idle_ms_per_wall_ms):
    """Round trips plus the CPU slope without idle CPU, against the mean-latency slope."""
    latency_slope, _ = analyze_note.fit_line(
        [(r["rtt_ms"], statistics.fmean(r["latencies"][("all", "all")])) for r in runs])
    cpu_slope, _ = analyze_note.fit_line([(r["rtt_ms"], r["cpu_ms_per_request"]) for r in runs])
    idle_slope = idle_ms_per_wall_ms * latency_slope
    return {"runs": len(runs), "mean_latency_slope_ms_per_ms": round(latency_slope, 4),
            "mean_round_trips_per_request": round(mean_round_trips, 4),
            "cpu_slope_ms_per_ms": round(cpu_slope, 4), "idle_cpu_slope": round(idle_slope, 4),
            "round_trips_plus_cpu_excluding_idle": round(
                mean_round_trips + cpu_slope - idle_slope, 4)}


def summarize_app(runs_root, app):
    run_name = note_tables.MAIN_SWEEPS[app]
    runs = read_runs(runs_root / run_name)
    clean = [r for r in runs.values() if r["other_threads"] <= LOAD_LIMIT_THREADS]
    trace = analyze_note.summarize_trace(runs_root, note_tables.STALL_TRACES[app])
    mix = analyze_note.find_group(trace, "all", "all")["round_trips_by_rtt"]["0"]
    idle = analyze_note.idle_rate(runs_root / run_name / "idle_cpu.csv")["cpu_ms_per_wall_ms"]
    mix_points = condition_medians(clean, group_p50(("all", "all")))
    step = largest_rise(mix_points)
    groups = []
    for kind, name, title in note_tables.SLOPE_GROUPS[app]:
        count = analyze_note.find_group(trace, kind, name)["round_trips_by_rtt"]["0"]["median"]
        points = condition_medians(clean, group_p50((kind, name)))
        groups.append({"kind": kind, "name": name, "title": title,
                       "clean_p50_by_rtt": {f"{x:g}": round(y, 4) for x, y in points},
                       "step": largest_rise(points), **off_step_fits(points, step, count)})
    all_cpu = condition_medians(runs.values(), lambda r: r["cpu_ms_per_request"])
    clean_cpu = condition_medians(clean, lambda r: r["cpu_ms_per_request"])
    return {
        "run": run_name, "runs": len(runs), "clean_runs": len(clean),
        "loaded_runs": len(runs) - len(clean),
        "mix_step": step, "groups": groups,
        "cpu_ms_per_request_all_runs": {f"{x:g}": round(y, 4) for x, y in all_cpu},
        "cpu_step_all_runs": largest_rise(all_cpu),
        "cpu_step_clean_runs": largest_rise(clean_cpu),
        "cpu_accounting_all_runs": cpu_accounting(list(runs.values()), mix["mean"], idle),
        "cpu_accounting_clean_runs": cpu_accounting(clean, mix["mean"], idle),
    }


def main():
    result = {
        "load_limit_threads": LOAD_LIMIT_THREADS,
        "note": ("Clean runs: other processes kept at most load_limit_threads of the pinned "
                 "hardware threads busy. CPU per request is the median over runs of the "
                 "app plus database container CPU time per request."),
        "apps": {app: summarize_app(analyze_note.RUNS, app) for app in ("redmine", "aimeos")},
    }
    OUTPUT.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Wrote {OUTPUT.relative_to(PROJECT)}")
    for app, item in result["apps"].items():
        print(app, "loaded runs", item["loaded_runs"], "of", item["runs"], "mix step",
              item["mix_step"], "cpu step", item["cpu_step_all_runs"])
        for g in item["groups"]:
            print(f"  {g['title']:28s} RT {g['round_trips']:>5g} step "
                  f"{g['step']['low_rtt_ms']:g}-{g['step']['high_rtt_ms']:g} "
                  f"0-2 {g['ratio_0_to_2']:.2f} below {g['below_step_ratio']:.2f} "
                  f"above {g['above_step_ratio']:.2f}")
        print("  accounting all", item["cpu_accounting_all_runs"])
        print("  accounting clean", item["cpu_accounting_clean_runs"])
        print("  cpu per request", item["cpu_ms_per_request_all_runs"])


if __name__ == "__main__":
    main()
