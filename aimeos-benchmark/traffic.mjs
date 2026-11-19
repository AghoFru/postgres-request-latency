import assert from "node:assert/strict";
import { performance } from "node:perf_hooks";

const base = "http://127.0.0.1:8000";
const listing = "/shop/search";
// An optional ROUTE limits the mix to one route, for per-route query tracing.
const routeFilter = process.env.ROUTE ?? "";
assert(["", "product_list", "product_detail"].includes(routeFilter), `Unknown ROUTE ${routeFilter}`);

const discoverProducts = async () => {
  const response = await fetch(`${base}${listing}`);
  assert.equal(response.status, 200, "Aimeos product listing must load");
  const html = await response.text();
  const paths = [...html.matchAll(/href="([^"]+)"/g)]
    .map((match) => new URL(match[1], base).pathname)
    .filter((path) => /^\/shop\/[^/]+\/\d+\/?$/.test(path));
  const products = [...new Set(paths)];
  assert(products.length > 0, "Aimeos demo catalog must contain product links");
  return products;
};

// PRODUCTS skips discovery, so that a traced run contains only the measured requests.
const knownProducts = () => {
  const products = JSON.parse(process.env.PRODUCTS);
  assert(Array.isArray(products) && products.length > 0 && products.length <= 10000);
  for (const path of products) assert(/^\/shop\/[^/]+\/\d+\/?$/.test(path), "Bad product path");
  return products;
};

const main = async () => {
  assert.equal(process.argv[2], "measure", "Use measure.");
  const count = Number(process.argv[3]);
  const concurrency = Number(process.argv[4]);
  const warmup = Number(process.argv[5]);
  assert(Number.isInteger(count) && count > 0 && count <= 100000);
  assert(Number.isInteger(concurrency) && concurrency > 0 && concurrency <= 64);
  assert(Number.isInteger(warmup) && warmup >= 0 && warmup <= 100000);

  const products = process.env.PRODUCTS ? knownProducts() : await discoverProducts();
  let next = 0;
  let failures = 0;
  const latencies = [];
  const routes = [];
  const paths = [];
  const startsEpochMs = [];
  const endsEpochMs = [];
  const request = async (record) => {
    const index = next;
    next += 1;
    const route = routeFilter || (index % 10 < 6 ? "product_list" : "product_detail");
    const path = route === "product_list"
      ? `${listing}?f_sort=${index % 2 ? "name" : "price"}&l_page=${index % 3}`
      : products[(index * 7) % products.length];
    const startEpoch = performance.timeOrigin + performance.now();
    const started = performance.now();
    try {
      const response = await fetch(`${base}${path}`);
      await response.arrayBuffer();
      assert(response.ok, `Aimeos returned HTTP ${response.status}`);
    } catch {
      failures += 1;
    }
    if (record) {
      latencies.push(performance.now() - started);
      routes.push(route);
      paths.push(path);
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
    product_count: products.length, products, routes, paths, startsEpochMs, endsEpochMs,
  })}\n`);
};

await main();
