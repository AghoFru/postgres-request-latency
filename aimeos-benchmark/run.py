"""Run a repeatable storefront request mix against Aimeos and PostgreSQL."""

import csv
import argparse
import json
import math
import random
import statistics
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT.parent.parent / "results"
COMPOSE = [
    "docker", "compose", "--project-name", "db-latency-aimeos",
    "-f", str(ROOT / "compose.benchmark.yaml"),
]
RTT_MS = [0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2, 5, 10]
SUB_MS_RTT = [0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1]
# Bound every Compose call so that a stuck build or container cannot hold a sweep open.
COMMAND_TIMEOUT_S = 1800


def compose(*args, capture=False):
    return subprocess.run(
        [*COMPOSE, *args], check=True, text=True, capture_output=capture,
        timeout=COMMAND_TIMEOUT_S,
    )


def shape(service, delay_ms):
    compose(
        "exec", "-T", service, "tc", "qdisc", "replace", "dev", "eth0",
        "root", "netem", "delay", f"{delay_ms:g}ms",
    )


def percentile(values, percent):
    return sorted(values)[math.ceil(len(values) * percent / 100) - 1]


def initialize():
    compose("up", "-d", "--wait", "database", "database-shaper")
    for command in (
        ("migrate", "--force"),
        ("aimeos:setup", "--option=setup/default/demo:1"),
    ):
        compose("run", "--rm", "--no-deps", "app", "php", "artisan", *command)
    compose("up", "-d", "--build", "--wait")


def measure(count, concurrency, warmup):
    result = compose(
        "exec", "-T", "probe", "node", "/work/traffic.mjs", "measure",
        str(count), str(concurrency), str(warmup), capture=True,
    )
    measured = json.loads(result.stdout.strip().splitlines()[-1])
    if measured["failures"]:
        raise RuntimeError(f"Aimeos had {measured['failures']} failed requests")
    return measured


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sub-ms", action="store_true")
    args = parser.parse_args()
    rtts = SUB_MS_RTT if args.sub_ms else RTT_MS
    repetitions = 10 if args.sub_ms else 3
    requests = 50 if args.sub_ms else 10
    warmup = 20 if args.sub_ms else 5
    tasks = [
        (rtt, 1, repetition, requests)
        for repetition in range(1, repetitions + 1)
        for rtt in rtts
    ]
    random.Random(20260926).shuffle(tasks)
    suffix = "subms" if args.sub_ms else "fine"
    raw_path = RESULTS / f"aimeos_{suffix}_raw.csv"
    summary = []

    try:
        initialize()
        with raw_path.open("w", newline="") as output:
            writer = csv.writer(output)
            writer.writerow([
                "rtt_ms", "concurrency", "repetition", "request", "latency_ms",
            ])
            for run_index, (rtt, concurrency, repetition, count) in enumerate(tasks, 1):
                shape("app", rtt / 2)
                shape("database-shaper", rtt / 2)
                measured = measure(count, concurrency, warmup)
                for request, latency in enumerate(measured["latencies"], 1):
                    writer.writerow([rtt, concurrency, repetition, request, f"{latency:.6f}"])
                summary.append({
                    "rtt_ms": rtt,
                    "concurrency": concurrency,
                    "repetition": repetition,
                    "requests": len(measured["latencies"]),
                    "failures": measured["failures"],
                    "product_count": measured["product_count"],
                    "p50_ms": round(percentile(measured["latencies"], 50), 4),
                    "p95_ms": round(percentile(measured["latencies"], 95), 4),
                    "mean_ms": round(statistics.mean(measured["latencies"]), 4),
                    "requests_per_second": round(
                        len(measured["latencies"]) / (measured["durationMs"] / 1000), 2
                    ),
                })
                output.flush()
                if run_index % 10 == 0 or run_index == len(tasks):
                    print(f"Completed {run_index}/{len(tasks)} Aimeos runs", flush=True)
        (RESULTS / f"aimeos_{suffix}_summary.json").write_text(
            json.dumps(summary, indent=2) + "\n"
        )
    finally:
        compose("down", "--volumes")


if __name__ == "__main__":
    main()
