#!/usr/bin/env bash
# Deploy one environment (dev|prod) on the VPS.
# Usage: deploy.sh <env-name> <git-ref> <image-tag>
# Runs on the VPS as the "deploy" user. Expects:
#   /opt/recruitment/<env>/repo    git checkout of this repository
#   /opt/recruitment/<env>/.env    environment file (see ops/deploy/env.template)
#   /opt/recruitment/<env>/tls/    internal CA + service certs (TLS_ENABLED=true only)
set -euo pipefail

ENV_NAME="${1:?env name required (dev|prod)}"
GIT_REF="${2:?git ref required}"
IMAGE_TAG="${3:?image tag required}"

ROOT="/opt/recruitment/${ENV_NAME}"
REPO_DIR="${ROOT}/repo"
ENV_FILE="${ROOT}/.env"

cd "${REPO_DIR}"
for attempt in 1 2 3; do
  if git fetch --quiet origin "${GIT_REF}"; then
    break
  fi
  [ "${attempt}" -eq 3 ] && { echo "ERROR: git fetch failed for ${GIT_REF}" >&2; exit 1; }
  sleep 3
done
git checkout --quiet --force FETCH_HEAD

# Read env values with sed (never shell-source: URLs contain ? and &).
envget() {
  sed -n "s|^$1=\(.*\)$|\1|p" "${ENV_FILE}" | head -1
}

export API_IMAGE_TAG="${IMAGE_TAG}"

COMPOSE_FILES=(-f compose.yaml -f ops/deploy/compose.ghcr.yml)
if [ "$(envget TLS_ENABLED)" = "true" ]; then
  COMPOSE_FILES+=(-f ops/deploy/compose.tls.yml)
fi

compose() {
  docker compose -p "recruitment-${ENV_NAME}" --env-file "${ENV_FILE}" "${COMPOSE_FILES[@]}" --profile application "$@"
}

echo "==> pulling images (${IMAGE_TAG})"
compose pull --quiet api web

echo "==> starting infrastructure"
compose up -d --no-build --wait postgres redis minio

echo "==> provisioning object store"
MC_IMAGE="minio/mc:latest@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727"
compose_network="recruitment-${ENV_NAME}_default"
docker network inspect "${compose_network}" >/dev/null 2>&1 || docker network create "${compose_network}"
MC_SCHEME="http"
MC_TLS_ARGS=()
if [ "$(envget TLS_ENABLED)" = "true" ]; then
  MC_SCHEME="https"
  MC_TLS_ARGS=(-v "${ROOT}/tls/ca.pem:/etc/ssl/internal-ca.pem:ro" -e SSL_CERT_FILE=/etc/ssl/internal-ca.pem)
fi
docker run --rm -i --network "${compose_network}" "${MC_TLS_ARGS[@]}" \
  -e MC_HOST_local="${MC_SCHEME}://$(envget MINIO_ROOT_USER):$(envget MINIO_ROOT_PASSWORD)@minio:9000" \
  -e WRITER_KEY="$(envget OBJECT_STORE_WRITER_ACCESS_KEY_ID)" \
  -e WRITER_SECRET="$(envget OBJECT_STORE_WRITER_SECRET_ACCESS_KEY)" \
  -e DELETE_KEY="$(envget OBJECT_STORE_DELETE_ACCESS_KEY_ID)" \
  -e DELETE_SECRET="$(envget OBJECT_STORE_DELETE_SECRET_ACCESS_KEY)" \
  -e BUCKET="$(envget OBJECT_STORE_BUCKET)" \
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
API_ROLE_PW=$(sed -n 's|^COMPOSE_DATABASE_URL=postgresql+psycopg://sourcing_api:\([^@]*\)@postgres.*|\1|p' "${ENV_FILE}")
MAINT_ROLE_PW=$(sed -n 's|^COMPOSE_MAINTENANCE_DATABASE_URL=postgresql+psycopg://sourcing_maintenance:\([^@]*\)@postgres.*|\1|p' "${ENV_FILE}")
MIGRATION_ROLE_PW=$(sed -n 's|^COMPOSE_MIGRATION_DATABASE_URL=postgresql+psycopg://\([^:]*\):\([^@]*\)@postgres.*|\2|p' "${ENV_FILE}")
MIGRATION_ROLE_USER=$(sed -n 's|^COMPOSE_MIGRATION_DATABASE_URL=postgresql+psycopg://\([^:]*\):\([^@]*\)@postgres.*|\1|p' "${ENV_FILE}")
test -n "${API_ROLE_PW}" && test -n "${MAINT_ROLE_PW}"
test -n "${MIGRATION_ROLE_PW}" && test "${MIGRATION_ROLE_USER}" = "sourcing_migration"
compose exec -T postgres psql -U "$(envget POSTGRES_USER)" -d "$(envget POSTGRES_DB)" -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_migration') THEN
    CREATE ROLE sourcing_migration LOGIN;
  END IF;
END \$\$;
ALTER ROLE sourcing_api LOGIN PASSWORD '${API_ROLE_PW}';
ALTER ROLE sourcing_maintenance LOGIN PASSWORD '${MAINT_ROLE_PW}';
ALTER ROLE sourcing_migration LOGIN PASSWORD '${MIGRATION_ROLE_PW}';
ALTER ROLE sourcing_migration SUPERUSER;
SQL

echo "==> running migrations"
compose run --rm -T -e MIGRATION_DATABASE_URL="$(envget COMPOSE_MIGRATION_DATABASE_URL)" api alembic upgrade head < /dev/null

echo "==> starting stack"
compose up -d --no-build --remove-orphans

echo "==> waiting for API health"
ok=""
for _ in $(seq 1 30); do
  if curl -fsS --max-time 5 "$(envget API_PUBLIC_URL)/health/ready" >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 5
done
if [ -z "${ok}" ]; then
  echo "ERROR: API health check failed for $(envget API_PUBLIC_URL)" >&2
  compose ps
  exit 1
fi

echo "==> deployed ${ENV_NAME} @ ${IMAGE_TAG}"
echo "    web: $(envget WEB_PUBLIC_URL)"
echo "    api: $(envget API_PUBLIC_URL)"
compose ps
