#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${ROOT}/.venv/bin/tokenlens-azure"
TRACE_INPUT="${TOKENLENS_TRACE_INPUT:-${ROOT}/foundry-traces}"
REPORT_DIR="${TOKENLENS_REPORT_DIR:-${ROOT}/reports}"

if [[ ! -x "${PYTHON}" ]]; then
  echo "TokenLens CLI not found at ${PYTHON}; install the project without activation." >&2
  exit 2
fi
if [[ ! -e "${TRACE_INPUT}" ]]; then
  echo "Trace input not found: ${TRACE_INPUT}" >&2
  exit 2
fi

echo "Analyzing configured traces: ${TRACE_INPUT}"
# Pass a directory/glob containing rotated event streams so late task-result
# and human-review events can be joined without uploading raw traces.
"${PYTHON}" analyze "${TRACE_INPUT}" --format html --output-dir "${REPORT_DIR}" --quiet
"${PYTHON}" analyze "${TRACE_INPUT}" --format json --output-dir "${REPORT_DIR}" --quiet
latest="$(find "${REPORT_DIR}" -maxdepth 1 -type f -name 'tokenlens-report-*.html' -print | sort | tail -n 1)"
echo "report-path=$(cd -- "$(dirname -- "${latest}")" && pwd)/$(basename -- "${latest}")"
