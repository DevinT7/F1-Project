# RaceIQ developer tasks. Windows without make: run the commands under each target directly.

.PHONY: check test typecheck openapi openapi-check gen-types check-frontend e2e up down migrate logs seed

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
