# AI CAD Bridge — Claude Code / Codex inside FreeCAD, Blender and Cura

> Personal project idea (Edimar, 2026-09-03). Not started.

## Goal

Let an AI coding agent (Claude Code, Codex CLI) work **inside** the tools already used for
CAD, 3D and printing, with a do → validate loop that happens in the software itself:

| Software | What the agent should be able to do | How it validates |
|---|---|---|
| FreeCAD | build/edit sketches, bodies, assemblies; run macros | recompute, check geometry/constraints, export STEP/STL and measure |
| Blender | create/edit objects, materials, modifiers; run bpy scripts | render preview, check mesh (manifold, normals), export |
| Ultimaker Cura | load model, set profile, slice | slicing result (time, filament, errors), preview layers |

## Two delivery options

1. **Extension / add-on (preferred)** — a plugin per software that exposes document state and
   actions over a local MCP server (or a thin bridge), which Claude Code / Codex connect to as
   a tool. Distributable, no fork, works with stock releases.
2. **Bundled release** — a custom build of the software with the integration embedded, only if
   the extension route hits limits (sandboxing, API gaps).

## Starting point

- FreeCAD already has ground here: offline VSM extension on FreeCAD Flatpak 1.1.1 with Ollama
  (an earlier offline extension on FreeCAD Flatpak) and the "own model as FreeCAD host" idea.
- MyBenchLab follows the same MCP-first philosophy (AI generates an editable feature list).
- Suggested order: FreeCAD (Python API, MCP server in-process) → Blender (bpy, socket/MCP
  bridge as add-on) → Cura (plugin + CuraEngine CLI).

## Open questions

- One generic bridge protocol vs. per-software tool sets.
- How the agent sees the model: structured tree + measurements first; screenshots/renders as
  a secondary channel.
- Safety: undo/transactions per agent step; never save over the user's file without confirm.
