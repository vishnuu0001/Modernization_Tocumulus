#!/bin/sh
set -eu

mkdir -p /app/data/projects /app/data/logs /app/data/toolchains \
  "${NUGET_PACKAGES:-/app/data/toolchains/nuget}" \
  "${DOTNET_CLI_HOME:-/tmp/dotnet}" \
  "${XDG_RUNTIME_DIR:-/tmp/dotnet-runtime-10001}" \
  /tmp/modernization_jobs

# .NET 8/9/10 NuGet migrations create a named mutex through the user runtime
# directory. The container root is intentionally read-only, so HOME cannot be
# used as a fallback. Keep the mutex/session files in the writable /tmp tmpfs
# with the private permissions required for a per-user XDG runtime directory.
chmod 0700 "${XDG_RUNTIME_DIR:-/tmp/dotnet-runtime-10001}"

if [ "${AUTH_REQUIRED:-true}" = "true" ]; then
  if [ -z "${MODERNIZATION_DATABASE_URL:-}" ]; then
    echo "MODERNIZATION_DATABASE_URL is required when AUTH_REQUIRED=true" >&2
    exit 78
  fi
  if [ -z "${MODERNIZATION_BOOTSTRAP_ADMIN_PASSWORD:-}" ]; then
    echo "MODERNIZATION_BOOTSTRAP_ADMIN_PASSWORD is required for secure initialization" >&2
    exit 78
  fi

  attempts=0
  until python -c "import os,psycopg; c=psycopg.connect(os.environ['MODERNIZATION_DATABASE_URL'],connect_timeout=5); c.close()"; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 30 ]; then
      echo "PostgreSQL did not become ready after 150 seconds" >&2
      exit 69
    fi
    sleep 5
  done
  python -c "from services.access_control import AccessStore; AccessStore().initialize()"
  unset MODERNIZATION_BOOTSTRAP_ADMIN_PASSWORD
fi

exec "$@"
