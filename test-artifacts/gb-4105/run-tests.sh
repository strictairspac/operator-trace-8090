#!/usr/bin/env bash
# Row-per-reading migration acceptance suite (gb-4105).
#
# Requirements: PostgreSQL reachable at TEST_DATABASE_URL and a seeded backend
# at ACCEPT_BASE_URL for the HTTP acceptance tests.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="${PYTHONPATH:-}:$HERE/../../backend"
export TEST_DATABASE_URL="${TEST_DATABASE_URL:-postgresql+psycopg://trace@127.0.0.1:17744/operatortrace_test}"
export ACCEPT_BASE_URL="${ACCEPT_BASE_URL:-http://127.0.0.1:18245}"
exec python -m pytest "$HERE/tests" -q "$@"
