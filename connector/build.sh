#!/usr/bin/env bash
# Build the Windows connector with the bank's server URL baked in.
#   SERVER=https://tally.yourbank.in VERSION=1.0.0 ./build.sh
set -euo pipefail
cd "$(dirname "$0")"
SERVER=${SERVER:-http://localhost:8000}
VERSION=${VERSION:-0.1.0}
# -H windowsgui: no console window (the UI is in the browser; output goes to %AppData%\TallyConnector\connector.log).
LDFLAGS="-s -w -H windowsgui -X main.version=$VERSION -X main.defaultServer=$SERVER"
mkdir -p dist
GOOS=windows GOARCH=amd64 go build -trimpath -ldflags "$LDFLAGS" -o dist/TallyConnector.exe .
GOOS=windows GOARCH=386   go build -trimpath -ldflags "$LDFLAGS" -o dist/TallyConnector-32bit.exe .
echo "built dist/TallyConnector.exe (server $SERVER, version $VERSION)"
