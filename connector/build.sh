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
# Symbols are kept (no -s -w) and the exe carries version info and a manifest:
# stripped, anonymous Go binaries are a common antivirus false positive, and
# Windows Defender quarantined the connector on a real install (2026-10-04).
WIN_LDFLAGS="-H windowsgui -X main.version=$VERSION -X main.defaultServer=$SERVER"
LINUX_LDFLAGS="-s -w -X main.version=$VERSION -X main.defaultServer=$SERVER"
mkdir -p dist
# One 64-bit exe: Windows 11 is 64-bit only, 32-bit Windows 10 is rare, ARM Windows runs it under
# emulation, and 32-bit Go binaries draw more antivirus false positives.
IFS=. read -r V1 V2 V3 <<< "$VERSION"
cat > versioninfo.json <<JSON
{
  "FixedFileInfo": {"FileVersion": {"Major": ${V1:-0}, "Minor": ${V2:-0}, "Patch": ${V3:-0}, "Build": 0},
                    "ProductVersion": {"Major": ${V1:-0}, "Minor": ${V2:-0}, "Patch": ${V3:-0}, "Build": 0},
                    "FileFlagsMask": "3f", "FileOS": "040004", "FileType": "01"},
  "StringFileInfo": {
    "CompanyName": "Tally Connector",
    "FileDescription": "Tally Connector - shares Tally accounts with your lender",
    "FileVersion": "$VERSION",
    "InternalName": "TallyConnector",
    "LegalCopyright": "Tally Connector",
    "OriginalFilename": "TallyConnector.exe",
    "ProductName": "Tally Connector",
    "ProductVersion": "$VERSION"
  },
  "VarFileInfo": {"Translation": {"LangID": "0409", "CharsetID": "04B0"}},
  "ManifestPath": "TallyConnector.exe.manifest"
}
JSON
GOVERSIONINFO=${GOVERSIONINFO:-$(go env GOPATH)/bin/goversioninfo}
"$GOVERSIONINFO" -64 -o resource_windows_amd64.syso versioninfo.json
rm -f versioninfo.json
GOOS=windows GOARCH=amd64 go build -trimpath -ldflags "$WIN_LDFLAGS" -o dist/TallyConnector.exe .
rm -f resource_windows_amd64.syso
GOOS=linux   GOARCH=amd64 go build -trimpath -ldflags "$LINUX_LDFLAGS" -o dist/tallyconnector-linux-amd64 .
echo "built dist/TallyConnector.exe (all Windows PCs), dist/tallyconnector-linux-amd64 (server $SERVER, version $VERSION)"
