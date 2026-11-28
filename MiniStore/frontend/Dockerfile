# syntax=docker/dockerfile:1
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-alpine AS build
WORKDIR /app
ARG NEXT_PUBLIC_COMMERCE_API_BASE_URL=http://localhost:8100
ARG NEXT_PUBLIC_FINSHOP_VNPAY_SANDBOX=false
ENV NEXT_PUBLIC_COMMERCE_API_BASE_URL=${NEXT_PUBLIC_COMMERCE_API_BASE_URL} \
    NEXT_PUBLIC_FINSHOP_VNPAY_SANDBOX=${NEXT_PUBLIC_FINSHOP_VNPAY_SANDBOX} \
    NEXT_STANDALONE=1 \
    NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3100 \
    HOSTNAME=0.0.0.0
RUN addgroup -S app && adduser -S app -G app
COPY --from=build --chown=app:app /app/.next/standalone ./
COPY --from=build --chown=app:app /app/.next/static ./.next/static
COPY --from=build --chown=app:app /app/public ./public
USER app
EXPOSE 3100
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
  CMD wget -qO- http://127.0.0.1:3100/ > /dev/null || exit 1
CMD ["node", "server.js"]
