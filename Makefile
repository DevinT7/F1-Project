# RaceIQ developer tasks. Windows without make: run the commands under each target directly.

.PHONY: test openapi openapi-check gen-types

test:
	cd backend && python -m pytest -q

# Export the reviewed OpenAPI snapshot (backend/openapi.json).
openapi:
	cd backend && python -m scripts.export_openapi

# CI: fail when the API changed without a reviewed snapshot.
openapi-check:
	cd backend && python -m scripts.export_openapi --check

# Regenerate frontend/src/api/types.ts from the API (openapi-typescript), then type-check it.
gen-types: openapi
	cd frontend && npm run gen-types && npx tsc --noEmit --strict --skipLibCheck src/api/types.ts
