#!/usr/bin/env bash
# Builds one TallyVedaDemo exe per demo company, each carrying that company's
# freshly generated books (to today) and filling only the company of its name.
# For test PCs only; never ship these in the connector zip.
#   TallyVedaDemoSeed.exe          "TallyVeda Demo"            books from 1-Apr-2024 (dev/mock_tally.py)
#   TallyVedaDemo-Healthy.exe      "TallyVeda Demo Healthy"    books from 1-Apr-2023 (dev/demo_companies.py)
#   TallyVedaDemo-Stressed.exe     "TallyVeda Demo Stressed"
#   TallyVedaDemo-Seasonal.exe     "TallyVeda Demo Seasonal"
#   TallyVedaDemo-RedFlags.exe     "TallyVeda Demo Red Flags"
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p ../../dist
build() { # profile, company name, books from, exe name
  python3 ../../../dev/demo_seed_data.py data "$1" >/dev/null
  GOOS=windows GOARCH=amd64 go build -trimpath \
    -ldflags "-X 'main.companyName=$2' -X main.booksFrom=$3" -o "../../dist/$4" .
  echo "built connector/dist/$4 for \"$2\" (books from $3)"
}
build ""         "TallyVeda Demo"           2024-04-01 TallyVedaDemoSeed.exe
build healthy    "TallyVeda Demo Healthy"   2023-04-01 TallyVedaDemo-Healthy.exe
build stressed   "TallyVeda Demo Stressed"  2023-04-01 TallyVedaDemo-Stressed.exe
build seasonal   "TallyVeda Demo Seasonal"  2023-04-01 TallyVedaDemo-Seasonal.exe
build redflags   "TallyVeda Demo Red Flags" 2023-04-01 TallyVedaDemo-RedFlags.exe
