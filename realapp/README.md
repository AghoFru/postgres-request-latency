# Redmine request mix

This track runs the official Redmine container against PostgreSQL. It loads
Redmine's default data, then creates 12 projects and 360 issues with the Rails
models. It replays a read-heavy mix: 60% paginated issue lists, 30% issue details
with journals, and 10% project lists. The script generates an administrator API
key for each run and discards it with the disposable containers.

Run `python3 run.py` from this directory on a Linux host with Docker Compose. The dense sweep tests
0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2, 5, and 10 ms RTT. It uses one
client, three repetitions, five warmup requests, and 20 measured requests per
RTT. It writes raw request timings and run summaries under `../results/`. The
earlier sweep keeps five repetitions at client concurrency one and eight over
its original six RTT values. Docker Compose removes the app and database
containers after each run.

This is a synthetic API workload against the real application. It does not use
production traffic, browser rendering, writes, or human think time. The dataset
and request mix are fixed so that the latency sweep is repeatable.

Run `python3 run.py --sub-ms` for configured RTT settings from 0 to 1 ms. It
uses 20 repetitions, 20 warmup requests, and 100 measured requests per setting.
The results use the `redmine_subms_` file prefix.

Run `DB_LATENCY_TRACE=1 python3 run.py --trace-sql` for a separate trace of
uncached Active Record SQL events by endpoint. Run `python3 run.py
--probe-db-rtt` to measure 200 uncached `SELECT 1` calls through Rails at each
RTT setting. These diagnostic runs do not contribute to the application timing
tables.
