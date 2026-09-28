FROM node:22-slim AS base
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1

# --- development image: full deps, source mounted as a volume, hot reload ---
FROM base AS development
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
EXPOSE 3000
CMD ["npm", "run", "dev"]

# --- dependency install for production build ---
FROM base AS deps
COPY package.json package-lock.json* ./
RUN npm install

# --- production build ---
FROM base AS builder
# NEXT_PUBLIC_* values are inlined into the JavaScript bundle by
# `next build`; setting them only at container runtime has no effect. Without
# this ARG the bundle silently fell back to http://localhost:8000/api/v1 (see
# src/lib/constants.ts), so every browser called its own machine. The build
# refuses to proceed without a value rather than ship that fallback again.
ARG NEXT_PUBLIC_API_BASE_URL
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
RUN test -n "$NEXT_PUBLIC_API_BASE_URL" || { \
      echo "NEXT_PUBLIC_API_BASE_URL build arg is required for the production frontend image" >&2; \
      exit 1; }
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# --- production runtime: standalone output only, no dev deps ---
FROM base AS production
ENV NODE_ENV=production
# node:22-slim already ships a non-root `node` user/group at uid/gid 1000
# (Debian's next free uid on this image) — reusing it instead of creating
# a second one, since `useradd --uid 1000` collides and fails the build.
COPY --from=builder /app/public ./public
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
USER node
EXPOSE 3000
CMD ["node", "server.js"]
