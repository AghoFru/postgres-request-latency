"""Measure Redmine and Aimeos page latency under injected database RTT.

Each invocation runs one phase and writes a new directory under ../runs/:

  fidelity  Measure the RTT that netem produces (TCP echo probe and Rails SELECT 1).
  trace     Count database round trips (packet capture) and statements (pg_stat_statements).
  sweep     Measure page latency over the RTT grid with randomized condition order.

The script needs Docker with the Compose plugin and runs on a Linux host.
"""

import argparse
import csv
import gzip
import hashlib
import importlib.util
import json
import random
import re
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
EXPERIMENTS = PROJECT / "experiments"
SCRATCH = PROJECT / ".note-tmp"
NODE_IMAGE = (
    "node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c"
)
ALPINE_IMAGE = (
    "alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce"
)
COMMAND_TIMEOUT_S = 900
SEED = 20261118
SWEEP_RTTS_MS = [0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1, 1.5, 2]
FIDELITY_RTTS_MS = [0, 0.02, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.75, 1, 2]
METHODS = ("container", "hostveth")
CONTROL_STALL_MS = 0.5
NO_QDISC = None  # A condition with no qdisc at all, as opposed to netem with 0 ms delay.
APPS = {
    "redmine": {
        "compose": PROJECT / "realapp" / "compose.yaml",
        "module": PROJECT / "realapp" / "run.py",
        "app_service": "redmine",
        "app_netns": "shaper-redmine",
        "database": "redmine",
        "routes": ["issue_list", "issue_detail", "project_list"],
    },
    "aimeos": {
        "compose": PROJECT / "apps" / "aimeos" / "compose.benchmark.yaml",
        "module": PROJECT / "apps" / "aimeos" / "run.py",
        "app_service": "app",
        "app_netns": "app",
        "database": "aimeos",
        "routes": ["product_list", "product_detail"],
    },
}
PACKET = re.compile(
    r"^(\d+\.\d+) IP (\S+)\.(\d+) > (\S+)\.(\d+): Flags \[([^\]]*)\].*?length (\d+)"
)


def label(condition):
    return "none" if condition is NO_QDISC else f"{condition:g}"


def run(command, *, timeout=COMMAND_TIMEOUT_S, check=True, input_text=None):
    result = subprocess.run(
        command, text=True, capture_output=True, timeout=timeout, input=input_text,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed with status {result.returncode}: {command[:6]} "
            f"{(result.stderr or result.stdout)[-2000:]}"
        )
    return result


def last_json(text):
    for line in reversed(text.strip().splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError("Command printed no JSON result")


def load_module(path):
    spec = importlib.util.spec_from_file_location(f"note_{path.parent.name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def override_document(app, sysctls):
    config = APPS[app]
    # SELinux hosts block read-only bind mounts of unlabeled files. Disabling the label
    # for the few containers that mount files avoids relabeling files on the host.
    unlabeled = ["label=disable"]
    services = {
        "database": {
            "command": [
                "postgres", "-c", "shared_preload_libraries=pg_stat_statements",
            ],
            "sysctls": sysctls,
        },
        "database-shaper": {"command": [
            "sh", "-c", "apk add --no-cache iproute2 tcpdump >/dev/null && sleep infinity",
        ]},
        "db-echo": {
            "image": NODE_IMAGE,
            "network_mode": "service:database",
            "depends_on": ["database"],
            "command": ["node", "/note/echo_server.mjs"],
            "volumes": [f"{EXPERIMENTS}:/note:ro"],
            "security_opt": unlabeled,
        },
        "probe": {
            "image": NODE_IMAGE,
            "volumes": [f"{EXPERIMENTS}:/note:ro"],
            "security_opt": unlabeled,
        },
        config["app_service"]: {"sysctls": sysctls, "security_opt": unlabeled},
    }
    return {"services": services}


class Stack:
    """One disposable Compose stack with netem control on both sides of the database link."""

    def __init__(self, app, sysctls=None):
        """sysctls apply to both the application and the database network namespace."""
        self.app = app
        self.config = APPS[app]
        self.name = f"note-{app}"
        SCRATCH.mkdir(exist_ok=True)
        override = SCRATCH / f"{self.name}.override.json"
        override.write_text(json.dumps(override_document(app, sysctls or {}), indent=2))
        self.compose_base = [
            "docker", "compose", "--project-name", self.name,
            "-f", str(self.config["compose"]), "-f", str(override),
        ]
        self.module = load_module(self.config["module"])
        self.module.COMPOSE = self.compose_base
        self.helper = f"{self.name}-hostshaper"
        self.veths = {}
        self.applied = set()
        self.issue_ids = None
        self.products = None

    def compose(self, *args, **kwargs):
        return run([*self.compose_base, *args], **kwargs)

    def up(self):
        if self.app == "redmine":
            self.compose("up", "-d", "--wait", timeout=1800)
            self.issue_ids, _ = self.module.prepare_app()
        else:
            self.module.initialize()
            self.compose("up", "-d", "--wait", timeout=1800)
        self.compose("exec", "-T", "database", "psql", "-U", self.config["database"],
                     "-d", self.config["database"], "-c",
                     "CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        self.start_helper()

    def down(self):
        run(["docker", "rm", "-f", self.helper], check=False)
        self.compose("down", "--volumes", check=False, timeout=600)

    def start_helper(self):
        run(["docker", "rm", "-f", self.helper], check=False)
        run(["docker", "run", "-d", "--rm", "--net=host", "--cap-add", "NET_ADMIN",
             "--name", self.helper, ALPINE_IMAGE, "sh", "-c",
             "apk add --no-cache iproute2 >/dev/null && touch /ready && sleep infinity"])
        for _ in range(120):
            if run(["docker", "exec", self.helper, "test", "-f", "/ready"],
                   check=False).returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Host shaper helper did not become ready")
        links = run(["docker", "exec", self.helper, "ip", "-o", "link", "show"]).stdout
        names = {}
        for line in links.splitlines():
            index, name = line.split(": ", 2)[:2]
            names[index] = name.split("@", 1)[0]
        for side, service in (("app", self.config["app_netns"]), ("db", "database-shaper")):
            peer = self.compose("exec", "-T", service, "cat",
                                "/sys/class/net/eth0/iflink").stdout.strip()
            if peer not in names or not names[peer].startswith("veth"):
                raise RuntimeError(f"No host veth peer for the {side} namespace")
            self.veths[side] = names[peer]

    def tc(self, method, side, *args, check=True):
        if method == "container":
            service = self.config["app_netns"] if side == "app" else "database-shaper"
            return self.compose("exec", "-T", service, "tc", "qdisc", *args[:1],
                                "dev", "eth0", *args[1:], check=check)
        return run(["docker", "exec", self.helper, "tc", "qdisc", *args[:1],
                    "dev", self.veths[side], *args[1:]], check=check)

    def set_condition(self, method, condition):
        """Apply half of the RTT on each side of the link, or remove all qdiscs."""
        for other in list(self.applied):
            if other != method or condition is NO_QDISC:
                for side in ("app", "db"):
                    self.tc(other, side, "del", "root", check=False)
                self.applied.discard(other)
        if condition is NO_QDISC:
            return
        for side in ("app", "db"):
            self.tc(method, side, "replace", "root", "netem", "delay", f"{condition / 2:g}ms")
        self.applied.add(method)

    def qdisc_report(self):
        report = {}
        for method in METHODS:
            for side in ("app", "db"):
                shown = self.tc(method, side, "show", check=False).stdout.strip()
                report[f"{method}_{side}"] = shown.split(" refcnt")[0]
        return report

    def traffic(self, count, warmup, route=""):
        env = ["-e", f"ROUTE={route}"]
        if self.app == "redmine":
            env += ["-e", f"ISSUE_IDS={self.issue_ids}"]
        elif self.products:
            env += ["-e", f"PRODUCTS={json.dumps(self.products)}"]
        result = self.compose("exec", "-T", *env, "probe", "node", "/work/traffic.mjs",
                              "measure", str(count), "1", str(warmup), timeout=1200)
        measured = last_json(result.stdout)
        if measured["failures"]:
            raise RuntimeError(f"{self.app} had {measured['failures']} failed requests")
        if self.app == "aimeos":
            self.products = measured["products"]
        return measured

    def tcp_probe(self, samples, gap_ms):
        result = self.compose("exec", "-T", "probe", "node", "/note/rtt_probe.mjs",
                              "database", str(samples), f"{gap_ms:g}", timeout=600)
        return last_json(result.stdout)["samples"]

    def select1_probe(self, iterations):
        result = self.compose("exec", "-T", "-e", f"DB_PROBE_ITERATIONS={iterations}",
                              "redmine", "bundle", "exec", "rails", "runner",
                              "/work/db_round_trip_probe.rb", timeout=600)
        return last_json(result.stdout)["samples_ms"]

    def psql(self, sql):
        return self.compose("exec", "-T", "database", "psql", "-At", "-v", "ON_ERROR_STOP=1",
                            "-U", self.config["database"], "-d", self.config["database"],
                            "-c", sql).stdout


class PmQos:
    """Hold /dev/cpu_dma_latency at 0 so that idle CPUs stay in shallow C-states."""

    name = "note-pmqos-hold"

    def __enter__(self):
        run(["docker", "rm", "-f", self.name], check=False)
        run(["docker", "run", "-d", "--rm", "--privileged", "--name", self.name, ALPINE_IMAGE,
             "sh", "-c", ("exec 3>/dev/cpu_dma_latency && printf '\\000\\000\\000\\000' >&3 "
                          "&& exec sleep infinity")])
        time.sleep(2)
        return self

    def __exit__(self, *_):
        run(["docker", "rm", "-f", self.name], check=False)


def deep_idle_entries_per_s(seconds=3):
    """Count entries into the deepest cpuidle state across all CPUs over a short window."""
    def total():
        paths = sorted(Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpuidle/state*/usage"))
        deepest = max(int(p.parent.name.removeprefix("state")) for p in paths)
        return sum(int(p.read_text()) for p in paths if p.parent.name == f"state{deepest}")
    before = total()
    time.sleep(seconds)
    return (total() - before) / seconds


def fidelity_tcp_blocks(stack, writer, arm, methods, rng, samples, repetitions):
    tasks = [
        (method, condition, gap, repetition)
        for method in methods
        for condition in [NO_QDISC, *FIDELITY_RTTS_MS]
        for gap in (0, 1)
        for repetition in range(1, repetitions + 1)
    ]
    rng.shuffle(tasks)
    for block, (method, condition, gap, repetition) in enumerate(tasks, 1):
        stack.set_condition(method, condition)
        for index, value in enumerate(stack.tcp_probe(samples, gap), 1):
            writer.writerow([arm, method, label(condition), gap, repetition, block, index, value])
    print(f"{arm}: {len(tasks)} TCP probe blocks done", flush=True)
    return [list(task[:1]) + [label(task[1])] + list(task[2:]) for task in tasks]


def fidelity(run_dir, args, metadata):
    rng = random.Random(SEED)
    orders = {}
    tcp_path = run_dir / "fidelity_tcp.csv.gz"
    with gzip.open(tcp_path, "wt", newline="") as tcp_file:
        tcp = csv.writer(tcp_file)
        tcp.writerow(["arm", "method", "rtt_ms", "gap_ms", "repetition", "block", "sample",
                      "measured_rtt_ms"])
        stack = Stack("redmine")
        try:
            stack.up()
            metadata["qdisc_example"] = qdisc_example(stack)
            orders["default"] = fidelity_tcp_blocks(stack, tcp, "default", METHODS, rng,
                                                    args.samples, args.repetitions)
            select1(stack, run_dir, rng, args)
            metadata["deep_idle_entries_per_s"] = {"default": deep_idle_entries_per_s()}
            with PmQos():
                metadata["deep_idle_entries_per_s"]["pmqos_hold"] = deep_idle_entries_per_s()
                orders["pmqos_hold"] = fidelity_tcp_blocks(
                    stack, tcp, "pmqos_hold", ("container",), rng, args.samples,
                    args.repetitions)
        finally:
            stack.down()
        stack = Stack("redmine", sysctls={"net.ipv4.tcp_autocorking": "0"})
        try:
            stack.up()
            orders["no_autocork"] = fidelity_tcp_blocks(
                stack, tcp, "no_autocork", ("container",), rng, args.samples, args.repetitions)
        finally:
            stack.down()
    metadata["block_orders"] = orders


def qdisc_example(stack):
    stack.set_condition("container", 0.1)
    container = stack.qdisc_report()
    stack.set_condition("hostveth", 0.1)
    host = stack.qdisc_report()
    stack.set_condition("container", NO_QDISC)
    return {"container_0.1ms": container, "hostveth_0.1ms": host}


def select1(stack, run_dir, rng, args):
    tasks = [
        (method, condition, repetition)
        for method in METHODS
        for condition in [NO_QDISC, *FIDELITY_RTTS_MS]
        for repetition in (1, 2)
    ]
    rng.shuffle(tasks)
    with gzip.open(run_dir / "fidelity_select1.csv.gz", "wt", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["method", "rtt_ms", "repetition", "block", "sample", "latency_ms"])
        for block, (method, condition, repetition) in enumerate(tasks, 1):
            stack.set_condition(method, condition)
            for index, value in enumerate(stack.select1_probe(args.select1_iterations), 1):
                writer.writerow([method, label(condition), repetition, block, index, value])
    print(f"Rails SELECT 1: {len(tasks)} blocks done", flush=True)


def attribute_packets(lines, starts, ends, rtt_ms=0.0):
    """Count, per request window, TCP handshakes and client flights that the server answered.

    A stall is a pause inside one side's data flight that waits for a pure ACK from the other
    side, for example when the congestion window is smaller than a response. Each stall costs
    one more RTT but has no data reply, so answered flights do not count it. A pause counts
    when it is at least half the RTT and the other side sent a pure ACK during it. At RTT 0
    the threshold is CONTROL_STALL_MS, which measures how often slow server work alone looks
    like a stall.
    """
    packets = []
    for line in lines:
        match = PACKET.match(line)
        if match:
            stamp, _, sport, _, dport, flags, length = match.groups()
            packets.append((float(stamp) * 1000, sport, dport, flags, int(length)))
    rows = [{"handshakes": 0, "answered_flights": 0, "stalls": 0, "packets": 0}
            for _ in starts]
    pending = {}
    last_data = {}
    acked_since = {}
    stall_ms = rtt_ms / 2 if rtt_ms > 0 else CONTROL_STALL_MS
    unassigned = 0
    window = 0
    for stamp, sport, dport, flags, length in sorted(packets):
        while window < len(ends) and stamp > ends[window]:
            window += 1
        if window >= len(starts) or stamp < starts[window]:
            unassigned += 1
            continue
        row = rows[window]
        row["packets"] += 1
        to_server = dport == "5432"
        client = sport if to_server else dport
        side = "client" if to_server else "server"
        other = "server" if to_server else "client"
        if length > 0:
            previous = last_data.get(client)
            if (previous and previous[0] == side
                    and stamp - previous[1] >= stall_ms and acked_since.get((client, other))):
                row["stalls"] += 1
            last_data[client] = (side, stamp)
            acked_since[(client, other)] = False
        elif flags == ".":
            acked_since[(client, side)] = True
        if to_server and flags == "S":
            row["handshakes"] += 1
        elif length > 0 and to_server:
            pending[client] = True
        elif length > 0 and pending.get(client):
            row["answered_flights"] += 1
            pending[client] = False
    for row in rows:
        row["round_trips"] = row["handshakes"] + row["answered_flights"]
    return rows, unassigned, len(packets)


def capture_round_trips(stack, count, rtt_ms):
    stack.compose("exec", "-T", "database-shaper", "rm", "-f", "/tmp/note.pcap")
    stack.compose("exec", "-d", "database-shaper", "tcpdump", "-i", "eth0", "-nn", "-U",
                  "-w", "/tmp/note.pcap", "tcp", "port", "5432")
    time.sleep(2)
    measured = stack.traffic(count, 0)
    time.sleep(1)
    stack.compose("exec", "-T", "database-shaper", "pkill", "-INT", "tcpdump")
    time.sleep(2)
    text = stack.compose("exec", "-T", "database-shaper", "tcpdump", "-nn", "-tt", "-r",
                         "/tmp/note.pcap").stdout
    stack.compose("exec", "-T", "database-shaper", "rm", "-f", "/tmp/note.pcap")
    rows, unassigned, total = attribute_packets(
        text.splitlines(), measured["startsEpochMs"], measured["endsEpochMs"], rtt_ms)
    return measured, rows, unassigned, total


STATEMENTS_SQL = (
    "SELECT coalesce(json_agg(json_build_object('calls', calls, 'query', "
    "left(regexp_replace(query, '\\s+', ' ', 'g'), 300)) ORDER BY calls DESC), '[]') "
    "FROM pg_stat_statements WHERE query !~* 'pg_stat_statements'"
)


def trace_statements(stack, args):
    """Count statements per request for each route with pg_stat_statements."""
    output = {}
    for route in stack.config["routes"]:
        stack.traffic(10, 0, route)
        stack.psql("SELECT pg_stat_statements_reset()")
        stack.traffic(args.trace_route_requests, 0, route)
        statements = json.loads(stack.psql(STATEMENTS_SQL).strip())
        calls = sum(item["calls"] for item in statements)
        output[route] = {
            "requests": args.trace_route_requests,
            "statement_calls": calls,
            "statements_per_request": calls / args.trace_route_requests,
            "statements": statements,
        }
    return output


def trace(run_dir, args, metadata):
    stack = Stack(args.app, sysctls=args.sysctls)
    summary = {}
    try:
        stack.up()
        metadata["images"] = stack.compose("images", "--format", "json").stdout.strip()
        stack.set_condition(args.method, 0)
        stack.traffic(100, 20)
        with (run_dir / "trace_round_trips.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["rtt_ms", "request", "route", "path", "latency_ms", "handshakes",
                             "answered_flights", "round_trips", "stalls", "packets"])
            for condition in args.trace_rtts:
                stack.set_condition(args.method, condition)
                measured, rows, unassigned, total = capture_round_trips(
                    stack, args.trace_requests, condition)
                for index, row in enumerate(rows):
                    writer.writerow([
                        label(condition), index + 1, measured["routes"][index],
                        measured["paths"][index], round(measured["latencies"][index], 4),
                        row["handshakes"], row["answered_flights"], row["round_trips"],
                        row["stalls"], row["packets"],
                    ])
                summary[label(condition)] = {"packets": total, "unassigned_packets": unassigned}
        stack.set_condition(args.method, 0)
        statements = trace_statements(stack, args)
    finally:
        stack.down()
    metadata["packet_capture"] = summary
    (run_dir / "trace_statements.json").write_text(json.dumps(statements, indent=2) + "\n")


def write_requests(writer, args, run_index, phase, condition, repetition, measured):
    for index, latency in enumerate(measured["latencies"]):
        writer.writerow([args.app, args.method, run_index, phase, label(condition), repetition,
                         index + 1, measured["routes"][index], measured["paths"][index],
                         round(latency, 4)])


def sweep(run_dir, args, metadata):
    conditions = [NO_QDISC, *args.rtts] if args.include_none else list(args.rtts)
    tasks = [(c, rep) for rep in range(1, args.runs + 1) for c in conditions]
    random.Random(SEED).shuffle(tasks)
    metadata["order"] = [[label(c), rep] for c, rep in tasks]
    deadline = time.monotonic() + args.budget_s
    stack = Stack(args.app, sysctls=args.sysctls)
    with (run_dir / "sweep_raw.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["app", "method", "run_index", "phase", "rtt_ms", "repetition",
                         "request", "route", "path", "latency_ms"])
        try:
            stack.up()
            metadata["images"] = stack.compose("images", "--format", "json").stdout.strip()
            stack.set_condition(args.method, 0)
            measured = stack.traffic(args.requests, args.warmup)
            write_requests(writer, args, 0, "cold_start_discarded", 0, 0, measured)
            for run_index, (condition, repetition) in enumerate(tasks, 1):
                if time.monotonic() > deadline:
                    metadata["stopped_by_budget_after_runs"] = run_index - 1
                    break
                stack.set_condition(args.method, condition)
                measured = stack.traffic(args.requests, args.warmup)
                write_requests(writer, args, run_index, "measured", condition, repetition,
                               measured)
                handle.flush()
                if run_index % 10 == 0 or run_index == len(tasks):
                    print(f"{args.app}: {run_index}/{len(tasks)} runs", flush=True)
        finally:
            stack.down()


def environment():
    def out(command):
        return run(command, check=False).stdout.strip()

    release = out(["uname", "-r"])
    config_path = Path(f"/boot/config-{release}")
    kernel_config = [
        line for line in (config_path.read_text().splitlines() if config_path.exists() else [])
        if re.match(r"(# )?CONFIG_(HZ=|HZ_|NO_HZ|HIGH_RES_TIMERS|NET_SCH_NETEM)", line)
    ]
    idle = [
        {"name": (state / "name").read_text().strip(),
         "exit_latency_us": int((state / "latency").read_text())}
        for state in sorted(Path("/sys/devices/system/cpu/cpu0/cpuidle").glob("state*"))
    ]
    cpu = re.search(r"Model name:\s*(.+)", out(["lscpu"]))
    files = sorted(
        [*EXPERIMENTS.glob("*.py"), *EXPERIMENTS.glob("*.mjs"), *PROJECT.glob("realapp/*.*"),
         *[PROJECT / "apps" / "aimeos" / name for name in (
             "run.py", "traffic.mjs", "compose.benchmark.yaml", "Dockerfile.benchmark",
             "entrypoint.benchmark.sh", "composer.lock")]])

    return {
        "kernel": release,
        "kernel_config": kernel_config,
        "cpu_model": cpu.group(1) if cpu else None,
        "cpu_threads": len(list(Path("/sys/devices/system/cpu").glob("cpu[0-9]*"))),
        "cpufreq_governor": out(["cat", "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"]),
        "cpuidle_states": idle,
        "clocksource": out(["cat", "/sys/devices/system/clocksource/clocksource0/"
                                   + "current_clocksource"]),
        "tcp_autocorking": out(["cat", "/proc/sys/net/ipv4/tcp_autocorking"]),
        "tcp_limit_output_bytes": out(["cat", "/proc/sys/net/ipv4/tcp_limit_output_bytes"]),
        "tcp_slow_start_after_idle": out(["cat",
                                          "/proc/sys/net/ipv4/tcp_slow_start_after_idle"]),
        "loadavg_at_start": out(["cat", "/proc/loadavg"]),
        "docker_server": out(["docker", "version", "--format", "{{.Server.Version}}"]),
        "docker_compose": out(["docker", "compose", "version", "--short"]),
        "python": sys.version.split()[0],
        "aimeos_commit": out(["git", "-C", str(PROJECT / "apps" / "aimeos"), "rev-parse",
                              "HEAD"]),
        "aimeos_local_changes": out(["git", "-C", str(PROJECT / "apps" / "aimeos"), "status",
                                     "--porcelain"]).splitlines(),
        "sha256": {
            str(path.relative_to(PROJECT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files if path.is_file()
        },
    }


def scrub(text):
    """Keep machine-local paths out of the saved record."""
    return text.replace(str(PROJECT), "<project>").replace(str(Path.home()), "~")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["fidelity", "trace", "sweep"])
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--app", choices=sorted(APPS), default="redmine")
    parser.add_argument("--method", choices=METHODS, default="container")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--select1-iterations", type=int, default=500)
    parser.add_argument("--trace-requests", type=int, default=100)
    parser.add_argument("--trace-route-requests", type=int, default=30)
    parser.add_argument("--trace-rtts", type=float, nargs="+", default=[0, 1])
    parser.add_argument("--budget-s", type=int, default=3 * 3600)
    parser.add_argument("--rtts", type=float, nargs="+", default=SWEEP_RTTS_MS)
    parser.add_argument("--no-none", dest="include_none", action="store_false",
                        help="Leave out the condition with no qdisc.")
    parser.add_argument("--sysctl", action="append", default=[], metavar="KEY=VALUE",
                        help="Network sysctl for both namespaces, for example "
                             "net.ipv4.tcp_slow_start_after_idle=0.")
    args = parser.parse_args()
    args.sysctls = {}
    for item in args.sysctl:
        key, _, value = item.partition("=")
        if not re.fullmatch(r"net\.ipv4\.[a-z_]+", key) or not re.fullmatch(r"[0-9]+", value):
            parser.error(f"--sysctl must look like net.ipv4.name=NUMBER, not {item}")
        args.sysctls[key] = value
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,80}", args.run_name):
        parser.error("--run-name must be lowercase words joined by hyphens")
    run_dir = PROJECT / "runs" / args.run_name
    run_dir.mkdir(parents=True, exist_ok=False)
    metadata = {
        "phase": args.phase,
        "command": "python3 experiments/realapp_note.py " + " ".join(sys.argv[1:]),
        "args": vars(args),
        "seed": SEED,
        "sweep_rtts_ms": SWEEP_RTTS_MS,
        "fidelity_rtts_ms": FIDELITY_RTTS_MS,
        "code_version": "No version control. See the sha256 values of the scripts.",
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": environment(),
        "status": "running",
    }
    phases = {"fidelity": fidelity, "trace": trace, "sweep": sweep}
    try:
        phases[args.phase](run_dir, args, metadata)
        metadata["status"] = "complete"
    except BaseException as error:
        metadata["status"] = "failed"
        metadata["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        metadata["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        metadata["loadavg_at_end"] = Path("/proc/loadavg").read_text().strip()
        (run_dir / "metadata.json").write_text(scrub(json.dumps(metadata, indent=2)) + "\n")


if __name__ == "__main__":
    main()
