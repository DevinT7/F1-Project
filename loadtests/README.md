# Load test (Phase 9)

`locustfile.py` puts load on the endpoint users hit most: a **cached comparison** (`POST /api/v1/comparisons`, 2024 R5 qualifying, VER vs ALO, ~374 KB of JSON, gzip like the browser). It is 80% of requests, with `GET /api/v1/sessions/{key}` as the other 20%.
- **Byte check:** a comparison only counts as a success if the response says `"cached":true`.
- **Targets:** cached p95 < 500 ms and < 1% errors at the target concurrency of 50 users.

## Run it

Against the compose stack with the per-IP rate limit off (every simulated user shares one IP; the limiter itself is tested in `backend/tests/test_auth.py`):

```bash
docker compose -f docker-compose.yml -f docker-compose.e2e.yml up -d
pip install locust==2.46.7
# Linux / macOS
locust -f loadtests/locustfile.py --headless --processes 4 -u 50 -r 10 -t 2m --host http://localhost:8000 \
       --csv loadtests/results/cached_comparison
# Windows (no --processes): one master + 4 workers
locust -f loadtests/locustfile.py --master --headless --expect-workers 4 -u 50 -r 10 -t 2m \
       --host http://localhost:8000 --csv loadtests/results/cached_comparison
locust -f loadtests/locustfile.py --worker     # ×4, in separate shells
```

The load generator must not be the bottleneck. With one generator process the measured p95 was 760 ms while the server sat at 61% CPU: the client, not the API, was saturated. Hence FastHttpUser, the byte check instead of JSON parsing, and 4 generator processes.

## Results (2026-10-15, 4 uvicorn workers in the API container, same machine as the generator)

| Users | Requests | Failures | Throughput | Cached comparison p50 / p95 / p99 | Session status p95 | Verdict |
|---|---|---|---|---|---|---|
| **50** (target) | 26,649 | **0** | 226 req/s | 23 / **89** / 150 ms | 92 ms | **meets both targets** |
| 200 (4× stress) | 28,093 | **0** | 318 req/s | 380 / 650 / 850 ms | 850 ms | no errors; p95 above 500 ms: CPU-bound |

Raw Locust output is in `results/`: `*_stats.csv`, the per-second `*_stats_history.csv`, and the empty `*_failures.csv` / `*_exceptions.csv`.
- **At 200 users the API is CPU-bound.** The generator shares the machine's cores with the stack, and gzip of pre-compressed bodies is already skipped (cache hits are served pre-gzipped).
- **Scaling** is horizontal: more `WEB_CONCURRENCY` or instances. All state is in Redis.
- **In production** `RaceIQSlowComparisons` (`monitoring/prometheus/alerts.yml`) watches the same p95.
