#!/usr/bin/env bash
# Point git at the version-controlled hooks in scripts/git-hooks (run once per clone).
# The pre-push hook runs the pytest unit suite and aborts a push if it fails.
set -euo pipefail
cd "$(dirname "$0")/.."
chmod +x scripts/git-hooks/* 2>/dev/null || true
git config core.hooksPath scripts/git-hooks
echo "✓ core.hooksPath → scripts/git-hooks (pre-push now runs pytest)"
