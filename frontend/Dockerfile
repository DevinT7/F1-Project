# RaceIQ frontend (Phase 9): Next.js standalone output on a pinned, minimal Node image.
#
#   docker build --build-arg BACKEND_URL=http://backend:8000 -t raceiq-frontend ./frontend
#
# Next.js bakes rewrites in at build time, so the backend the /api proxy points at is a build
# argument. The runtime stage contains only the standalone server + static assets (no node_modules
# tree, no sources) and runs as the unprivileged `node` user.

ARG NODE_IMAGE=node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402

FROM ${NODE_IMAGE} AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM ${NODE_IMAGE} AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ARG BACKEND_URL=http://backend:8000
ARG NEXT_PUBLIC_SENTRY_DSN=
ARG NEXT_PUBLIC_RELEASE=dev
ENV BACKEND_URL=$BACKEND_URL NEXT_PUBLIC_SENTRY_DSN=$NEXT_PUBLIC_SENTRY_DSN NEXT_PUBLIC_RELEASE=$NEXT_PUBLIC_RELEASE
RUN npx next build --webpack

FROM ${NODE_IMAGE} AS runtime
# the standalone server needs only `node`: npm / corepack (and their dependencies' CVEs) are removed
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
    /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
COPY --from=build --chown=node /app/.next/standalone ./
COPY --from=build --chown=node /app/.next/static ./.next/static
USER node
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=5 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"]
CMD ["node", "server.js"]
