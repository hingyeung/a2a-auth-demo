# Excalidraw Diagram Skill

A coding agent skill that generates beautiful and practical Excalidraw diagrams from natural language descriptions. Not just boxes-and-arrows - diagrams that **argue visually**.

Compatible with any coding agent that supports skills. For agents that read from `.claude/skills/` (like [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and [OpenCode](https://github.com/nicepkg/OpenCode)), just drop it in and go.

## What Makes This Different

- **Diagrams that argue, not display.** Every shape/group of shapes mirrors the concept it represents — fan-outs for one-to-many, timelines for sequences, convergence for aggregation. No uniform card grids.
- **Evidence artifacts.** As an example, technical diagrams include real code snippets and actual JSON payloads.
- **Built-in visual validation.** A Playwright-based render pipeline lets the agent see its own output, catch layout issues (overlapping text, misaligned arrows, unbalanced spacing), and fix them in a loop before delivering.
- **Brand-customizable.** All colors and brand styles live in a single file (`references/color-palette.md`). Swap it out and every diagram follows your palette.

## Installation

Clone or download this repo, then copy it into your project's `.claude/skills/` directory:

```bash
git clone https://github.com/coleam00/excalidraw-diagram-skill.git
cp -r excalidraw-diagram-skill .claude/skills/excalidraw-diagram
```

## Setup

The skill includes a render pipeline that lets the agent visually validate its diagrams. It checks for `playwright-cli` first — if that's already on your PATH, it drives that existing browser and there is nothing to install. Otherwise it falls back to Playwright's own bundled Chromium.

**Option A: Ask your coding agent (easiest)**

Just tell your agent: *"Set up the Excalidraw diagram skill renderer by following the instructions in SKILL.md."* It will check for `playwright-cli` and only install Playwright's Chromium if that check comes back empty.

**Option B: Manual**

```bash
command -v playwright-cli   # found? nothing else to do.
```

If that comes back empty:

```bash
cd .claude/skills/excalidraw-diagram/references
uv sync
uv run playwright install chromium
```

## Usage

Ask your coding agent to create a diagram:

> "Create an Excalidraw diagram showing how the AG-UI protocol streams events from an AI agent to a frontend UI"

The skill handles the rest — concept mapping, layout, JSON generation, rendering, and visual validation.

## Customize Colors

Edit `references/color-palette.md` to match your brand. Everything else in the skill is universal design methodology.

## File Structure

```
excalidraw-diagram/
  SKILL.md                          # Design methodology + workflow
  references/
    color-palette.md                # Brand colors (edit this to customize)
    element-templates.md            # JSON templates for each element type
    json-schema.md                  # Excalidraw JSON format reference
    render_excalidraw.py            # Render .excalidraw to PNG (Playwright's own Chromium)
    render_template.html            # Browser template for render_excalidraw.py
    render_with_playwright_cli.sh   # Render .excalidraw to PNG via playwright-cli, if present
    render_template_cli.html        # Browser template for render_with_playwright_cli.sh
    pyproject.toml                  # Python dependencies (playwright)
```
