# syntax=docker/dockerfile:1.10
FROM node:22.23.2-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS dependencies
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22.23.2-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS builder
WORKDIR /app
ARG BUILD_OIDC_ISSUER=https://identity.build.invalid/
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=dependencies /app/node_modules ./node_modules
COPY . .
RUN mkdir -p public \
    && export AUTH_SECRET="$(node -e 'process.stdout.write(require("node:crypto").randomBytes(48).toString("base64url"))')" \
    && export AUTH_URL=https://build.invalid \
    && export API_BASE_URL=https://api.build.invalid \
    && export OIDC_ISSUER="${BUILD_OIDC_ISSUER}" \
    && export OIDC_AUDIENCE=https://api.build.invalid/ \
    && export OIDC_CLIENT_ID=build-only \
    && export OIDC_CLIENT_SECRET="$(node -e 'process.stdout.write(require("node:crypto").randomBytes(32).toString("base64url"))')" \
    && npm run build

FROM node:22.23.2-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
RUN rm -rf /usr/local/lib/node_modules/npm \
    /usr/local/bin/npm /usr/local/bin/npx \
    && groupadd --gid 10001 sourcing \
    && useradd --uid 10001 --gid sourcing --no-create-home --shell /usr/sbin/nologin sourcing
WORKDIR /app
COPY --from=builder --chown=root:root /app/public ./public
COPY --from=builder --chown=root:root /app/.next/standalone ./
COPY --from=builder --chown=root:root /app/.next/static ./.next/static
COPY --from=builder --chown=root:root /app/start-standalone.mjs ./start-standalone.mjs
RUN chmod -R a-w /app
USER 10001:10001
EXPOSE 3000
CMD ["node", "start-standalone.mjs"]
