"""Request IDs, JSON logs and /metrics."""

import json
import logging

import psycopg
from prometheus_client import REGISTRY

from tally.observability import JsonFormatter, request_id


def sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_every_response_carries_a_request_id(client):
    generated = client.get("/health").headers["X-Request-ID"]
    echoed = client.get("/health", headers={"X-Request-ID": "trace-42.a_b"}).headers["X-Request-ID"]
    # A caller's ID ends up in logs and headers, so anything but a plain token is replaced.
    replaced = client.get("/health", headers={"X-Request-ID": "x" * 65}).headers["X-Request-ID"]

    assert len(generated) == 32
    assert echoed == "trace-42.a_b"
    assert replaced != "x" * 65 and len(replaced) == 32


def test_an_error_names_its_request_id(client):
    response = client.get("/v1/accounts/999999", headers={"X-Request-ID": "report-me"})

    assert response.status_code == 404
    assert response.json()["request_id"] == "report-me"


def test_each_request_is_logged_once_with_its_outcome(client, caplog):
    with caplog.at_level(logging.INFO, logger="tally.access"):
        client.get("/v1/accounts/999999", headers={"X-Request-ID": "log-me"})
        client.get("/health")  # polled constantly: counted in metrics, kept out of the log

    (record,) = [r for r in caplog.records if r.name == "tally.access"]
    assert (record.route, record.status) == ("/v1/accounts/{account_id}", 404)
    assert record.ms >= 0


def test_log_lines_are_json_with_the_request_id_and_extra_fields():
    record = logging.LogRecord("tally.access", logging.INFO, __file__, 1, "GET %s", ("/x",), None)
    record.status = 200
    token = request_id.set("abc")
    try:
        line = json.loads(JsonFormatter().format(record))
    finally:
        request_id.reset(token)

    assert line["msg"] == "GET /x"
    assert (line["request_id"], line["status"], line["level"]) == ("abc", 200, "INFO")


def test_metrics_report_latency_per_route_not_per_id(client):
    labels = {"method": "GET", "route": "/v1/accounts/{account_id}", "status": "404"}
    before = sample("tally_http_request_duration_seconds_count", **labels)

    client.get("/v1/accounts/999998")
    client.get("/v1/accounts/999999")
    body = client.get("/metrics").text

    assert sample("tally_http_request_duration_seconds_count", **labels) == before + 2
    assert "tally_http_request_duration_seconds_bucket" in body
    assert "999999" not in body


def test_metrics_count_errors_by_code(client):
    before = sample("tally_problems_total", code="ACCOUNT_NOT_FOUND")

    client.get("/v1/accounts/999999")

    assert sample("tally_problems_total", code="ACCOUNT_NOT_FOUND") == before + 1


def test_lock_waits_are_measured(books, wallet):
    alice, bob = wallet("alice", funded=100), wallet("bob")
    before = sample("tally_account_lock_wait_seconds_count")

    books.transfer(alice["id"], bob["id"], 10)

    assert sample("tally_account_lock_wait_seconds_count") == before + 1


def test_retried_transactions_are_counted(db):
    before = sample("tally_db_retries_total", error="DeadlockDetected")
    attempts = []

    def deadlocks_once(conn):
        attempts.append(1)
        if len(attempts) == 1:
            raise psycopg.errors.DeadlockDetected("deadlock detected")

    db.run(deadlocks_once)

    assert sample("tally_db_retries_total", error="DeadlockDetected") == before + 1
