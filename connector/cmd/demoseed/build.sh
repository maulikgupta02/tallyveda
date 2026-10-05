#!/usr/bin/env bash
# Builds TallyVedaDemoSeed.exe with freshly generated demo books (2024-04-01 to today).
# For test PCs only; never ship it in the connector zip.
set -euo pipefail
cd "$(dirname "$0")"
python3 ../../../dev/demo_seed_data.py data
mkdir -p ../../dist
GOOS=windows GOARCH=amd64 go build -trimpath -o ../../dist/TallyVedaDemoSeed.exe .
echo "built connector/dist/TallyVedaDemoSeed.exe"
