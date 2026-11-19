// Measure application-to-database TCP round trips with 64-byte ping-pong messages.
// Usage: node rtt_probe.mjs HOST SAMPLES GAP_MS
import assert from "node:assert/strict";
import net from "node:net";
import { performance } from "node:perf_hooks";
import { setTimeout as sleep } from "node:timers/promises";

const [host, samplesArg, gapArg] = process.argv.slice(2);
const samples = Number(samplesArg);
const gapMs = Number(gapArg);
assert(/^[a-z0-9.-]{1,64}$/.test(host ?? ""), "HOST must be a service name");
assert(Number.isInteger(samples) && samples > 0 && samples <= 100000);
assert(Number.isFinite(gapMs) && gapMs >= 0 && gapMs <= 100);
const MESSAGE_BYTES = 64;
const WARMUP_SAMPLES = 50;
const REPLY_TIMEOUT_MS = 2000;

const socket = net.connect(7777, host);
socket.setNoDelay(true);
await new Promise((resolve, reject) => {
  socket.once("connect", resolve);
  socket.once("error", reject);
});
let pending = 0;
let wake = null;
socket.on("data", (chunk) => {
  pending += chunk.length;
  if (pending >= MESSAGE_BYTES && wake) {
    pending -= MESSAGE_BYTES;
    const done = wake;
    wake = null;
    done();
  }
});
const payload = Buffer.alloc(MESSAGE_BYTES, 0x61);
const roundTrip = async () => {
  const reply = new Promise((resolve, reject) => {
    wake = resolve;
    setTimeout(() => reject(new Error("echo reply timed out")), REPLY_TIMEOUT_MS).unref();
  });
  const started = performance.now();
  socket.write(payload);
  await reply;
  return performance.now() - started;
};
const values = [];
for (let index = 0; index < WARMUP_SAMPLES + samples; index += 1) {
  if (gapMs > 0) await sleep(gapMs);
  else await new Promise((resolve) => setImmediate(resolve));
  const value = await roundTrip();
  if (index >= WARMUP_SAMPLES) values.push(Math.round(value * 10000) / 10000);
}
socket.end();
process.stdout.write(`${JSON.stringify({ samples: values, gap_ms: gapMs })}\n`);
process.exit(0);
