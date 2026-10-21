#!/usr/bin/env bash
# Records or checks SHA-256 hashes of every model file.
#   scripts/verify_models.sh record   # once, right after downloading (outside the contained environment)
#   scripts/verify_models.sh check    # inside the environment, before every start or upgrade
set -euo pipefail
cd "$(dirname "$0")/.."
MODELS_DIR="${MODELS_DIR:-./models}"
SUMS="$MODELS_DIR/SHA256SUMS"
if command -v sha256sum >/dev/null; then SHA="sha256sum"; else SHA="shasum -a 256"; fi

case "${1:-check}" in
  record)
    (cd "$MODELS_DIR" && find . -type f ! -name SHA256SUMS ! -name README.md -print0 | sort -z | xargs -0 $SHA) > "$SUMS"
    echo "Recorded $(wc -l < "$SUMS" | tr -d ' ') files in $SUMS. Store a copy of this file somewhere separate."
    ;;
  check)
    [ -f "$SUMS" ] || { echo "No $SUMS. Run '$0 record' where the models were downloaded."; exit 1; }
    (cd "$MODELS_DIR" && $SHA -c --quiet SHA256SUMS) && echo "All model files match their recorded hashes."
    ;;
  *) echo "usage: $0 record|check"; exit 2 ;;
esac
