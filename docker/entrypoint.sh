#!/usr/bin/env bash
set -euo pipefail

read_secret() {
  local variable_name="$1"
  local file_variable_name="${variable_name}_FILE"
  local file_path="${!file_variable_name:-}"
  if [[ -n "${file_path}" ]]; then
    if [[ ! -r "${file_path}" ]]; then
      echo "Secret file for ${variable_name} is not readable: ${file_path}" >&2
      exit 1
    fi
    export "${variable_name}=$(<"${file_path}")"
  fi
}

read_secret AUTH_TOKEN_SECRET
read_secret MODERNIZATION_DATABASE_PASSWORD
read_secret MODERNIZATION_BOOTSTRAP_ADMIN_PASSWORD

if [[ -z "${MODERNIZATION_DATABASE_URL:-}" ]]; then
  : "${MODERNIZATION_DATABASE_PASSWORD:?Set MODERNIZATION_DATABASE_PASSWORD or its _FILE variant}"
  database_host="${MODERNIZATION_DATABASE_HOST:-postgres}"
  database_port="${MODERNIZATION_DATABASE_PORT:-5432}"
  database_name="${MODERNIZATION_DATABASE_NAME:-modernization}"
  database_user="${MODERNIZATION_DATABASE_USER:-modernization}"
  database_sslmode="${MODERNIZATION_DATABASE_SSLMODE:-prefer}"
  encoded_password="$(python -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "${MODERNIZATION_DATABASE_PASSWORD}")"
  export MODERNIZATION_DATABASE_URL="postgresql://${database_user}:${encoded_password}@${database_host}:${database_port}/${database_name}?sslmode=${database_sslmode}"
fi

: "${AUTH_TOKEN_SECRET:?Set AUTH_TOKEN_SECRET or AUTH_TOKEN_SECRET_FILE}"
: "${CORS_ORIGINS:?Set CORS_ORIGINS to the exact HTTPS application origin}"

mkdir -p "${HOME}" "${TMPDIR}" "${MODERNIZATION_PROJECTS_DIR}" \
  "${MODERNIZATION_FS_ROOT}" "${MODERNIZATION_MAVEN_REPOSITORY}" \
  "$(dirname "${MODERNIZATION_CREDENTIAL_PUBLIC_KEY}")"

if [[ ! -s "${MODERNIZATION_CREDENTIAL_PUBLIC_KEY}" ]]; then
  cp /tmp/credential_public_key.pem "${MODERNIZATION_CREDENTIAL_PUBLIC_KEY}"
fi

exec "$@"
