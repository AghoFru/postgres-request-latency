# Injected database RTT in Redmine and Aimeos

A measurement of how median page latency in two real web applications changes when Linux
`netem` adds 0 to 2 ms of round-trip time (RTT) between the application and PostgreSQL.

The scripts run each application and its database in Docker, inject the RTT on both sides of
the database link, and replay a fixed request mix with one client. A packet capture counts
the sequential database round trips of each page, and the fitted slope of p50 latency against
RTT is compared with that count.

## Result

This is a replication note. It repeats the queries-times-RTT model of page latency on two
unmodified applications. It does not propose a new model or mechanism.

On one x86_64 host (AMD Ryzen AI Max+ 395, Linux 7.2), `netem` added the configured RTT within
0.03 ms from 0.02 to 2 ms. Packet captures gave fixed sequential database round-trip counts per
page (8 to 112) in Redmine 6.1.4 and Aimeos. With the containers pinned to one CCD, p50 page
latency rose with injected RTT in the order of the counts, and the 0 to 2 ms least-squares
slopes were 18 to 32% above the counts.

Most of that excess came from one step: between 0.4 and 0.5 ms RTT in Redmine and between 0.5
and 0.75 ms in Aimeos. Container CPU time per request stepped up at the same place. Away from
the step, the slopes of single pages were 5 to 17% above the counts.

| Page | Round trips | 0 to 2 ms slope [95% CI] | Ratio 0 to 2 ms | Below step | Above step |
| --- | ---: | --- | ---: | ---: | ---: |
| Redmine issue list | 41 | 52.0 [51.3, 57.3] | 1.27 | 1.07 | 1.07 |
| Redmine issue detail | 20 | 23.6 [23.2, 26.9] | 1.18 | 1.13 | 1.08 |
| Redmine project list | 8 | 9.8 [9.6, 11.2] | 1.22 | 1.09 | 1.10 |
| Aimeos listing, page 0 (price) | 112 | 133.5 [119.6, 147.4] | 1.19 | 1.05 | 1.09 |
| Aimeos listing, page 2 (price) | 39 | 49.1 [43.5, 54.7] | 1.26 | 1.06 | 1.14 |
| Aimeos product detail | 43 | 55.5 [49.6, 61.9] | 1.29 | 1.09 | 1.17 |

Slopes are in ms of p50 per ms of RTT. The 0 to 2 ms slopes use all 20 runs of 200 requests at
each of 11 settings. The host was shared: other processes used more than 2 of the 16 pinned
threads in 49 of 240 Redmine runs and 67 of 240 Aimeos runs. The ratios on each side of the
step use the runs without that load. "Below step" fits the settings under the step, and "Above
step" fits the settings from its upper edge to 2 ms.

In the runs without foreign load, round trips plus the growth of container CPU time per
request, without idle CPU, approximately match the slope of mean latency: 38.4 against 39.0 ms
per ms in Redmine, and 84.6 against 85.3 in Aimeos. Container CPU time per request in Redmine
was 29.7 ms at 0 ms RTT and 45.8 ms at 2 ms (medians over runs).

Each pinned Redmine control setting ran once, as one sweep of 10 runs at 4 RTT settings (0,
0.5, 1, and 2 ms). With deep CPU idle states held off, the issue-list slope was 42.5 ms per ms,
1.04 times its 41 round trips (default settings: 48.9). Earlier unpinned controls showed no
effect of the same settings (issue-list slopes 55.4 to 57.4).

Details, all pages, controls, and limits are in the note:
[paper/Injected Database Round-Trip Time and Page Latency in Redmine and Aimeos.pdf](paper/Injected%20Database%20Round-Trip%20Time%20and%20Page%20Latency%20in%20Redmine%20and%20Aimeos.pdf).
Raw data and run metadata are in `runs/`. Derived values are in `results/note/summary.json`
and `results/note/step.json`.

## Prior work

The model and the mechanism are prior work. Round trips per page and their cost at 0.5 to
10 ms of delay: [Sloth (SIGMOD 2014)](https://people.eecs.berkeley.edu/~akcheung/papers/sigmod14.html)
and [Pyxis (PVLDB 2012)](https://people.eecs.berkeley.edu/~akcheung/papers/vldb12.html). The
cost of ORM data-access patterns:
[Chen et al. (ICSE 2014)](https://ece.uwaterloo.ca/~wshang/pubs/peter_icse2014.pdf),
[Chen et al. (TSE 2016)](https://ece.uwaterloo.ca/~wshang/pubs/peter_tse2016.pdf), and
[Yang et al. (ICSE 2018)](https://hyperloop-rails.github.io/220-HowNotStructure.pdf). The
accuracy of `netem` and other link emulators:
[Popescu, Zilberman, and Moore (2017)](https://www.cl.cam.ac.uk/techreports/UCAM-CL-TR-914.pdf),
[Nussbaum and Richard (2009)](https://members.loria.fr/lnussbaum/files/netemulators-cns09.pdf),
and [Lübke et al. (2014)](https://www.rn.inf.tu-dresden.de/uploads/Publikationen/lubke2014measuring0.pdf).
CPU sleep states and frequency scaling as a source of request latency:
[Kanev et al. (IISWC 2014)](https://vlsiarch.eecs.harvard.edu/publications/tradeoffs-between-power-management-and-tail-latency-warehouse-scale),
[Tales of the Tail (SoCC 2014)](https://homes.cs.washington.edu/~drkp/papers/latency-tr14.pdf),
[μDPM (HPCA 2019)](https://par.nsf.gov/servlets/purl/10109256),
[AgileWatts](https://arxiv.org/abs/2203.02550), and
[AgilePkgC](https://arxiv.org/abs/2204.10466).

## Limits

One host, shared with other processes. `netem` delay only, one client at a time, and synthetic
request mixes on small seeded data sets. The fidelity probe and the packet traces ran without
CPU pinning. The PostgreSQL, Redmine, Alpine, and Redmine-side Node.js images are pinned by
digest. The Aimeos application image builds from the `php:8.4-cli-bookworm` tag and the Aimeos
probe uses the `node:22-bookworm-slim` tag, both without a digest. Each run's `metadata.json`
records the image IDs that ran.

## Reproduce

On a Linux host with Docker and the Compose plugin, fetch Aimeos at the pinned commit, then run
each phase into a new run directory:

```sh
aimeos-benchmark/prepare.sh
uv run --no-project python experiments/realapp_note.py fidelity --run-name my-fidelity
uv run --no-project python experiments/realapp_note.py trace --app redmine --trace-rtts 0 1 2 --run-name my-redmine-trace
uv run --no-project python experiments/realapp_note.py sweep --app redmine --runs 20 \
    --cpuset-app 8-11,24-27 --cpuset-db 12-14,28-30 --cpuset-client 15,31 \
    --run-name my-redmine-sweep
```

Use `--app aimeos` for Aimeos. Choose CPU sets that exist on your host. Then summarize with
`uv run --no-project python experiments/analyze_note.py --fidelity my-fidelity --trace my-redmine-trace --sweep my-redmine-sweep`.
`experiments/step_note.py` locates the step and fits each side of it for the sweeps named in
`experiments/note_tables.py`.

## License

The project code and the Aimeos benchmark files in `aimeos-benchmark/` are under the MIT
license in `LICENSE`. Aimeos itself is MIT-licensed by its authors.
