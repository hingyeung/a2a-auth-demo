#!/usr/bin/env bash
# Render an .excalidraw file to PNG using playwright-cli (an already-running,
# already-installed browser) instead of Playwright's own bundled Chromium.
#
# Use this path when `playwright-cli` is on PATH — it needs no browser
# download. render_excalidraw.py (uv run playwright install chromium) is the
# fallback for hosts that don't have playwright-cli.
#
# Usage:
#   render_with_playwright_cli.sh <path-to-file.excalidraw> [output.png]
set -euo pipefail

if ! command -v playwright-cli >/dev/null 2>&1; then
  echo "ERROR: playwright-cli not found on PATH." >&2
  echo "Fall back to: uv run python render_excalidraw.py <file>" >&2
  exit 1
fi

EXCALIDRAW_FILE="${1:?usage: render_with_playwright_cli.sh <file.excalidraw> [output.png]}"
if [ ! -f "$EXCALIDRAW_FILE" ]; then
  echo "ERROR: file not found: $EXCALIDRAW_FILE" >&2
  exit 1
fi
OUTPUT_PNG="${2:-${EXCALIDRAW_FILE%.excalidraw}.png}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/render_template_cli.html"

ABS_EXCALIDRAW="$(cd "$(dirname "$EXCALIDRAW_FILE")" && pwd)/$(basename "$EXCALIDRAW_FILE")"
mkdir -p "$(dirname "$OUTPUT_PNG")"
ABS_OUTPUT="$(cd "$(dirname "$OUTPUT_PNG")" && pwd)/$(basename "$OUTPUT_PNG")"

# Serve from filesystem root so both the template (under .claude/skills/...)
# and the diagram file (anywhere in the repo) are reachable. Local-only —
# playwright-cli's browser is the only client that ever connects.
PORT=8765
while lsof -i ":$PORT" >/dev/null 2>&1; do PORT=$((PORT + 1)); done

python3 -m http.server "$PORT" --directory / >/tmp/excalidraw-render-server.log 2>&1 &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" >/dev/null 2>&1 || true
  playwright-cli close >/dev/null 2>&1 || true
  rm -rf .playwright-cli
}
trap cleanup EXIT
sleep 1

TEMPLATE_URL="http://localhost:$PORT$TEMPLATE"
DATA_URL="http://localhost:$PORT$ABS_EXCALIDRAW"

playwright-cli close >/dev/null 2>&1 || true
playwright-cli open "$TEMPLATE_URL"

playwright-cli run-code "async (page) => {
  await page.waitForFunction(() => window.__moduleReady === true, { timeout: 15000 });
  const data = await page.evaluate(async () => {
    const r = await fetch('$DATA_URL?_=' + Date.now());
    return await r.json();
  });
  const result = await page.evaluate((d) => window.renderDiagram(d), data);
  if (!result || !result.success) throw new Error('render failed: ' + (result && result.error));
  await page.waitForFunction(() => window.__renderComplete === true, { timeout: 15000 });
  const svg = await page.\$('#root svg');
  if (!svg) throw new Error('no svg element found');
  await svg.screenshot({ path: '$ABS_OUTPUT' });
  return await svg.boundingBox();
}"

echo "$ABS_OUTPUT"
