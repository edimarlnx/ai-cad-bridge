# FreeCAD AI Bridge

Let Claude Code or Codex CLI work **inside a running FreeCAD**: read the
document, run modeling code, recompute, measure and check the geometry, take a
screenshot, export. The do → validate loop happens in FreeCAD itself, one undo
per agent step, and the user's file is never overwritten behind their back.

First host of the [AI CAD bridge idea](IDEA.md); the plan lives in
[docs/plan-freecad-bridge.md](docs/plan-freecad-bridge.md). Blender and Cura
come later, with the same JSON-RPC contract.

Python only, standard library only — no pip packages on the host and none
inside FreeCAD.

## How it works

```
Claude Code / Codex ──stdio (MCP JSON-RPC)──▶ bridge/freecad_mcp.py   (host)
                                                 │ HTTP JSON-RPC, 127.0.0.1
                                                 ▼
                                   FreeCAD add-on "AiBridge" (in-process)
                                   runs each request on FreeCAD's main thread,
                                   inside a transaction, and returns JSON
```

Two processes, both stdlib. The add-on generates a bearer token on first start
and writes it with the port to `~/.config/freecad-ai-bridge/config.json` (mode
0600); the MCP server reads the same file, so there is no secret to copy.

## Install

```bash
bash freecad/install.sh          # copies the add-on into the FreeCAD Mod dir
bash bridge/register.sh          # prints the registration commands (edits nothing)
```

`install.sh` resolves the newest versioned Flatpak data dir
(`~/.var/app/org.freecad.FreeCAD/data/FreeCAD/v1-1/Mod/AiBridge` today) and
records the absolute path of `bridge/freecad_mcp.py` inside the add-on so the
workbench can hand you the right registration line.

## Start it in FreeCAD

Restart FreeCAD, choose the **AI Bridge** workbench, press **Start bridge**.
The Report view prints the port and the registration command; **Stop bridge**
shuts it down, **Copy registration command** puts it on the clipboard.

From a macro or a script:

```python
import AiBridge
AiBridge.start()      # honours AIBRIDGE_PORT, default 8765
AiBridge.status()
AiBridge.stop()
```

Headless daemon (the bridge needs the main thread, because FreeCAD's document
API is not thread safe):

```python
# serve.py
import AiBridge
AiBridge.serve()
```
```bash
flatpak run --command=FreeCADCmd org.freecad.FreeCAD serve.py
```

`AIBRIDGE_AUTOSTART=1` starts the server during FreeCAD's startup.

## Register the agent

Claude Code:

```bash
claude mcp add freecad -- python3 /abs/path/ai-cad-bridge/bridge/freecad_mcp.py
```

Codex CLI — add to `~/.codex/config.toml`:

```toml
[mcp_servers.freecad]
command = "python3"
args = ["/abs/path/ai-cad-bridge/bridge/freecad_mcp.py"]
```

If FreeCAD is closed the MCP server still starts: `tools/list` returns the
catalogue and calls answer with a message telling you to start the bridge.

## First prompt

> modele um suporte em L 100x60x5 mm com dois furos de 6 mm e me diga o volume

A good run looks like: `fc_status` → `fc_workbenches` → `fc_recipes partdesign`
→ `fc_exec` (body, constrained sketch, pad, holes) → `fc_recompute` →
`fc_measure` (volume, bbox) → `fc_check` → `fc_screenshot` → the measured
numbers reported back.

## Tools

| tool | what it does |
|---|---|
| `fc_status` | version, GUI or headless, active document, server uptime |
| `fc_doc_list` / `fc_doc_new` / `fc_doc_open` / `fc_doc_save_as` | document lifecycle |
| `fc_tree` | object tree: type, state, visibility, bounding box |
| `fc_object_get` | typed properties + shape summary of one object |
| `fc_exec` | run Python in FreeCAD as one undoable transaction |
| `fc_recompute` | recompute and list objects in error |
| `fc_measure` | bbox, volume (mm³), area (mm²), distance, centre of mass |
| `fc_check` | isValid, isClosed, counts, tolerance, non-destructive `fix()` report |
| `fc_sketch_summary` | geometry, constraints, DoF, conflicts, redundancies |
| `fc_screenshot` | PNG of the 3D view (GUI only) |
| `fc_export` | STEP, STL, OBJ, IGES, BREP |
| `fc_undo` / `fc_redo` | walk the transaction stack |
| `fc_workbenches` | which workbenches this build can use |
| `fc_api_help` | live introspection of modules, attributes and TypeIds |
| `fc_recipes` | verified snippets per workbench |

Everything FreeCAD does is reachable: `fc_exec` runs any FreeCAD Python, and the
knowledge tools plus the recipes under `freecad/AiBridge/recipes/` (PartDesign,
Assembly, TechDraw, CAM, Part — all verified headless on FreeCAD 1.1.3) mean the
agent does not have to guess the API. The recipes are also MCP resources
(`freecad://recipes/<workbench>`).

## Safety

* **Transactions.** Every mutating tool runs inside
  `openTransaction`/`commitTransaction`, so one agent step is one Ctrl+Z. A
  handler that raises — or an `fc_exec` whose code raises — aborts the
  transaction, leaving no half-built debris.
* **No silent overwrites.** `fc_doc_save_as` and `fc_export` refuse to replace
  an existing file unless you pass `overwrite: true`, and nothing ever saves
  over the user's original document implicitly. Generated files default to
  `~/.cache/freecad-ai-bridge/`.
* **Localhost only.** The server binds `127.0.0.1` and rejects any request
  without the exact bearer token from the 0600 config file. Nothing is exposed
  to the network and nothing leaves the machine.
* **Honest failures.** `fc_exec` returns the traceback verbatim,
  `fc_screenshot` says `available: false` headless instead of inventing an
  image, and shapes are summarized rather than dumped.

## Tests

```bash
bash tests/run.sh
```

`tests/test_headless.py` runs inside the real Flatpak FreeCAD (FreeCADCmd, no
GUI) and drives every tool over HTTP: builds a box with `fc_exec`, recomputes,
reads the tree, measures 1000 mm³, checks validity, summarizes a sketch,
exports STL, undoes and redoes, probes `PartDesign::Pad`, reads a recipe and
saves the document. `tests/test_mcp_stdio.py` drives the MCP server over pipes
against a fake add-on, including the offline fallback. Test scripts must live
under `$HOME`: the Flatpak sandbox cannot see `/tmp`.

## Known limitations on FreeCAD 1.1.3

* Page-level SVG/PDF export in TechDraw lives in `TechDrawGui`, so headless
  sessions only get DXF (`TechDraw.writeDXFPage`) and per-view SVG/DXF strings.
* Importing `Draft` from inside a bridge request aborts a *headless* FreeCAD
  (it pulls Qt). `fc_workbenches` therefore reports it without importing it, and
  `fc_api_help` refuses to import it headless unless you pass `force: true`.
* Screenshots need a GUI session.
