#!/usr/bin/env bash
# Builds a connector release and publishes it through the backend: the zip at
# /download (exe + user guides) and latest.json, which connectors poll to update.
#   VERSION=0.5.0 ./release.sh            background updates allowed (default)
#   VERSION=0.5.0 AUTO=false ./release.sh  only offered on the connector page
set -euo pipefail
cd "$(dirname "$0")"
: "${VERSION:?set VERSION, e.g. VERSION=0.5.0}"
SERVER=${SERVER:-https://tally-connector-1lir.onrender.com}
AUTO=${AUTO:-true}
SERVER="$SERVER" VERSION="$VERSION" ./build.sh
out=../backend/app/static/downloads
zip_name="dist/TallyConnector-$VERSION.zip"
rm -f "$zip_name"
zip -q -j "$zip_name" dist/TallyConnector.exe guides/*.txt
cp "$zip_name" "$out/TallyConnector.zip"
sha=$(shasum -a 256 dist/TallyConnector.exe | cut -d' ' -f1)
size=$(wc -c < dist/TallyConnector.exe | tr -d ' ')
printf '{"version": "%s", "sha256": "%s", "size": %s, "auto": %s}\n' "$VERSION" "$sha" "$size" "$AUTO" > "$out/latest.json"
echo "published $VERSION: $out/TallyConnector.zip and latest.json (auto=$AUTO)"
