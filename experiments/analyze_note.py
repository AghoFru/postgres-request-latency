"""Summarize the measurement-note runs: netem fidelity, traced round trips, and slope fits.

Usage:
  python experiments/analyze_note.py --fidelity RUN --trace RUN RUN --sweep RUN RUN \
      [--min-fit-rtt MS]

RUN is a directory name under runs/. The output goes to results/note/summary.json.
"""

import argparse
import csv
import gzip
import json
import random
import statistics
from collections import defaultdict
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
RUNS = PROJECT / "runs"
BOOTSTRAPS = 10_000
SEED = 20261118


def percentile(values, percent):
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def ci95(estimates):
    return [round(percentile(estimates, 2.5), 4), round(percentile(estimates, 97.5), 4)]


def fit_line(points):
    """Ordinary least squares for (x, y) points. Return slope, intercept."""
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in points)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in points)
    slope = sxy / sxx
    return slope, mean_y - slope * mean_x


def r_squared(points, slope, intercept):
    mean_y = sum(y for _, y in points) / len(points)
    total = sum((y - mean_y) ** 2 for _, y in points)
    residual = sum((y - (slope * x + intercept)) ** 2 for x, y in points)
    return 1 - residual / total


def median_points(groups):
    """One point per RTT condition: the median of the run-level p50 values."""
    return [(x, statistics.median(groups[x])) for x in sorted(groups)]


def bootstrap_fit(groups, rng):
    """Resample runs within each RTT condition, take each condition's median, and refit.

    Return slope and intercept CIs. Fitting the condition medians keeps a minority of
    disturbed runs at one condition from moving the slope, as it does for the medians.
    """
    slopes, intercepts = [], []
    keys = sorted(groups)
    for _ in range(BOOTSTRAPS):
        sample = [(x, statistics.median(rng.choices(groups[x], k=len(groups[x]))))
                  for x in keys]
        slope, intercept = fit_line(sample)
        slopes.append(slope)
        intercepts.append(intercept)
    return ci95(slopes), ci95(intercepts)


def bootstrap_median(values, rng):
    return ci95([statistics.median(rng.choices(values, k=len(values)))
                 for _ in range(BOOTSTRAPS)])


MIN_REQUESTS_PER_PATH_RUN = 5


def load_sweep(runs_root, run):
    """Return {group: {rtt_label: [run p50, ...]}} for measured runs only.

    Groups are the whole mix ("all", "all"), each route, and each request path that occurs
    at least MIN_REQUESTS_PER_PATH_RUN times in every run.
    """
    latencies = defaultdict(list)
    with (runs_root / run / "sweep_raw.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    app = rows[0]["app"]
    cold = [float(r["latency_ms"]) for r in rows if r["phase"] == "cold_start_discarded"]
    for row in rows:
        if row["phase"] != "measured":
            continue
        key = (row["rtt_ms"], row["repetition"])
        value = float(row["latency_ms"])
        latencies[(("all", "all"), *key)].append(value)
        latencies[(("route", row["route"]), *key)].append(value)
        latencies[(("path", row["path"]), *key)].append(value)
    run_p50 = defaultdict(lambda: defaultdict(list))
    smallest = defaultdict(lambda: 10**9)
    requests = set()
    for (group, rtt, _), values in latencies.items():
        run_p50[group][rtt].append(statistics.median(values))
        smallest[group] = min(smallest[group], len(values))
        if group[0] == "all":
            requests.add(len(values))
    kept = {g: v for g, v in run_p50.items()
            if g[0] != "path" or smallest[g] >= MIN_REQUESTS_PER_PATH_RUN}
    return app, kept, sorted(requests), cold


def fit_group(by_rtt, low, rng):
    groups = {float(k): v for k, v in by_rtt.items() if k != "none" and float(k) >= low}
    points = median_points(groups)
    slope, intercept = fit_line(points)
    slope_ci, intercept_ci = bootstrap_fit(groups, rng)
    residuals = {f"{x:g}": round(y - (slope * x + intercept), 4) for x, y in points}
    all_runs = [(x, y) for x, values in groups.items() for y in values]
    ols_slope, ols_intercept = fit_line(all_runs)
    return {
        "rtt_range_ms": [min(groups), max(groups)],
        "estimator": "OLS through the per-condition medians of run-level p50",
        "slope_ms_per_ms": round(slope, 4), "slope_ci95": slope_ci,
        "intercept_ms": round(intercept, 4), "intercept_ci95": intercept_ci,
        "r_squared": round(r_squared(points, slope, intercept), 5),
        "median_residual_ms": residuals,
        "ols_all_runs": {
            "slope_ms_per_ms": round(ols_slope, 4),
            "r_squared": round(r_squared(all_runs, ols_slope, ols_intercept), 5),
        },
    }


def rtt_order(rtt):
    """Sort key for RTT labels: "none" (no qdisc) first, then numeric settings."""
    return -1.0 if rtt == "none" else float(rtt)


def cpu_per_request(runs_root, run):
    """Median container CPU time per request at each RTT, or None if the run lacks it."""
    path = runs_root / run / "run_cpu.csv"
    if not path.exists():
        return None
    by_rtt = defaultdict(lambda: {"app": [], "db": []})
    with path.open() as handle:
        for row in csv.DictReader(handle):
            count = int(row["requests_with_warmup"])
            by_rtt[row["rtt_ms"]]["app"].append(int(row["app_cpu_usec"]) / count / 1000)
            by_rtt[row["rtt_ms"]]["db"].append(int(row["db_cpu_usec"]) / count / 1000)
    return {
        rtt: {f"{side}_cpu_ms_per_request": round(statistics.median(values), 4)
              for side, values in sides.items()}
        for rtt, sides in sorted(by_rtt.items(), key=lambda item: rtt_order(item[0]))
    }


def mean_decomposition(runs_root, run, trace):
    """Account for the slope of run-mean latency with round trips and container CPU time.

    If each round trip costs exactly one RTT and the work per request is constant, the slope
    of mean latency equals the mean round trips per request. The slope of measured container
    CPU time per request is added to that. This is accounting: the CPU time is measured, but
    its placement on the request path is inferred. When the run measured idle CPU, the idle
    rate times the extra wall time per request is also reported and removed.
    """
    cpu_path = runs_root / run / "run_cpu.csv"
    if not cpu_path.exists():
        return None
    latencies = defaultdict(list)
    with (runs_root / run / "sweep_raw.csv").open() as handle:
        for row in csv.DictReader(handle):
            if row["phase"] == "measured" and row["rtt_ms"] != "none":
                latencies[(row["run_index"], row["rtt_ms"])].append(float(row["latency_ms"]))
    mean_points = [(float(rtt), statistics.fmean(v)) for (_, rtt), v in latencies.items()]
    cpu_points = []
    with cpu_path.open() as handle:
        for row in csv.DictReader(handle):
            if row["rtt_ms"] != "none":
                total = int(row["app_cpu_usec"]) + int(row["db_cpu_usec"])
                cpu_points.append((float(row["rtt_ms"]),
                                   total / int(row["requests_with_warmup"]) / 1000))
    mix = find_group(trace, "all", "all")["round_trips_by_rtt"]["0"]
    latency_slope, _ = fit_line(mean_points)
    cpu_slope, _ = fit_line(cpu_points)
    result = {
        "mean_latency_slope_ms_per_ms": round(latency_slope, 4),
        "mean_round_trips_per_request": round(mix["mean"], 4),
        "cpu_ms_per_request_slope": round(cpu_slope, 4),
        "round_trips_plus_cpu_slope": round(mix["mean"] + cpu_slope, 4),
    }
    idle = idle_rate(runs_root / run / "idle_cpu.csv")
    if idle is not None:
        # Idle CPU accrues with wall time, so a request that takes longer also collects more
        # of it. Remove that part from the CPU slope.
        idle_slope = idle["cpu_ms_per_wall_ms"] * latency_slope
        result.update({
            "idle": idle,
            "idle_cpu_slope": round(idle_slope, 4),
            "cpu_slope_excluding_idle": round(cpu_slope - idle_slope, 4),
            "round_trips_plus_cpu_excluding_idle": round(mix["mean"] + cpu_slope - idle_slope, 4),
        })
    return result


def idle_rate(path):
    """Container CPU per wall time with no requests, from the before and after windows."""
    if not path.exists():
        return None
    with path.open() as handle:
        rows = list(csv.DictReader(handle))
    windows = {}
    for row in rows:
        wall_ms = float(row["wall_s"]) * 1000
        windows[row["phase"]] = {
            "app_cpu_ms_per_wall_ms": round(int(row["app_cpu_usec"]) / 1000 / wall_ms, 5),
            "db_cpu_ms_per_wall_ms": round(int(row["db_cpu_usec"]) / 1000 / wall_ms, 5),
            "wall_s": float(row["wall_s"]),
        }
    total = statistics.fmean(w["app_cpu_ms_per_wall_ms"] + w["db_cpu_ms_per_wall_ms"]
                             for w in windows.values())
    return {"windows": windows, "cpu_ms_per_wall_ms": round(total, 5)}


def pinned_load(runs_root, run):
    """Busy cores on the pinned CPUs per run, minus the app and database containers.

    The remainder includes the load client, docker exec, and any other process that ran on
    those CPUs during the run.
    """
    path = runs_root / run / "run_cpu.csv"
    if not path.exists():
        return None
    with path.open() as handle:
        rows = [r for r in csv.DictReader(handle) if r.get("pinned_cpus_busy_core_s")]
    if not rows:
        return None
    other = sorted(
        (float(r["pinned_cpus_busy_core_s"])
         - (int(r["app_cpu_usec"]) + int(r["db_cpu_usec"])) / 1e6) / float(r["wall_s"])
        for r in rows)
    return {"runs": len(other), "median_other_busy_cores": round(statistics.median(other), 3),
            "p95_other_busy_cores": round(percentile(other, 95), 3),
            "max_other_busy_cores": round(other[-1], 3)}


def leave_one_out(values):
    """Largest change of the median when one run is left out."""
    full = statistics.median(values)
    return max(abs(statistics.median(values[:i] + values[i + 1:]) - full)
               for i in range(len(values)))


def find_group(trace, kind, name):
    return next(g for g in trace["groups"] if g["kind"] == kind and g["name"] == name)


def summarize_sweep(runs_root, run, min_fit_rtt, rng):
    app, run_p50, requests, cold = load_sweep(runs_root, run)
    output = {"run": run, "app": app, "requests_per_run": requests,
              "cold_start_discarded_p50_ms": round(statistics.median(cold), 4),
              "cpu_by_rtt": cpu_per_request(runs_root, run),
              "pinned_cpu_load": pinned_load(runs_root, run),
              "groups": []}
    for group, by_rtt in sorted(run_p50.items()):
        if "0" not in by_rtt:
            continue
        conditions = []
        baseline = statistics.median(by_rtt["0"])
        for rtt in sorted(by_rtt, key=lambda k: -1.0 if k == "none" else float(k)):
            values = by_rtt[rtt]
            median = statistics.median(values)
            interval = bootstrap_median(values, rng)
            conditions.append({
                "rtt_ms": rtt, "runs": len(values), "p50_ms": round(median, 4),
                "p50_ci95_ms": interval,
                "ci_half_width_ms": round((interval[1] - interval[0]) / 2, 4),
                "max_leave_one_out_shift_ms": round(leave_one_out(values), 4),
                "delta_from_netem_0_ms": round(median - baseline, 4),
            })
        output["groups"].append({
            "kind": group[0], "name": group[1], "conditions": conditions,
            "fits": {
                "all_netem_points": fit_group(by_rtt, 0.0, rng),
                "fit_range": fit_group(by_rtt, min_fit_rtt, rng),
            },
        })
    return output


def summarize_fidelity(runs_root, run):
    blocks = defaultdict(list)
    with gzip.open(runs_root / run / "fidelity_tcp.csv.gz", "rt") as handle:
        for row in csv.DictReader(handle):
            key = (row["arm"], row["method"], row["gap_ms"], row["rtt_ms"])
            blocks[key].append(float(row["measured_rtt_ms"]))
    select1 = defaultdict(list)
    with gzip.open(runs_root / run / "fidelity_select1.csv.gz", "rt") as handle:
        for row in csv.DictReader(handle):
            select1[("select1", row["method"], "na", row["rtt_ms"])].append(
                float(row["latency_ms"]))
    output = []
    for source, table in (("tcp_echo", blocks), ("rails_select1", select1)):
        for key in sorted(table, key=lambda k: (*k[:3], -1.0 if k[3] == "none" else float(k[3]))):
            arm, method, gap, rtt = key
            values = table[key]
            base = statistics.median(table[(arm, method, gap, "none")])
            p50 = statistics.median(values)
            configured = 0.0 if rtt == "none" else float(rtt)
            output.append({
                "probe": source, "arm": arm, "method": method, "gap_ms": gap, "rtt_ms": rtt,
                "samples": len(values),
                **{f"p{p}_ms": round(percentile(values, p), 4) for p in (5, 50, 95, 99)},
                "added_p50_ms": round(p50 - base, 4),
                "excess_over_configured_p50_ms": round(p50 - base - configured, 4),
            })
    return output


def round_trip_stats(values):
    return {"requests": len(values), "median": statistics.median(values), "min": min(values),
            "max": max(values), "mean": statistics.fmean(values)}


def summarize_trace(runs_root, run):
    groups = defaultdict(lambda: defaultdict(list))
    stalled = defaultdict(lambda: defaultdict(list))
    effective = defaultdict(lambda: defaultdict(list))
    with (runs_root / run / "trace_round_trips.csv").open() as handle:
        has_stalls = "stalls" in (handle.readline())
        handle.seek(0)
        for row in csv.DictReader(handle):
            value = int(row["round_trips"])
            stalls = int(row.get("stalls") or 0)
            for key in (("all", "all"), ("route", row["route"]), ("path", row["path"])):
                groups[key][row["rtt_ms"]].append(value)
                stalled[key][row["rtt_ms"]].append(stalls)
                effective[key][row["rtt_ms"]].append(value + stalls)
            groups[("handshakes", row["route"])][row["rtt_ms"]].append(int(row["handshakes"]))
    statements = json.loads((runs_root / run / "trace_statements.json").read_text())
    metadata = json.loads((runs_root / run / "metadata.json").read_text())
    return {
        "run": run, "app": metadata["args"]["app"],
        "packet_capture": metadata.get("packet_capture"),
        "statements_per_request": {
            route: item["statements_per_request"] for route, item in statements.items()
        },
        "stalls_recorded": has_stalls,
        "groups": [
            {"kind": kind, "name": name,
             "round_trips_by_rtt": {rtt: round_trip_stats(v) for rtt, v in sorted(by.items())},
             "stalls_by_rtt": {rtt: round_trip_stats(v)
                               for rtt, v in sorted(stalled[(kind, name)].items())},
             "round_trips_plus_stalls_by_rtt": {
                 rtt: round_trip_stats(v) for rtt, v in sorted(effective[(kind, name)].items())}}
            for (kind, name), by in sorted(groups.items())
        ],
    }


def compare(trace, sweep, fit):
    """Pair each sweep group with its traced round trips at RTT 0, and with stalls if traced."""
    traced = {(g["kind"], g["name"]): g for g in trace["groups"]}
    rows = []
    for group in sweep["groups"]:
        found = traced.get((group["kind"], group["name"]))
        if not found or "0" not in found["round_trips_by_rtt"]:
            continue
        counts = found["round_trips_by_rtt"]
        result = group["fits"][fit]
        extra = {}
        if trace["stalls_recorded"]:
            for rtt, stats in found["round_trips_plus_stalls_by_rtt"].items():
                extra[f"round_trips_plus_stalls_median_rtt{rtt}"] = stats["median"]
            with_stalls = found["round_trips_plus_stalls_by_rtt"].get("2")
            if with_stalls:
                # Stalls cost one RTT each but are not answered flights, so they belong in
                # the count of RTT-bound waits. The 2 ms trace has the clearest stalls.
                extra["slope_over_round_trips_plus_stalls"] = round(
                    result["slope_ms_per_ms"] / with_stalls["median"], 4)
        rows.append({
            **extra,
            "kind": group["kind"], "name": group["name"],
            "round_trips_median_rtt0": counts["0"]["median"],
            "round_trips_range_rtt0": [counts["0"]["min"], counts["0"]["max"]],
            "slope_ms_per_ms": result["slope_ms_per_ms"], "slope_ci95": result["slope_ci95"],
            "slope_over_round_trips": round(
                result["slope_ms_per_ms"] / counts["0"]["median"], 4),
            "round_trips_inside_slope_ci": (
                result["slope_ci95"][0] <= counts["0"]["median"] <= result["slope_ci95"][1]),
        })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fidelity", required=True)
    parser.add_argument("--trace", nargs="+", required=True)
    parser.add_argument("--sweep", nargs="+", required=True)
    parser.add_argument("--min-fit-rtt", type=float, default=0.0)
    parser.add_argument("--runs-root", type=Path, default=RUNS)
    parser.add_argument("--output", type=Path,
                        default=PROJECT / "results" / "note" / "summary.json")
    args = parser.parse_args()
    rng = random.Random(SEED)
    trace_list = [summarize_trace(args.runs_root, r) for r in args.trace]
    # The last trace given for an app is the reference count for that app's sweeps.
    traces = {t["app"]: t for t in trace_list}
    sweeps = [summarize_sweep(args.runs_root, r, args.min_fit_rtt, rng) for r in args.sweep]
    summary = {
        "bootstrap_resamples": BOOTSTRAPS, "seed": SEED, "min_fit_rtt_ms": args.min_fit_rtt,
        "fit_note": ("OLS through per-condition medians of run-level p50. CIs resample runs "
                     "within each condition and recompute the medians."),
        "fidelity": {"run": args.fidelity,
                     "rows": summarize_fidelity(args.runs_root, args.fidelity)},
        "traces": trace_list,
        "sweeps": sweeps,
        "mean_decomposition": {
            sweep["run"]: mean_decomposition(args.runs_root, sweep["run"], traces[sweep["app"]])
            for sweep in sweeps
        },
        "slope_vs_round_trips": {
            sweep["run"]: {fit: compare(traces[sweep["app"]], sweep, fit)
                           for fit in ("all_netem_points", "fit_range")}
            for sweep in sweeps
        },
    }
    target = args.output
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"Wrote {target.name}")


if __name__ == "__main__":
    main()
