# RaceIQ developer tasks. Windows without make: run the commands under each target directly.

.PHONY: check test typecheck openapi openapi-check gen-types check-frontend e2e up down migrate logs seed \n        monitoring alert-tests restore-drill

# Everything CI runs (except e2e): backend tests + mypy, OpenAPI drift, frontend typecheck/lint/tests.
check: test typecheck openapi-check check-frontend

test:
	cd backend && python -m pytest -q

typecheck:
	cd backend && python -m mypy

# Export the reviewed OpenAPI snapshot (backend/openapi.json).
openapi:
	cd backend && python -m scripts.export_openapi

# CI: fail when the API changed without a reviewed snapshot.
openapi-check:
	cd backend && python -m scripts.export_openapi --check

# Regenerate frontend/src/api/types.ts from the API (openapi-typescript), then type-check it.
gen-types: openapi
	cd frontend && npm run gen-types && npx tsc --noEmit --strict --skipLibCheck src/api/types.ts

# Frontend: type-check, lint, unit + component tests (Vitest + MSW).
check-frontend:
	cd frontend && npm run check

# Frontend end-to-end smoke (Playwright; starts the backend on the fixture repo + a prod build).
e2e:
	cd frontend && npx playwright test

# ---------------------------------------------------------------- Phase 8 local stack (docker-compose.yml)
# Postgres + TimescaleDB, Redis, S3 (SeaweedFS), API, worker and frontend; seeded on first start.
up:
	docker compose up -d --build
	@echo "RaceIQ: http://localhost:$${FRONTEND_PORT:-3000}  (API http://localhost:$${BACKEND_PORT:-8000}/docs)"

down:
	docker compose down

migrate:
	docker compose run --rm backend alembic upgrade head

seed:
	docker compose run --rm seed

logs:
	docker compose logs -f backend worker

# ---------------------------------------------------------------- Phase 9 operations (docs/runbook.md, docs/monitoring.md)
# Prometheus (http://localhost:9090) + Grafana (http://localhost:3001) next to the stack.
monitoring:
	docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d

# Alert rules: promtool check + unit tests.
alert-tests:
	docker run --rm -v "$(CURDIR)/monitoring/prometheus:/p" -w /p --entrypoint promtool prom/prometheus:v3.5.0 test rules alerts_test.yml

# Back up the stack's database, restore it into a scratch database, verify every table.
restore-drill:
	docker compose exec -T -e SOURCE_URL=postgresql://raceiq:$${POSTGRES_PASSWORD:-raceiq-dev}@localhost:5432/raceiq db bash -s < ops/db/restore_drill.sh
