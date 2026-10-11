#!/usr/bin/env bash
# Decode the shared SAW debug keystore from SAW_DEBUG_KEYSTORE_B64 or a .b64 file.
# Writes a git-ignored keystore and prints only the output path (never the key).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${SAW_DEBUG_KEYSTORE_OUT:-$ROOT/android/.secrets/saw-debug.keystore}"
SRC="${1:-}"

mkdir -p "$(dirname "$OUT")"

python3 - "$SRC" "$OUT" <<'PY'
import base64
import os
import sys

src, out = sys.argv[1], sys.argv[2]
if src:
    data = open(src, "rb").read()
else:
    data = os.environ.get("SAW_DEBUG_KEYSTORE_B64", "").encode("ascii", "ignore")
data = b"".join(data.split())
if not data:
    sys.stderr.write("Set SAW_DEBUG_KEYSTORE_B64 or pass a .b64 file\n")
    sys.exit(1)
open(out, "wb").write(base64.b64decode(data))
print(out)
PY
