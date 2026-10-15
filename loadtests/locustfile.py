"""Phase 9 load test (Locust): the cached comparison endpoint at target concurrency.

    locust -f loadtests/locustfile.py --headless --processes 4 -u 50 -r 10 -t 2m --host http://localhost:8000 \
           --csv loadtests/results/cached_comparison

Targets (docs/progress.md): p95 < 500 ms for cached comparisons, < 1% errors. Run it against a
stack without the per-IP rate limit (docker-compose.e2e.yml), since every simulated user shares
one IP; the limiter itself is covered by tests/test_auth.py.

Each user requests the same seeded comparison (2024 R5 qualifying, VER vs ALO, transport
resolution 2 m) with gzip, like the frontend. The first request warms the cache.
"""
from locust import between, events, task
from locust.contrib.fasthttp import FastHttpUser

BODY = {"session_key": "2024-05-Q", "reference": {"driver": "VER", "lap": "fastest"},
        "target": {"driver": "ALO", "lap": "fastest"}, "resolution_m": 2.0}


@events.test_start.add_listener
def warm(environment, **_kwargs):
    import httpx
    httpx.post(f"{environment.host}/api/v1/comparisons", json=BODY, timeout=120)


class Analyst(FastHttpUser):
    # FastHttpUser + a byte check (no full JSON parse of ~400 KB): the load generator must not be
    # the bottleneck. Run several generator processes too: --processes 4
    wait_time = between(0.05, 0.3)

    @task(8)
    def cached_comparison(self):
        with self.client.post("/api/v1/comparisons", json=BODY, headers={"Accept-Encoding": "gzip"},
                              name="POST /comparisons (cached)", catch_response=True) as r:
            if r.status_code != 200 or b'"cached":true' not in (r.content or b""):
                r.failure(f"status {r.status_code} or not served from the cache")

    @task(2)
    def session_status(self):
        self.client.get("/api/v1/sessions/2024-05-Q", name="GET /sessions/{key}")
