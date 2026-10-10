// Run: node --test 'tests/js/*.test.js'   (Node 22+, no dependencies)
import assert from "node:assert/strict";
import { test } from "node:test";

import { ApiError, createActions, isRetryable } from "../../src/tally/static/actions.js";

/** A fake API that records each call and answers with whatever the test queues. */
function fakeApi() {
  const calls = [];
  const answers = [];
  const request = (method, path, body, key) => {
    calls.push({ method, path, body, key });
    return new Promise((resolve, reject) => answers.push({ resolve, reject }));
  };
  return { request, calls, answer: () => answers.shift() };
}

let counter = 0;
const newKey = () => `key-${++counter}`;

test("a double click sends one request", async () => {
  const api = fakeApi();
  const run = createActions(api.request, newKey);

  const first = run("transfer", "POST", "/v1/transfers", { amount: 1 });
  const second = run("transfer", "POST", "/v1/transfers", { amount: 1 });
  api.answer().resolve({ id: "t1" });

  assert.deepEqual(await first, { id: "t1" });
  assert.equal(await second, undefined);
  assert.equal(api.calls.length, 1);
});

test("a retry after a lost answer reuses the key, so the server can replay", async () => {
  const api = fakeApi();
  const run = createActions(api.request, newKey);

  const attempt = run("transfer", "POST", "/v1/transfers", {});
  api.answer().reject(new ApiError(0, { code: "NETWORK_ERROR" }));
  await assert.rejects(attempt);
  const retry = run("transfer", "POST", "/v1/transfers", {});
  api.answer().resolve({ id: "t1" });
  await retry;

  assert.equal(api.calls[0].key, api.calls[1].key);
});

test("503s and a key still in use keep the key too", async () => {
  for (const error of [new ApiError(503, { code: "LOCK_TIMEOUT" }), new ApiError(409, { code: "IDEMPOTENCY_KEY_IN_USE" })]) {
    const api = fakeApi();
    const run = createActions(api.request, newKey);
    const attempt = run("hold", "POST", "/v1/holds", {});
    api.answer().reject(error);
    await assert.rejects(attempt);
    const retry = run("hold", "POST", "/v1/holds", {});
    api.answer().resolve({});
    await retry;
    assert.equal(api.calls[0].key, api.calls[1].key, error.code);
  }
});

test("a definite answer retires the key: the next action is a new one", async () => {
  const api = fakeApi();
  const run = createActions(api.request, newKey);

  const ok = run("transfer", "POST", "/v1/transfers", {});
  api.answer().resolve({ id: "t1" });
  await ok;
  const refused = run("transfer", "POST", "/v1/transfers", {});
  api.answer().reject(new ApiError(409, { code: "INSUFFICIENT_FUNDS" }));
  await assert.rejects(refused, /HTTP 409/);
  const next = run("transfer", "POST", "/v1/transfers", {});
  api.answer().resolve({ id: "t2" });
  await next;

  const keys = api.calls.map((call) => call.key);
  assert.equal(new Set(keys).size, 3);
});

test("different actions run independently, with their own keys", async () => {
  const api = fakeApi();
  const run = createActions(api.request, newKey);

  const a = run("reverse:1", "POST", "/v1/transactions/1/reversal", {});
  const b = run("reverse:2", "POST", "/v1/transactions/2/reversal", {});
  api.answer().resolve({});
  api.answer().resolve({});
  await Promise.all([a, b]);

  assert.equal(api.calls.length, 2);
  assert.notEqual(api.calls[0].key, api.calls[1].key);
});

test("which failures leave the outcome open", () => {
  assert.equal(isRetryable(new ApiError(0, null)), true);
  assert.equal(isRetryable(new ApiError(500, null)), true);
  assert.equal(isRetryable(new ApiError(503, { code: "SERVER_BUSY" })), true);
  assert.equal(isRetryable(new ApiError(409, { code: "IDEMPOTENCY_KEY_IN_USE" })), true);
  assert.equal(isRetryable(new ApiError(409, { code: "INSUFFICIENT_FUNDS" })), false);
  assert.equal(isRetryable(new ApiError(422, { code: "IDEMPOTENCY_KEY_REUSED" })), false);
});
