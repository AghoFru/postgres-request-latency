"""Run a repeatable read-heavy request mix against the Redmine application."""

import csv
import argparse
import json
import math
import os
import random
import re
import secrets
import statistics
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMPOSE = [
    "docker", "compose", "--project-name", "db-latency-redmine",
    "-f", str(ROOT / "compose.yaml"),
]
RTT_MS = [0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2, 5, 10]
SUB_MS_RTT = [0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1]
# Bound every Compose call so that a stuck container cannot hold a sweep open.
COMMAND_TIMEOUT_S = 1800


def compose(*args, capture=False, input_text=None, redact=()):
    command = [*COMPOSE, *args]
    secrets = set(redact)
    try:
        return subprocess.run(
            command, check=True, text=True, capture_output=capture,
            input=input_text, timeout=COMMAND_TIMEOUT_S,
        )
    except subprocess.CalledProcessError as error:
        detail = "\n".join(part for part in (error.stderr, error.stdout) if part)
        detail = detail or "command failed"
        for secret in secrets:
            detail = detail.replace(secret, "[redacted]")
        raise RuntimeError(
            f"Compose command failed with status {error.returncode}: {detail.strip()}"
        ) from None


def percentile(values, percent):
    return sorted(values)[math.ceil(len(values) * percent / 100) - 1]


def shape(service, delay_ms):
    compose(
        "exec", "-T", service, "tc", "qdisc", "replace", "dev", "eth0",
        "root", "netem", "delay", f"{delay_ms:g}ms",
    )


def prepare_app():
    """Load default data, enable the API, and seed issues. Return issue IDs and the seed summary."""
    compose(
        "exec", "-T", "-e", "REDMINE_LANG=en", "redmine", "bundle", "exec",
        "rake", "redmine:load_default_data", "RAILS_ENV=production",
    )
    api_key = secrets.token_hex(20)
    sql = f"""
    DO $$
    DECLARE admin_id integer;
    BEGIN
      SELECT id INTO admin_id FROM users WHERE login = 'admin';
      IF admin_id IS NULL THEN
        RAISE EXCEPTION 'Redmine admin user is missing';
      END IF;
      DELETE FROM tokens WHERE user_id = admin_id AND action = 'api';
      INSERT INTO tokens (user_id, action, value, created_on, updated_on)
        VALUES (admin_id, 'api', '{api_key}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
      UPDATE settings
        SET value = '1', updated_on = CURRENT_TIMESTAMP
        WHERE name = 'rest_api_enabled';
      IF NOT FOUND THEN
        INSERT INTO settings (name, value, updated_on)
          VALUES ('rest_api_enabled', '1', CURRENT_TIMESTAMP);
      END IF;
    END $$;
    """
    update = compose(
        "exec", "-T", "database", "psql", "-v", "ON_ERROR_STOP=1",
        "-U", "redmine", "-d", "redmine", capture=True, input_text=sql,
        redact=(api_key,),
    )
    if "DO" not in update.stdout:
        raise RuntimeError("The Redmine API key or REST API setting was not updated")
    compose(
        "exec", "-T", "probe", "sh", "-c",
        "umask 077 && cat > /run/secrets/redmine_api_key",
        capture=True, input_text=api_key, redact=(api_key,),
    )
    print("Seeding Redmine projects and issues", flush=True)
    seeded = compose(
        "exec", "-T", "redmine", "bundle", "exec", "rails", "runner",
        "/work/seed.rb", capture=True,
    )
    seed_result = next(
        json.loads(line) for line in reversed(seeded.stdout.splitlines())
        if line.startswith('{')
    )
    issue_ids = json.dumps(seed_result["issues"], separators=(",", ":"))
    return issue_ids, seed_result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sub-ms", action="store_true")
    parser.add_argument("--trace-sql", action="store_true")
    parser.add_argument("--probe-db-rtt", action="store_true")
    args = parser.parse_args()
    if args.trace_sql and os.environ.get("DB_LATENCY_TRACE") != "1":
        parser.error("--trace-sql requires DB_LATENCY_TRACE=1")
    rtts = SUB_MS_RTT if args.sub_ms else RTT_MS
    repetitions = 20 if args.sub_ms else 3
    requests = 100 if args.sub_ms else 20
    warmup = 20 if args.sub_ms else 5
    trace_stage = ROOT.parent / ".work" / "redmine-trace-init.rb"
    trace_stage.parent.mkdir(exist_ok=True)
    compose("up", "-d", "--wait")
    try:
        if args.trace_sql:
            compose(
                "cp", "probe:/work/db_latency_trace.rb", str(trace_stage),
            )
            compose(
                "cp", str(trace_stage),
                "redmine:/usr/src/redmine/config/initializers/db_latency_trace.rb",
            )
            trace_stage.unlink()
            compose("restart", "redmine")
            compose("up", "-d", "--force-recreate", "shaper-redmine", "probe")
            compose("up", "-d", "--wait")

        if args.probe_db_rtt:
            results = []
            tasks = [
                (rtt, repetition)
                for repetition in range(1, 6)
                for rtt in SUB_MS_RTT
            ]
            random.Random(20260926).shuffle(tasks)
            for rtt, repetition in tasks:
                shape("shaper-redmine", rtt / 2)
                shape("database-shaper", rtt / 2)
                probe = compose(
                    "exec", "-T", "redmine", "bundle", "exec", "rails", "runner",
                    "/work/db_round_trip_probe.rb", capture=True,
                )
                measured = json.loads(probe.stdout.strip().splitlines()[-1])
                results.append({
                    "variant": "rails_postgres_select_1",
                    "rtt_ms": rtt,
                    "repetition": repetition,
                    "requests": measured["iterations"],
                    "p50_ms": measured["median_ms"],
                    "p95_ms": measured["p95_ms"],
                    "mean_ms": measured["mean_ms"],
                })
                if len(results) % 10 == 0 or len(results) == len(tasks):
                    print(f"Completed {len(results)}/{len(tasks)} query probes", flush=True)
            target = ROOT.parent / "results" / "redmine_db_round_trip_subms_summary.json"
            target.write_text(json.dumps(results, indent=2) + "\n")
            return

        issue_ids, seed_result = prepare_app()

        if args.trace_sql:
            measured = compose(
                "exec", "-T", "-e", f"ISSUE_IDS={issue_ids}", "probe",
                "node", "/work/traffic.mjs", "measure", "100", "1", "0",
                capture=True,
            )
            result = json.loads(measured.stdout.strip().splitlines()[-1])
            if result["failures"]:
                logs = compose("logs", "--no-color", "redmine", capture=True).stdout
                raise RuntimeError(
                    f"Redmine had {result['failures']} trace request failures. "
                    f"Recent app logs: {logs[-3000:]}"
                )
            logs = compose(
                "logs", "--no-color", "redmine", capture=True
            ).stdout
            marker = "DB_LATENCY_TRACE "
            groups = {}
            for line in logs.splitlines():
                if marker not in line:
                    continue
                payload = line.split(marker, 1)[1]
                start, end = payload.find("{"), payload.rfind("}")
                if start < 0 or end < start:
                    continue
                event = json.loads(payload[start:end + 1])
                if not event["route"]:
                    continue
                if event["route"] == "/login":
                    continue
                key = re.sub(r"^/issues/\d+\.json$", "/issues/:id.json", event["route"])
                group = groups.setdefault(key, {
                    "requests": 0, "sql_calls": [], "sql_ms": [],
                })
                group["requests"] += 1
                group["sql_calls"].append(event["uncached_sql_calls"])
                group["sql_ms"].append(event["sql_ms"])
            if not groups:
                raise RuntimeError(
                    f"No SQL trace entries found in {len(logs)} bytes of app logs"
                )
            output = [
                {
                    "route": route,
                    "requests": group["requests"],
                    "sql_calls_per_request": {
                        "min": min(group["sql_calls"]),
                        "median": statistics.median(group["sql_calls"]),
                        "max": max(group["sql_calls"]),
                    },
                    "median_sql_ms": statistics.median(group["sql_ms"]),
                }
                for route, group in sorted(groups.items())
            ]
            target = ROOT.parent / "results" / "redmine_sql_trace.json"
            target.write_text(json.dumps(output, indent=2) + "\n")
            print(json.dumps(output, indent=2))
            return

        tasks = [
            (rtt, 1, repetition, requests)
            for repetition in range(1, repetitions + 1)
            for rtt in rtts
        ]
        random.Random(20260926).shuffle(tasks)
        suffix = "subms" if args.sub_ms else "fine"
        raw_path = ROOT.parent / "results" / f"redmine_{suffix}_raw.csv"
        summary = []
        with raw_path.open("w", newline="") as output:
            writer = csv.writer(output)
            writer.writerow([
                "rtt_ms", "concurrency", "repetition", "request", "latency_ms",
            ])
            for run_index, (rtt, concurrency, repetition, count) in enumerate(tasks, 1):
                shape("shaper-redmine", rtt / 2)
                shape("database-shaper", rtt / 2)
                measured = compose(
                    "exec", "-T", "-e", f"ISSUE_IDS={issue_ids}", "probe",
                    "node", "/work/traffic.mjs", "measure", str(count),
                    str(concurrency), str(warmup),
                    capture=True,
                )
                result = json.loads(measured.stdout.strip().splitlines()[-1])
                if result["failures"]:
                    raise RuntimeError(f"Redmine had {result['failures']} failures")
                values = result["latencies"]
                for request, latency in enumerate(values, 1):
                    writer.writerow([rtt, concurrency, repetition, request, f"{latency:.6f}"])
                summary.append({
                    "rtt_ms": rtt,
                    "concurrency": concurrency,
                    "repetition": repetition,
                    "requests": len(values),
                    "failures": result["failures"],
                    "p50_ms": round(percentile(values, 50), 4),
                    "p95_ms": round(percentile(values, 95), 4),
                    "mean_ms": round(statistics.mean(values), 4),
                    "requests_per_second": round(
                        len(values) / (result["durationMs"] / 1000), 2
                    ),
                })
                output.flush()
                if run_index % 10 == 0 or run_index == len(tasks):
                    print(f"Completed {run_index}/{len(tasks)} Redmine runs", flush=True)
        (ROOT.parent / "results" / f"redmine_{suffix}_summary.json").write_text(
            json.dumps(summary, indent=2) + "\n"
        )
        print(f"Seeded {seed_result['projects']} projects and {len(seed_result['issues'])} issues")
    finally:
        trace_stage.unlink(missing_ok=True)
        compose("down")


if __name__ == "__main__":
    main()
