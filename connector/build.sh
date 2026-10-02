#!/usr/bin/env bash
# Build the connector with the bank's server URL baked in: the Windows exe
# applicants run, plus a headless Linux binary for cloud installs that only
# ever run -monitor-run from cron/systemd (see README.md "Linux / cloud installs").
#   SERVER=https://tally.yourbank.in VERSION=1.0.0 ./build.sh
set -euo pipefail
cd "$(dirname "$0")"
SERVER=${SERVER:-http://localhost:8000}
VERSION=${VERSION:-0.1.0}
# -H windowsgui: no console window (the UI is in the browser; output goes to %AppData%\TallyConnector\connector.log).
WIN_LDFLAGS="-s -w -H windowsgui -X main.version=$VERSION -X main.defaultServer=$SERVER"
LINUX_LDFLAGS="-s -w -X main.version=$VERSION -X main.defaultServer=$SERVER"
mkdir -p dist
# One 32-bit exe for every applicant: it runs natively on 32- and 64-bit Windows and under
# emulation on ARM Windows, and the connector only talks HTTP to Tally, so nothing needs 64-bit.
rm -f dist/TallyConnector-32bit.exe
GOOS=windows GOARCH=386   go build -trimpath -ldflags "$WIN_LDFLAGS" -o dist/TallyConnector.exe .
GOOS=linux   GOARCH=amd64 go build -trimpath -ldflags "$LINUX_LDFLAGS" -o dist/tallyconnector-linux-amd64 .
echo "built dist/TallyConnector.exe (all Windows PCs), dist/tallyconnector-linux-amd64 (server $SERVER, version $VERSION)"
