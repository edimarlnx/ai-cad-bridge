# Plan — FreeCAD AI Bridge (Claude Code / Codex inside FreeCAD)

> Status: **approved to build** (2026-09-03). First host of the AI CAD bridge idea
> (see `../IDEA.md`). No compilation: everything is Python.

## Goal

An agent (Claude Code or Codex CLI) works inside a running FreeCAD: reads the
document, runs modeling code, recomputes, measures and checks geometry, takes a
screenshot, exports. The do → validate loop happens in FreeCAD itself, with undo
per agent step and never saving over the user's file without being asked.

## Environment (verified)

- FreeCAD **1.1.3 Flatpak** (`org.freecad.FreeCAD`), embedded Python 3.13,
  `FreeCADCmd` headless available inside the sandbox
  (`flatpak run --command=FreeCADCmd org.freecad.FreeCAD`).
- Add-ons load from `~/.var/app/org.freecad.FreeCAD/data/FreeCAD/v1-1/Mod/<Name>/`
  (versioned dir; see `llm-from-scratch/freecad-ext/install.sh` which already
  resolves it). The sandbox shares the host network (localhost works both ways)
  and does NOT see `/tmp` (scripts under `~/` only).
- No `mcp` Python SDK on host or sandbox → the MCP server is **pure stdlib**.
- `InitGui.py` gotcha: FreeCAD exec's it with separate globals/locals; publish
  imports/constants with `globals().update(...)` (pattern in the VSM workbench).

## Architecture (two processes, stdlib only)

```
Claude Code / Codex ──stdio (MCP JSON-RPC)──▶ bridge/freecad_mcp.py (host)
                                                 │ HTTP JSON-RPC, 127.0.0.1:PORT
                                                 ▼
                                   FreeCAD add-on "AiBridge" (in-process server)
                                   runs each request on the GUI thread, wraps it
                                   in a transaction, returns JSON
```

- **In FreeCAD** (`AiBridge/`): `Init.py` (headless: starts the server also under
  FreeCADCmd when `AIBRIDGE_AUTOSTART=1`), `InitGui.py` (workbench with a
  Start/Stop command + status), `server.py` (`http.server.ThreadingHTTPServer`
  on `127.0.0.1`, port from env `AIBRIDGE_PORT` default `8765`, token from
  `~/.config/freecad-ai-bridge/token` created on first start, required as
  `Authorization: Bearer`), `dispatch.py` (executes on the main thread via
  `QtCore.QTimer.singleShot`/`FreeCADGui` when GUI is up, directly under
  FreeCADCmd), `tools/*.py` (one module per tool group, pure functions taking
  `App`/`Gui` so they are testable under FreeCADCmd).
- **On the host** (`bridge/freecad_mcp.py`): MCP over stdio (protocol
  `2025-06-18`: `initialize`, `notifications/initialized`, `tools/list`,
  `tools/call`, `ping`), forwards each tool call to the add-on and returns the
  JSON as text content; errors become `isError: true`. Discovers port/token from
  the same config file. Zero dependencies (json, sys, urllib).
- **Registration**: `claude mcp add freecad -- python3 <repo>/bridge/freecad_mcp.py`
  and, for Codex, `[mcp_servers.freecad] command = "python3", args = [...]` in
  `~/.codex/config.toml` (script `bridge/register.sh` prints both).

## Tools (v1)

| tool | input | output |
|---|---|---|
| `fc_status` | – | FreeCAD version, GUI or headless, active document, server uptime |
| `fc_doc_list` | – | open documents (name, path, modified) |
| `fc_doc_new` / `fc_doc_open` / `fc_doc_save_as` | name / path | document summary (`save_as` only; never overwrite the current file) |
| `fc_tree` | `doc?`, `depth?` | objects: name, label, type, parent, state (Touched/Invalid), visibility, bbox |
| `fc_object_get` | `name` | properties (typed), shape summary (volume, area, bbox, center of mass, isValid, isClosed, solids/faces/edges counts) |
| `fc_exec` | `code`, `doc?`, `transaction_name?` | run Python in FreeCAD with `App`, `Gui`, `doc` bound; returns `stdout`, `result` (repr of `_result` if set), exceptions; the whole call is one undoable transaction |
| `fc_recompute` | `doc?` | recompute + per-object errors (`obj.State`, `obj.getStatusString()`) |
| `fc_measure` | `{kind: 'bbox'\|'volume'\|'area'\|'distance'\|'center_of_mass', objects[]}` | numbers in mm/mm²/mm³ |
| `fc_check` | `name` | `Part` checks: `isValid`, `isClosed`, `fix()` result, shell/solid count, tolerance |
| `fc_screenshot` | `view?` ('iso'\|'front'\|'top'\|...), `width?`, `height?`, `fit?` | PNG path under `~/.cache/freecad-ai-bridge/` (GUI only; headless returns `unavailable`) |
| `fc_export` | `objects[]`, `format` ('step'\|'stl'\|'obj'), `path` | written file summary |
| `fc_undo` / `fc_redo` | – | undo stack info |
| `fc_sketch_summary` | `name` | geometry + constraints + DoF (`solve()`), reuses ideas from the VSM workbench |

Design rules: every mutating tool opens a transaction named after the tool (or
`transaction_name`) so the user can undo one agent step; `fc_exec` is the escape
hatch and must stay honest (returns tracebacks verbatim); nothing writes outside
`~/.cache/freecad-ai-bridge/` unless a path is given explicitly.

## Validation loop the agent is expected to follow

`fc_exec` (build) → `fc_recompute` (errors?) → `fc_check`/`fc_measure` (numbers
match the spec?) → `fc_screenshot` (looks right?) → `fc_export` when asked.
The MCP server's `instructions` (in `initialize`) say exactly this.

## Tests

- **Headless** (`tests/test_headless.py`, run with `FreeCADCmd`): starts the
  server in-process, exercises every tool over HTTP (exec builds a box, recompute,
  measure volume = 1000 mm³, check valid, export STL, undo restores tree),
  fails loudly. Script must live under `~/` (Flatpak cannot see /tmp).
- **Bridge unit** (`tests/test_mcp_stdio.py`, host `python3`): spawns
  `freecad_mcp.py` with a fake add-on server, checks `initialize` / `tools/list`
  / `tools/call` framing and error mapping.
- **GUI** (Edimar): install, open FreeCAD, Start the bridge from the workbench,
  register in Claude Code, ask it to model a bracket and validate.

## Deliverables / layout

```
ai-cad-bridge/
├── IDEA.md
├── docs/plan-freecad-bridge.md          (this file)
├── freecad/AiBridge/                    (add-on: Init.py, InitGui.py, package.xml, server.py, dispatch.py, tools/)
├── freecad/install.sh                   (same resolution as the VSM installer)
├── bridge/freecad_mcp.py                (MCP stdio server, stdlib)
├── bridge/register.sh
├── tests/test_headless.py, tests/test_mcp_stdio.py, tests/run.sh
└── README.md                            (install, register, first prompt, safety)
```

## Outcome that matters (user, 2026-09-03)

**Build anything in FreeCAD and get it OUT: to 3D printing and, above all, to
CNC.** Success is measured at the outputs, not at the model:

- **3D printing**: STL / 3MF / STEP export of a body that is a single valid,
  closed solid; report mesh stats (triangles, watertight, bbox, volume) and
  flag thin walls when asked (measure via offset/section). The Cura host comes
  later; for now the bridge hands a printable file to the user.
- **CNC (primary)**: a CAM Job on the model with stock, tools and operations,
  post-processed to **G-code for the user's machine**. The bridge must be able
  to: create the job, add tool bits/controllers with real feeds/speeds, add
  operations (profile, pocket, drilling, adaptive, engrave, 3D surface), run
  the CAM simulation/inspection (tool paths inside stock, no rapid moves
  through material, estimated time), and post-process with the right post
  (grbl / linuxcnc / mach3 / … — **open question: which controller Edimar
  uses**; default `grbl` until answered). Validation reads the G-code back
  (moves bounded by stock, spindle on before cuts, safe heights, tool numbers).

Phase 2 order therefore becomes: **CAM first** (right after Part Design, since
CAM needs a body), then TechDraw (shop drawings for the same parts), then
Assembly.

## Workbench coverage (user requirement, 2026-09-03)

**Everything FreeCAD can do must be reachable, with first-class support for
Part Design, Assembly, TechDraw and CAM.** Two layers make that true:

1. **Universal path**: `fc_exec` runs any FreeCAD Python (all workbenches are
   Python-scriptable), so nothing is ever impossible. What makes it *usable* is
   knowledge + validation, which is where the next layers come in.
2. **Knowledge tools** (v1.5, ships with v1):
   - `fc_api_help({module?, object?, query?})`: introspection of the live
     FreeCAD (`dir()`, docstrings, property lists, `TypeId`s) so the agent does
     not guess API names; e.g. `PartDesign::Pad` properties, `Assembly` joint
     types, `TechDraw::DrawViewPart` props, `Path`/`CAM` op classes.
   - `fc_recipes({workbench})`: curated, tested snippets per workbench (see
     below) served as MCP resources too (`freecad://recipes/<workbench>`).
   - `fc_workbenches`: which workbenches/modules are importable in this
     FreeCAD (PartDesign, Sketcher, Assembly, TechDraw, Path/CAM, Mesh, Draft,
     Spreadsheet, FEM) with versions, so the agent knows what it can use.
3. **Workbench tool groups** (phase 2, one commit each, each with headless
   tests that build a real thing and validate it):
   - **Part Design** — `pd_body_create`, `pd_sketch_create` (plane/face,
     geometry + constraints, returns DoF), `pd_pad/pocket/hole/fillet/chamfer/
     pattern`, `pd_feature_state` (per-feature errors). Validation: body is one
     valid solid, no invalid features, volume/bbox as expected.
   - **Assembly** (built-in Assembly workbench, FreeCAD 1.x) — `asm_create`,
     `asm_insert` (link to a part/body/file), `asm_joint` (fixed, revolute,
     cylindrical, slider, ball, distance…), `asm_solve` (solver result, DoF,
     conflicts/redundancies), `asm_bom`. Validation: solver converges, parts
     placed where expected (measure distance), no overlaps (common volume).
   - **TechDraw** — `td_page_create` (template), `td_view_add` (part view /
     projection group / section / detail), `td_dimension_add`, `td_export`
     (PDF/SVG/DXF). Validation: page renders (no `TechDraw` warnings), views
     have geometry, export file exists and is non-empty.
   - **CAM** (Path workbench = "CAM" in 1.x) — `cam_job_create` (model +
     stock), `cam_tool_add` (tool bit + controller), `cam_op_add` (profile,
     pocket, drilling, adaptive, engrave…), `cam_simulate` (path bounds,
     time estimate, collisions with stock/clamps where the API allows),
     `cam_postprocess` (G-code via a post processor: grbl, linuxcnc, …).
     Validation: G-code produced, tool paths inside stock bounds, `Path`
     objects without errors, feed/speed present.
   Each group also gets a recipe file the agent can read before acting.

Rule for all of them: a tool is only "done" when its headless test builds the
object and the validation numbers pass under `FreeCADCmd` on this machine.

## Later

Blender (bpy add-on with the same JSON-RPC contract), Cura (plugin + CuraEngine
CLI), bundled releases only if the add-on route hits limits.
