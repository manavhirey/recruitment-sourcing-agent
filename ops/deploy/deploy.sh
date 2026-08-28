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

echo "==> provisioning object store"
MC_IMAGE="minio/mc:latest@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727"
compose_network="recruitment-${ENV_NAME}_default"
docker run --rm -i --network "${compose_network}" \
  -e MC_HOST_local="http://${MINIO_ROOT_USER}:${MINIO_ROOT_PASSWORD}@minio:9000" \
  -e WRITER_KEY="${OBJECT_STORE_WRITER_ACCESS_KEY_ID}" \
  -e WRITER_SECRET="${OBJECT_STORE_WRITER_SECRET_ACCESS_KEY}" \
  -e DELETE_KEY="${OBJECT_STORE_DELETE_ACCESS_KEY_ID}" \
  -e DELETE_SECRET="${OBJECT_STORE_DELETE_SECRET_ACCESS_KEY}" \
  -e BUCKET="${OBJECT_STORE_BUCKET}" \
  --entrypoint sh "${MC_IMAGE}" <<'MCEOF'
set -eu
mc mb --ignore-existing "local/${BUCKET}"
cat > /tmp/writer.json <<EOF
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:PutObject","s3:GetObject"],"Resource":["arn:aws:s3:::${BUCKET}/*"]},
 {"Effect":"Allow","Action":["s3:ListBucket","s3:GetBucketLocation"],"Resource":["arn:aws:s3:::${BUCKET}"]}]}
EOF
cat > /tmp/delete.json <<EOF
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:DeleteObject"],"Resource":["arn:aws:s3:::${BUCKET}/*"]},
 {"Effect":"Allow","Action":["s3:ListBucket"],"Resource":["arn:aws:s3:::${BUCKET}"]}]}
EOF
mc admin user add "local" "${WRITER_KEY}" "${WRITER_SECRET}" 2>/dev/null || true
mc admin user add "local" "${DELETE_KEY}" "${DELETE_SECRET}" 2>/dev/null || true
mc admin policy create local writer-pol /tmp/writer.json 2>/dev/null || mc admin policy update local writer-pol /tmp/writer.json
mc admin policy create local delete-pol /tmp/delete.json 2>/dev/null || mc admin policy update local delete-pol /tmp/delete.json
mc admin policy attach local writer-pol --user "${WRITER_KEY}" || true
mc admin policy attach local delete-pol --user "${DELETE_KEY}" || true
MCEOF

echo "==> syncing least-privilege database roles"
API_ROLE_PW=$(sed -n 's|^COMPOSE_DATABASE_URL=postgresql+psycopg://sourcing_api:\([^@]*\)@postgres:.*|\1|p' "${ENV_FILE}")
MAINT_ROLE_PW=$(sed -n 's|^COMPOSE_MAINTENANCE_DATABASE_URL=postgresql+psycopg://sourcing_maintenance:\([^@]*\)@postgres:.*|\1|p' "${ENV_FILE}")
test -n "${API_ROLE_PW}" && test -n "${MAINT_ROLE_PW}"
compose exec -T postgres psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -v ON_ERROR_STOP=1 <<SQL
ALTER ROLE sourcing_api LOGIN PASSWORD '${API_ROLE_PW}';
ALTER ROLE sourcing_maintenance LOGIN PASSWORD '${MAINT_ROLE_PW}';
SQL

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
