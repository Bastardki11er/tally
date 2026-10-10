#!/usr/bin/env python3
"""Load test for the Tally API. Standard library only.

Two scenarios:

  spread   random transfers between many wallets: little lock contention
  hotspot  every customer pays the same merchant: every request needs the
           same row lock, the worst case for a ledger

After each run it checks the things that must never break under load:
  * /v1/reconciliation is clean (every balance equals the sum of its entries)
  * money is conserved: the wallets hold exactly what was deposited

Usage:
  python3 scripts/load_test.py --base-url http://localhost:8000 --requests 5000 --concurrency 50
"""

import argparse
import http.client
import json
import random
import statistics
import sys
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

_local = threading.local()


def call(base, method, path, body=None, idempotent=False):
    headers = {"Content-Type": "application/json"}
    if idempotent:
        # Generated once per logical request: if the connection drops and we retry,
        # the same key guarantees the transfer still happens at most once.
        headers["Idempotency-Key"] = str(uuid.uuid4())
    for attempt in range(2):
        conn = getattr(_local, "conn", None)
        if conn is None:
            conn = _local.conn = http.client.HTTPConnection(base.hostname, base.port or 80, timeout=30)
        try:
            conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            response = conn.getresponse()
            data = response.read()
            return response.status, json.loads(data) if data else None
        except (http.client.HTTPException, ConnectionError):
            # Keep-alive connections the server closed while idle; reconnect and retry once.
            conn.close()
            _local.conn = None
            if attempt == 1:
                raise


def create_account(base, name, kind):
    status, body = call(base, "POST", "/v1/accounts", {"name": name, "type": kind, "currency": "USD"})
    assert status == 201, body
    return body["id"]


def setup(base, wallets, deposit):
    cash = create_account(base, "Load test cash", "asset")
    ids = [create_account(base, f"wallet {i}", "liability") for i in range(wallets)]
    merchant = create_account(base, "Load test merchant", "liability")
    for wallet in ids:
        status, body = call(
            base,
            "POST",
            "/v1/transfers",
            {"debit_account_id": cash, "credit_account_id": wallet, "amount": deposit},
            idempotent=True,
        )
        assert status == 201, body
    return ids, merchant


def run(base, label, requests, concurrency, pick):
    def one(_):
        debit, credit, amount = pick()
        started = time.perf_counter()
        status, body = call(
            base,
            "POST",
            "/v1/transfers",
            {"debit_account_id": debit, "credit_account_id": credit, "amount": amount},
            idempotent=True,
        )
        outcome = "ok" if status == 201 else (body or {}).get("code", f"HTTP {status}")
        return outcome, (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(one, range(requests)))
    elapsed = time.perf_counter() - started
    latencies = sorted(ms for _, ms in results)
    pct = lambda p: latencies[min(len(latencies) - 1, int(p / 100 * len(latencies)))]  # noqa: E731
    print(f"\n[{label}] {requests} transfers, concurrency {concurrency}")
    print(f"  throughput  {requests / elapsed:,.0f} transfers/s ({elapsed:.2f} s)")
    print(f"  latency     p50 {statistics.median(latencies):.1f} ms   p95 {pct(95):.1f} ms   p99 {pct(99):.1f} ms")
    print("  outcomes    " + ", ".join(f"{k}={v}" for k, v in sorted(Counter(o for o, _ in results).items())))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--wallets", type=int, default=200)
    parser.add_argument("--requests", type=int, default=5000)
    parser.add_argument("--concurrency", type=int, default=50)
    args = parser.parse_args()
    base = urlparse(args.base_url)
    deposit = 10_000

    wallets, merchant = setup(base, args.wallets, deposit)
    print(f"{args.wallets} wallets funded with {deposit} each")

    def spread():
        debit, credit = random.sample(wallets, 2)
        return debit, credit, random.randint(1, 500)

    def hotspot():
        return random.choice(wallets), merchant, random.randint(1, 50)

    run(base, "spread", args.requests, args.concurrency, spread)
    run(base, "hotspot", args.requests, args.concurrency, hotspot)

    _, report = call(base, "GET", "/v1/reconciliation")
    held = sum(call(base, "GET", f"/v1/accounts/{a}")[1]["balance"] for a in [*wallets, merchant])
    conserved = held == deposit * len(wallets)
    print(f"\nreconciliation ok: {report['ok']}   money conserved: {conserved} ({held} == {deposit * len(wallets)})")
    sys.exit(0 if report["ok"] and conserved else 1)


if __name__ == "__main__":
    main()
