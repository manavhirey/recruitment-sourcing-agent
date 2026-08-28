#!/usr/bin/env bash
# Deploy one environment (dev|prod) on the VPS.
# Usage: deploy.sh <env-name> <git-ref> <image-tag>
# Runs on the VPS as the "deploy" user. Expects:
#   /opt/recruitment/<env>/repo    git checkout of this repository
#   /opt/recruitment/<env>/.env    environment file (see ops/deploy/env.template)
set -euo pipefail

ENV_NAME="${1:?env name required (dev|prod)}"
GIT_REF="${2:?git ref required}"
IMAGE_TAG="${3:?image tag required}"

ROOT="/opt/recruitment/${ENV_NAME}"
REPO_DIR="${ROOT}/repo"
ENV_FILE="${ROOT}/.env"

cd "${REPO_DIR}"
git fetch --quiet origin "${GIT_REF}"
git checkout --quiet --force FETCH_HEAD

# Shell-source the env file so compose interpolation and migration URLs are set.
set -a
# shellcheck disable=SC1090
. "${ENV_FILE}"
set +a
export API_IMAGE_TAG="${IMAGE_TAG}"

compose() {
  docker compose -p "recruitment-${ENV_NAME}" --env-file "${ENV_FILE}" -f compose.yaml -f ops/deploy/compose.ghcr.yml --profile application "$@"
}

echo "==> pulling images (${IMAGE_TAG})"
compose pull --quiet api web

echo "==> running migrations"
compose run --rm -T -e MIGRATION_DATABASE_URL="${COMPOSE_MIGRATION_DATABASE_URL:?COMPOSE_MIGRATION_DATABASE_URL required}" api alembic upgrade head < /dev/null

echo "==> starting stack"
compose up -d --no-build --remove-orphans

echo "==> waiting for API health"
ok=""
for _ in $(seq 1 30); do
  if curl -fsS --max-time 5 "${API_PUBLIC_URL:?API_PUBLIC_URL required}/health/ready" >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 5
done
if [ -z "${ok}" ]; then
  echo "ERROR: API health check failed for ${API_PUBLIC_URL}" >&2
  compose ps
  exit 1
fi

echo "==> deployed ${ENV_NAME} @ ${IMAGE_TAG}"
echo "    web: ${WEB_PUBLIC_URL:-unknown}"
echo "    api: ${API_PUBLIC_URL}"
compose ps
