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
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# --- production runtime: standalone output only, no dev deps ---
FROM base AS production
ENV NODE_ENV=production
RUN useradd --create-home --uid 1000 appuser
COPY --from=builder /app/public ./public
COPY --from=builder --chown=appuser:appuser /app/.next/standalone ./
COPY --from=builder --chown=appuser:appuser /app/.next/static ./.next/static
USER appuser
EXPOSE 3000
CMD ["node", "server.js"]
