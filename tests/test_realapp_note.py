"""Check packet attribution, slope fits, bootstrap intervals, and the derived tables.

Run: uv run --no-project python tests/test_realapp_note.py
"""

import csv
import importlib.util
import json
import random
import tempfile
import unittest
from pathlib import Path

EXPERIMENTS = Path(__file__).resolve().parent.parent / "experiments"


def load(name):
    spec = importlib.util.spec_from_file_location(name, EXPERIMENTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOTE = load("realapp_note")
ANALYZE = load("analyze_note")
TABLES = load("note_tables")
STEP = load("step_note")


def line(stamp_s, src, sport, dst, dport, flags, length):
    return (f"{stamp_s:.6f} IP {src}.{sport} > {dst}.{dport}: Flags [{flags}], "
            f"seq 1:2, ack 1, win 502, length {length}")


class AttributePacketsTest(unittest.TestCase):
    def test_counts_handshake_and_answered_flights(self):
        app, db = "10.9.0.3", "10.9.0.2"
        lines = [
            line(1.000, app, 40000, db, 5432, "S", 0),
            line(1.001, db, 5432, app, 40000, "S.", 0),
            line(1.002, app, 40000, db, 5432, ".", 0),
            line(1.003, app, 40000, db, 5432, "P.", 41),
            line(1.004, db, 5432, app, 40000, "P.", 90),
            line(1.0041, db, 5432, app, 40000, "P.", 20),
            line(1.005, app, 40000, db, 5432, "P.", 30),
            line(1.006, db, 5432, app, 40000, "P.", 60),
            line(1.007, app, 40000, db, 5432, "P.", 5),
            line(2.003, app, 40001, db, 5432, "P.", 30),
            line(2.004, db, 5432, app, 40001, "P.", 60),
            line(9.000, app, 40001, db, 5432, "P.", 30),
        ]
        rows, unassigned, total = NOTE.attribute_packets(
            lines, starts=[999.0, 1999.0], ends=[1008.0, 2008.0])
        self.assertEqual(total, 12)
        self.assertEqual(unassigned, 1)
        # Handshake, two answered queries, and an unanswered terminate message.
        self.assertEqual(rows[0]["handshakes"], 1)
        self.assertEqual(rows[0]["answered_flights"], 2)
        self.assertEqual(rows[0]["round_trips"], 3)
        self.assertEqual(rows[1]["round_trips"], 1)

    def test_counts_ack_clocked_stall_inside_a_response(self):
        app, db = "10.9.0.3", "10.9.0.2"
        lines = [
            line(1.0000, app, 40000, db, 5432, "P.", 41),
            line(1.0010, db, 5432, app, 40000, "P.", 1448),
            line(1.0011, db, 5432, app, 40000, "P.", 1448),
            line(1.0020, app, 40000, db, 5432, ".", 0),
            # The rest of the response waits one RTT (1 ms) for the ACK above.
            line(1.0030, db, 5432, app, 40000, "P.", 900),
            # A slow server without a pure ACK in the gap is not a stall.
            line(1.0050, db, 5432, app, 40000, "P.", 100),
        ]
        rows, _, _ = NOTE.attribute_packets(lines, starts=[999.0], ends=[1010.0], rtt_ms=1.0)
        self.assertEqual(rows[0]["answered_flights"], 1)
        self.assertEqual(rows[0]["stalls"], 1)
        # At RTT 4 ms the threshold is 2 ms, so the 1.9 ms pause does not count.
        rows, _, _ = NOTE.attribute_packets(lines, starts=[999.0], ends=[1010.0], rtt_ms=4.0)
        self.assertEqual(rows[0]["stalls"], 0)

    def test_condition_labels(self):
        self.assertEqual(NOTE.label(None), "none")
        self.assertEqual(NOTE.label(0), "0")
        self.assertEqual(NOTE.label(0.05), "0.05")

    def test_ignores_server_syn_and_packets_outside_windows(self):
        app, db = "10.9.0.3", "10.9.0.2"
        lines = [
            line(0.500, app, 40000, db, 5432, "P.", 30),
            line(1.000, db, 5432, app, 40000, "S", 0),
            line(1.001, app, 40000, db, 5432, "P.", 30),
            "1.002000 IP6 unrelated line that the parser skips",
        ]
        rows, unassigned, total = NOTE.attribute_packets(lines, [999.0], [1010.0])
        self.assertEqual(total, 3)
        self.assertEqual(unassigned, 1)
        self.assertEqual(rows[0]["handshakes"], 0)
        self.assertEqual(rows[0]["round_trips"], 0)


class DriverConfigTest(unittest.TestCase):
    def test_override_applies_sysctls_to_both_namespaces(self):
        document = NOTE.override_document("aimeos", {"net.ipv4.tcp_autocorking": "0"})
        services = document["services"]
        self.assertEqual(services["database"]["sysctls"], {"net.ipv4.tcp_autocorking": "0"})
        self.assertEqual(services["app"]["sysctls"], {"net.ipv4.tcp_autocorking": "0"})
        self.assertIn("pg_stat_statements", " ".join(services["database"]["command"]))

    def test_cpuset_parsing_and_pinning(self):
        self.assertEqual(NOTE.parse_cpuset("8-10,24"), [8, 9, 10, 24])
        with self.assertRaises(ValueError):
            NOTE.parse_cpuset("8-x")
        pins = {"app": "8-11", "db": "12-14", "client": "15"}
        services = NOTE.override_document("redmine", {}, pins)["services"]
        self.assertEqual(services["redmine"]["cpuset"], "8-11")
        self.assertEqual(services["shaper-redmine"]["cpuset"], "8-11")
        self.assertEqual(services["database"]["cpuset"], "12-14")
        self.assertEqual(services["db-echo"]["cpuset"], "12-14")
        self.assertEqual(services["probe"]["cpuset"], "15")
        unpinned = NOTE.override_document("redmine", {})["services"]
        self.assertNotIn("cpuset", unpinned["redmine"])

    def test_energy_preference_rejects_unsafe_value(self):
        with self.assertRaises(ValueError):
            NOTE.EnergyPreference("performance; rm -rf /")


class FitTest(unittest.TestCase):
    def test_fit_line_recovers_exact_line(self):
        points = [(x / 10, 3.0 + 41.0 * x / 10) for x in range(21)]
        slope, intercept = ANALYZE.fit_line(points)
        self.assertAlmostEqual(slope, 41.0)
        self.assertAlmostEqual(intercept, 3.0)
        self.assertAlmostEqual(ANALYZE.r_squared(points, slope, intercept), 1.0)

    def test_median_fit_resists_a_disturbed_minority_of_runs(self):
        by_rtt = {f"{x:g}": [50 + 40 * x] * 16 + [150 + 40 * x] * 4 for x in (0, 0.5, 1, 2)}
        fit = ANALYZE.fit_group(by_rtt, 0.0, random.Random(2))
        self.assertAlmostEqual(fit["slope_ms_per_ms"], 40.0)
        self.assertAlmostEqual(fit["r_squared"], 1.0)
        self.assertLess(fit["ols_all_runs"]["r_squared"], 0.9)

    def test_percentile_interpolates(self):
        self.assertEqual(ANALYZE.percentile([1, 2, 3, 4], 50), 2.5)
        self.assertEqual(ANALYZE.percentile([5], 99), 5)

    def test_bootstrap_interval_is_degenerate_without_noise(self):
        groups = {x: [2 * x + 1] * 4 for x in (0.0, 0.5, 1.0, 2.0)}
        slope_ci, intercept_ci = ANALYZE.bootstrap_fit(groups, random.Random(1))
        self.assertEqual(slope_ci, [2.0, 2.0])
        self.assertEqual(intercept_ci, [1.0, 1.0])

    def test_bootstrap_interval_covers_true_slope(self):
        rng = random.Random(7)
        groups = {x: [50 + 40 * x + rng.gauss(0, 2) for _ in range(10)]
                  for x in (0, 0.05, 0.1, 0.2, 0.5, 1, 1.5, 2)}
        slope_ci, _ = ANALYZE.bootstrap_fit(groups, random.Random(3))
        self.assertLess(slope_ci[0], 40)
        self.assertGreater(slope_ci[1], 40)
        self.assertLess(slope_ci[1] - slope_ci[0], 6)


def write_csv(path, header, rows):
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


class FixtureRunsTest(unittest.TestCase):
    """Build small run directories and check the summaries computed from them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sweep = self.root / "sweep"
        self.sweep.mkdir()
        rows = [["demo", "container", 0, "cold_start_discarded", "0", 0, 1, "page", "/a", 999.0]]
        cpu_rows = []
        run_index = 0
        for rtt in (0.0, 0.5, 1.0, 2.0):
            for repetition in (1, 2, 3):
                run_index += 1
                for request in range(1, 11):
                    # Page /a makes 10 round trips with 0.5 ms of extra work per ms of RTT.
                    latency = 20 + 10.5 * rtt + 0.01 * request
                    path = "/a" if request <= 6 else "/b"
                    rows.append(["demo", "container", run_index, "measured", f"{rtt:g}",
                                 repetition, request, "page", path, latency])
                cpu_rows.append([run_index, f"{rtt:g}", repetition, 10,
                                 int((5 + 2 * rtt) * 10_000), 10_000])
        # A run with no qdisc: its label must sort and must stay out of the fits.
        cpu_rows.append([run_index + 1, "none", 1, 10, 50_000, 10_000])
        header = ["app", "method", "run_index", "phase", "rtt_ms", "repetition", "request",
                  "route", "path", "latency_ms"]
        write_csv(self.sweep / "sweep_raw.csv", header, rows)
        write_csv(self.sweep / "run_cpu.csv", ["run_index", "rtt_ms", "repetition",
                                               "requests_with_warmup", "app_cpu_usec",
                                               "db_cpu_usec"], cpu_rows)
        self.trace = self.root / "trace"
        self.trace.mkdir()
        trace_rows = []
        for rtt in ("0", "2"):
            for request in range(1, 11):
                path = "/a" if request <= 6 else "/b"
                trace_rows.append([rtt, request, "page", path, 1.0, 0, 10, 10,
                                   1 if rtt == "2" else 0, 40])
        write_csv(self.trace / "trace_round_trips.csv",
                  ["rtt_ms", "request", "route", "path", "latency_ms", "handshakes",
                   "answered_flights", "round_trips", "stalls", "packets"], trace_rows)
        (self.trace / "trace_statements.json").write_text(json.dumps(
            {"page": {"statements_per_request": 10.0}}))
        (self.trace / "metadata.json").write_text(json.dumps(
            {"args": {"app": "demo"}, "packet_capture": {}}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_sweep_excludes_cold_start_and_fits_slope(self):
        summary = ANALYZE.summarize_sweep(self.root, "sweep", 0.0, random.Random(1))
        self.assertEqual(next(iter(summary["cpu_by_rtt"])), "none")
        self.assertEqual(summary["requests_per_run"], [10])
        self.assertEqual(summary["cold_start_discarded_p50_ms"], 999.0)
        group = next(g for g in summary["groups"] if g["kind"] == "all")
        self.assertAlmostEqual(group["fits"]["all_netem_points"]["slope_ms_per_ms"], 10.5)
        names = {(g["kind"], g["name"]) for g in summary["groups"]}
        # /b has only four requests per run, below the per-path minimum of five.
        self.assertIn(("path", "/a"), names)
        self.assertNotIn(("path", "/b"), names)

    def test_compare_reports_ratio_with_and_without_stalls(self):
        trace = ANALYZE.summarize_trace(self.root, "trace")
        sweep = ANALYZE.summarize_sweep(self.root, "sweep", 0.0, random.Random(1))
        rows = ANALYZE.compare(trace, sweep, "all_netem_points")
        page = next(r for r in rows if r["kind"] == "route")
        self.assertAlmostEqual(page["slope_over_round_trips"], 1.05)
        self.assertAlmostEqual(page["slope_over_round_trips_plus_stalls"], round(10.5 / 11, 4))
        self.assertFalse(page["round_trips_inside_slope_ci"])

    def test_idle_cpu_is_removed_in_proportion_to_extra_wall_time(self):
        # 10 s windows with 100 ms of CPU each side: 0.02 ms of CPU per ms of wall time.
        write_csv(self.sweep / "idle_cpu.csv", ["phase", "wall_s", "app_cpu_usec",
                                                "db_cpu_usec"],
                  [["before_runs", 10, 100_000, 100_000], ["after_runs", 10, 100_000, 100_000]])
        trace = ANALYZE.summarize_trace(self.root, "trace")
        result = ANALYZE.mean_decomposition(self.root, "sweep", trace)
        self.assertAlmostEqual(result["idle"]["cpu_ms_per_wall_ms"], 0.02)
        self.assertAlmostEqual(result["idle_cpu_slope"], 0.21)
        self.assertAlmostEqual(result["cpu_slope_excluding_idle"], 1.79)

    def test_leave_one_out_shift(self):
        self.assertEqual(ANALYZE.leave_one_out([1, 2, 3, 4, 100]), 0.5)
        self.assertEqual(ANALYZE.leave_one_out([5, 5, 5]), 0)

    def test_mean_decomposition_adds_round_trips_and_cpu_slope(self):
        trace = ANALYZE.summarize_trace(self.root, "trace")
        result = ANALYZE.mean_decomposition(self.root, "sweep", trace)
        self.assertAlmostEqual(result["mean_round_trips_per_request"], 10.0)
        self.assertAlmostEqual(result["cpu_ms_per_request_slope"], 2.0)
        self.assertAlmostEqual(result["round_trips_plus_cpu_slope"], 12.0)
        self.assertAlmostEqual(result["mean_latency_slope_ms_per_ms"], 10.5)


class TablesTest(unittest.TestCase):
    def test_sort_orders_must_agree(self):
        same = {"round_trips_by_rtt": {"0": 1}, "stalls_by_rtt": {"2": 0}}
        other = {"round_trips_by_rtt": {"0": 2}, "stalls_by_rtt": {"2": 0}}
        trace = {"groups": [{"kind": "path", "name": "a", **same},
                            {"kind": "path", "name": "b", **other}]}
        with self.assertRaises(ValueError):
            TABLES.trip_groups(trace, (("path", "a"), ("path", "b"), "Listing"))
        trace["groups"][1].update(same)
        _group, route, title = TABLES.trip_groups(trace, (("path", "a"), ("path", "b"), "Listing"))
        self.assertEqual((route, title), (None, "Listing"))

    def test_step_rows_reject_a_page_that_steps_elsewhere(self):
        edge = {"low_rtt_ms": 0.4, "high_rtt_ms": 0.5}
        group = {"title": "Page", "round_trips": 10, "step": {**edge, "rise_above_trend": 8.0},
                 "ratio_0_to_2": 1.2, "below_step_ratio": 1.05, "above_step_ratio": 1.06}
        step = {"apps": {app: {"mix_step": edge, "groups": [group]}
                         for app in ("redmine", "aimeos")}}
        self.assertIn("0.4 to 0.5 & 8.0 & 1.20 & 1.05 & 1.06", TABLES.step_rows(step)[0])
        group["step"] = {"low_rtt_ms": 0.5, "high_rtt_ms": 0.75, "rise_above_trend": 8.0}
        with self.assertRaises(ValueError):
            TABLES.step_rows(step)

    def test_tex_escapes_underscore_and_ampersand(self):
        self.assertEqual(TABLES.tex("a_b & c"), "a\\_b \\& c")


class StepTest(unittest.TestCase):
    # 10 round trips per page: 10.5 ms per ms off the step, and an 8 ms step at 0.4 to 0.5.
    POINTS = tuple((x, 20 + 10.5 * x + (8 if x >= 0.5 else 0))
                   for x in (0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1, 1.5, 2))

    def test_largest_rise_finds_the_step_not_the_widest_gap(self):
        step = STEP.largest_rise(list(self.POINTS))
        self.assertEqual((step["low_rtt_ms"], step["high_rtt_ms"]), (0.4, 0.5))
        self.assertAlmostEqual(step["rise_above_trend"], 8.0)

    def test_off_step_fits_recover_the_slope_on_each_side(self):
        fits = STEP.off_step_fits(list(self.POINTS), STEP.largest_rise(list(self.POINTS)), 10)
        self.assertAlmostEqual(fits["below_step_ratio"], 1.05)
        self.assertAlmostEqual(fits["above_step_ratio"], 1.05)
        # One line through the whole range takes the step as extra slope.
        self.assertGreater(fits["ratio_0_to_2"], 1.2)

    def test_read_runs_drops_no_qdisc_and_measures_foreign_load(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            write_csv(root / "run_cpu.csv",
                      ["run_index", "rtt_ms", "repetition", "requests_with_warmup",
                       "app_cpu_usec", "db_cpu_usec", "wall_s", "pinned_cpus_busy_core_s"],
                      [[1, "0", 1, 10, 1_000_000, 1_000_000, 2.0, 10.0],
                       [2, "none", 1, 10, 1_000_000, 0, 1.0, 1.0]])
            write_csv(root / "sweep_raw.csv",
                      ["app", "method", "run_index", "phase", "rtt_ms", "repetition",
                       "request", "route", "path", "latency_ms"],
                      [["demo", "container", 1, "measured", "0", 1, 1, "page", "/a", 5.0],
                       ["demo", "container", 1, "warmup", "0", 1, 1, "page", "/a", 99.0],
                       ["demo", "container", 2, "measured", "none", 1, 1, "page", "/a", 4.0]])
            runs = STEP.read_runs(root)
        self.assertEqual(list(runs), ["1"])
        # 10 busy thread-seconds minus 2 s of container CPU over 2 s of wall time.
        self.assertAlmostEqual(runs["1"]["other_threads"], 4.0)
        self.assertAlmostEqual(runs["1"]["cpu_ms_per_request"], 200.0)
        self.assertEqual(runs["1"]["latencies"][("all", "all")], [5.0])

    def test_cpu_accounting_removes_idle_cpu_in_proportion_to_latency(self):
        runs = [{"rtt_ms": x, "cpu_ms_per_request": 5 + 2 * x,
                 "latencies": {("all", "all"): [20 + 12 * x]}} for x in (0, 1, 2)]
        result = STEP.cpu_accounting(runs, 10.0, 0.05)
        self.assertAlmostEqual(result["idle_cpu_slope"], 0.6)
        self.assertAlmostEqual(result["round_trips_plus_cpu_excluding_idle"], 11.4)


if __name__ == "__main__":
    unittest.main()
