import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const base = "http://127.0.0.1:3000";
const apiKey = readFileSync("/run/secrets/redmine_api_key", "utf8");
assert(apiKey, "Redmine API key file is required");
const issueIds = JSON.parse(process.env.ISSUE_IDS ?? "[]");
// An optional ROUTE limits the mix to one route, for per-route query tracing.
const routeFilter = process.env.ROUTE ?? "";
const routes = ["issue_list", "issue_detail", "project_list"];
assert(routeFilter === "" || routes.includes(routeFilter), `Unknown ROUTE ${routeFilter}`);

const routeFor = (index) => {
  if (routeFilter) return routeFilter;
  const choice = index % 10;
  return choice < 6 ? "issue_list" : choice < 9 ? "issue_detail" : "project_list";
};

const pathFor = (route, index) => {
  if (route === "issue_list") return `/issues.json?limit=50&status_id=*&offset=${(index % 4) * 50}`;
  if (route === "issue_detail") {
    return `/issues/${issueIds[(index * 73) % issueIds.length]}.json?include=journals`;
  }
  return "/projects.json?limit=25";
};

if (process.argv[2] === "measure") {
  assert(issueIds.length > 0, "ISSUE_IDS is required");
  const count = Number(process.argv[3]);
  const concurrency = Number(process.argv[4]);
  const warmup = Number(process.argv[5]);
  assert(Number.isInteger(count) && count > 0 && count <= 100000);
  assert(Number.isInteger(concurrency) && concurrency > 0 && concurrency <= 64);
  assert(Number.isInteger(warmup) && warmup >= 0 && warmup <= 100000);

  let next = 0;
  let failures = 0;
  const latencies = [];
  const routeLog = [];
  const pathLog = [];
  const startsEpochMs = [];
  const endsEpochMs = [];
  const request = async (record) => {
    const index = next;
    next += 1;
    const route = routeFor(index);
    const path = pathFor(route, index);
    const startEpoch = performance.timeOrigin + performance.now();
    const start = performance.now();
    try {
      const response = await fetch(`${base}${path}`, {
        headers: { "X-Redmine-API-Key": apiKey },
      });
      await response.arrayBuffer();
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
    } catch {
      failures += 1;
    }
    if (record) {
      latencies.push(performance.now() - start);
      routeLog.push(route);
      pathLog.push(path);
      startsEpochMs.push(startEpoch);
      endsEpochMs.push(performance.timeOrigin + performance.now());
    }
  };
  const run = async (limit, record) => {
    next = 0;
    await Promise.all(Array.from({ length: concurrency }, async () => {
      while (next < limit) await request(record);
    }));
  };
  await run(warmup, false);
  const started = performance.now();
  await run(count, true);
  const durationMs = performance.now() - started;
  assert.equal(latencies.length, count);
  process.stdout.write(`${JSON.stringify({
    count, concurrency, failures, durationMs, latencies,
    routes: routeLog, paths: pathLog, startsEpochMs, endsEpochMs,
  })}\n`);
} else {
  throw new Error("Use measure.");
}
