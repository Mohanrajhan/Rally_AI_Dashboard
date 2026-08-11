#!/usr/bin/env bash
# Launch the GAIG - Rally AI-Adoption Dashboard.
# Run from the project root: ./scripts/run_dashboard.sh
set -euo pipefail
cd "$(dirname "$0")/.."
python -m src.app
