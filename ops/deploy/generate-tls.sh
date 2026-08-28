#!/usr/bin/env bash
# Generate an internal CA + per-service TLS certs for one environment.
# Usage: generate-tls.sh <tls-dir>   e.g. /opt/recruitment/prod/tls
# Run on the VPS as deploy; requires sudo for cert ownership fixes.
set -euo pipefail

TLS_DIR="${1:?tls directory required}"
mkdir -p "${TLS_DIR}/postgres" "${TLS_DIR}/redis" "${TLS_DIR}/minio"

openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout "${TLS_DIR}/ca.key" -out "${TLS_DIR}/ca.pem" \
  -subj "/CN=kunigami internal CA" >/dev/null 2>&1

for svc in postgres redis minio; do
  openssl req -newkey rsa:2048 -nodes -days 825 \
    -keyout "${TLS_DIR}/${svc}/server.key" -out "${TLS_DIR}/${svc}/server.csr" \
    -subj "/CN=${svc}" >/dev/null 2>&1
  openssl x509 -req -in "${TLS_DIR}/${svc}/server.csr" \
    -CA "${TLS_DIR}/ca.pem" -CAkey "${TLS_DIR}/ca.key" -CAcreateserial \
    -out "${TLS_DIR}/${svc}/server.crt" -days 825 \
    -extfile <(printf "subjectAltName=DNS:%s" "${svc}") >/dev/null 2>&1
  rm -f "${TLS_DIR}/${svc}/server.csr"
  chmod 600 "${TLS_DIR}/${svc}/server.key"
  chmod 644 "${TLS_DIR}/${svc}/server.crt"
done

# postgres (uid 70) and redis (uid 999) must own their keys to read them.
sudo chown 70:70 "${TLS_DIR}/postgres/server.key" "${TLS_DIR}/postgres/server.crt"
sudo chown 999:999 "${TLS_DIR}/redis/server.key" "${TLS_DIR}/redis/server.crt"

chmod 600 "${TLS_DIR}/ca.key"
echo "TLS material generated in ${TLS_DIR}"
