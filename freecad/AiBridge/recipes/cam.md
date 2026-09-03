# CAM recipe (the Path workbench, called "CAM" in FreeCAD 1.x)

Job (model + stock + tools) -> operations -> G-code through a post processor.
**Verified headless** on FreeCAD 1.1.3, all the way to real molds: see
`examples/moeda-zcloud/` in the repo, which machines six mold blocks and posts
twelve GRBL files, each one re-parsed and accepted by `cam_gcode_check`.

Prefer the `cam_*` tools over hand-written `fc_exec`: they already work around
the landmines listed at the end, which cost hours to find. Reach for `fc_exec`
only for something the tools cannot express.

Python module is `Path` (`import Path`); the GUI name is CAM. `import CAM`
also succeeds but it is only a namespace shim.

## The short version, with the bridge tools

```jsonc
// 1. a job on a solid; the stock hugs the model (stock_to_model, the default)
cam_job_create({"models": ["Mold"], "name": "Job", "post": "grbl",
                "post_args": "--translate_drill"})

// 2. tools. The first controller already exists: edit it with "reuse".
cam_tool_add({"reuse": "TC__5mm_Endmill", "label": "T1 endmill 3.175",
              "number": 1, "diameter": 3.175,
              "horiz_feed": 600, "vert_feed": 200, "spindle_speed": 10000})
cam_tool_add({"shape": "v-bit", "label": "T2 v-bit 30", "number": 2,
              "diameter": 3.175, "cutting_edge_angle": 30, "tip_diameter": 0.1,
              "horiz_feed": 400, "vert_feed": 150, "spindle_speed": 10000})

// 3. operations. "properties" sets anything the op has; the tool clears the
//    SetupSheet expression first, which is what makes the value stick.
cam_op_add({"type": "pocket_shape", "name": "op1_cavity", "tool": "T1 endmill 3.175",
            "base": [{"object": "Clone", "subs": ["Face8"]}],
            "properties": {"UseOutline": true, "StartDepth": 0, "FinalDepth": -1.8,
                           "StepDown": 0.6, "StepOver": 40, "FinishDepth": 0.2,
                           "SafeHeight": 5, "ClearanceHeight": 8}})

// 4. read it back, then post one file per tool (GRBL has no tool changer)
cam_inspect({})
cam_postprocess({"split_by": "tool", "output_dir": "~/cnc",
                 "files": ["op1-T1.gcode", "op2_4-T2.gcode"]})
cam_gcode_check({"path": "~/cnc/op1-T1.gcode", "z_floor": -3.1,
                 "safe_height": 5, "bounds": {"x_min": -23.4, "x_max": 23.4},
                 "tools": [1]})
```

## Job from a solid

```python
import Path
from Path.Main import Job as PathJob

job = PathJob.Create("CamJob", [doc.getObject("Body")])
doc.recompute()
```

`Job.Create` builds a full job: a **clone** of the model (`job.Model.Group[0]` —
operations must reference the clone, not your original object), a `Stock` from
its bounding box, a `SetupSheet`, an `Operations` group and one default tool
controller. Useful job properties: `PostProcessor`, `PostProcessorArgs`,
`PostProcessorOutputFile`, `GeometryTolerance`, `Fixtures`, `SplitOutput`,
`OrderOutputBy`, `CycleTime`.

To make the stock exactly the model box, zero
`ExtXneg/ExtXpos/ExtYneg/ExtYpos/ExtZneg/ExtZpos` on `job.Stock`.

## Tool bits and controllers

```python
from Path.Tool.toolbit import ToolBit
import Path.Tool.Controller as Controller

bit = ToolBit.from_shape_id("v-bit").attach_to_doc(doc=doc)   # or "endmill", "ballend", ...
bit.Diameter = 3.175
bit.CuttingEdgeAngle = 30      # V-bits only
bit.TipDiameter = 0.1
tc = Controller.Create("T2 v-bit 30", tool=bit, toolNumber=2)
job.Tools.addObject(tc)
```

Shape ids come from `Path.Tool.shape.TOOL_BIT_SHAPE_NAMES` (endmill, v-bit,
ballend, bullnose, chamfer, drill, reamer, tap, thread-mill, slittingsaw,
probe, ...). `Controller.Create(..., assignTool=False)` never adds the `Tool`
property — always pass `tool=`.

**Feeds are `App::PropertyVelocity`, stored in mm/s.** `tc.HorizFeed = 600`
means 600 mm/s and posts as `F36000`. Always assign a quantity string:

```python
tc.HorizFeed = "600 mm/min"
tc.VertFeed = "200 mm/min"
tc.SpindleSpeed = 10000        # plain rpm, no unit
```

`cam_tool_add` takes plain numbers and treats them as mm/min for you.

## Operations

```python
import Path.Op.PocketShape as OpPocketShape

op = OpPocketShape.Create("op1_cavity", parentJob=job)
op.ToolController = tc
op.Base = [(job.Model.Group[0], ["Face8"])]
doc.recompute()
```

Modules under `Path.Op`, all importable headless in 1.1.3: `Profile`, `Pocket`,
`PocketShape`, `Drilling`, `Engrave`, `Vcarve`, `Adaptive`, `Surface`,
`MillFace`, `Helix`, `Slot`, `Deburr`.

* **`parentJob=job`** matters as soon as the document has more than one job.
* **`Vcarve`** ignores `Base` unless the subelements are faces; the normal way
  in is `op.BaseShapes = [some_object_with_faces]`. It also needs a tool with
  `CuttingEdgeAngle < 180` and `TipDiameter`.
* **`Drilling`** takes explicit points in `op.Locations` (a list of vectors) —
  but it *pre-fills* `op.Base` with every drillable feature it finds in the
  model. Assign `op.Base = []` or you get extra holes (a mold cavity counts as
  one). `PeckEnabled = False` for a plain plunge.
* **`Engrave`** follows edges: `op.Base = [(wires_object, ["Edge1", ...])]`,
  cutting at `FinalDepth`.

## Depths: clear the expression or the value is a no-op

Every operation is created with SetupSheet **expressions** bound to
`StartDepth`, `FinalDepth`, `SafeHeight`, `ClearanceHeight`, `StepDown`,
`RetractHeight`, `PeckDepth` (e.g. `FinalDepth = OpFinalDepth`,
`SafeHeight = OpStockZMax + SetupSheet.SafeHeightOffset`). Assigning the
property looks like it works and is silently reverted by the next recompute —
the symptom is a V-carve that dives to the bottom of the stock.

```python
op.setExpression("FinalDepth", None)     # first
op.FinalDepth = -2.9                     # then
doc.recompute()
```

`cam_op_add` does this for every property it writes and reports which
expressions it cleared.

`Vcarve` reads its **top surface** from `BaseShapes[0].Shape.BoundBox.ZMax`, not
from `StartDepth`: to carve from a pocket floor at Z-1.8, translate the faces to
Z-1.8. `FinalDepth` then clamps how deep the V may go (the actual depth of each
stroke is `half_width / tan(angle/2)`, so thin strokes stay shallower — that is
V-carving, not a bug).

## SVG faces as carving geometry

```python
import importSVG, Part

importSVG.insert("/path/face.svg", doc.Name)   # very chatty on stdout
doc.recompute()
wires = [w for obj in new_objects for w in obj.Shape.Wires]
shape = Part.makeFace(wires, "Part::FaceMakerBullseye")   # nesting -> holes
shape.translate(App.Vector(0, 0, -1.8))
```

importSVG returns a mix of `Face` and `Shell` objects; rebuilding one face set
from all the closed wires with **Bullseye** is what gets the counters inside
letters treated as holes. OpenSCAD's SVG export has Y pointing down and
importSVG flips it back, so the geometry lands in the original orientation.

## Post processing (G-code)

```python
import Path.Post.Processor as PostProcessor

job.PostProcessor = "grbl"
job.PostProcessorArgs = "--translate_drill"
job.SplitOutput = True
job.OrderOutputBy = "Tool"        # or "Operation" / "Fixture"
doc.recompute()
post = PostProcessor.PostProcessorFactory.get_post_processor(job, "grbl")
sections = post.export()          # [(section_name, gcode), ...] — writes nothing
```

`export()` returns the text; **writing the files is on you** (`cam_postprocess`
does it, one per section, and refuses to overwrite). With
`SplitOutput = True` and `OrderOutputBy = "Tool"` you get exactly one section
per tool number, which is what a GRBL machine without a tool changer needs.

GRBL specifics:

* `--translate_drill` expands the `G81` canned cycle into `G0`/`G1`. GRBL has no
  canned cycles; without this the drilling block is dead code.
* the tool change is emitted as a comment, `( M6 T1 )` — GRBL has no tool
  changer. `cam_gcode_check` therefore also looks for the tool number in
  comments and reports `tool_source`.
* `post.tooltipArgs` prints the full argument list of a post processor.
* other posts are the modules in `<resource dir>/Mod/CAM/Path/Post/scripts`.

## Validation

* every operation object `State == ['Up-to-date']`;
* `len(op.Path.Commands) > 0` — an empty path means the operation found no
  geometry (wrong `Base`, tool bigger than the feature, stock mismatch);
* `cam_inspect` compares each `op.Path.BoundBox` with the stock box and gives a
  feed-based time estimate. Beware: `Path.BoundBox` treats a command with no `Z`
  word as Z=0, so trust the G-code parser for the real depth;
* `cam_gcode_check` re-reads the posted file with no FreeCAD involved: XY inside
  the given bounds (arcs bounded by their **sweep**, not their end points), Z
  never below `z_floor`, rapid XY moves only at or above `safe_height`, spindle
  started before the first cutting move, feed words present, expected tool
  numbers, no canned drill cycle.

## Landmines (all measured on this build)

1. **`findToolController` crashes with two tool controllers.**
   `PathScripts.PathUtils.findToolController` leaves `tc` unbound when a job has
   more than one controller and there is no GUI to ask, so `Op.Create` raises
   `UnboundLocalError`. Workaround: set `PathUtils.UserInput` to an object with
   `selectedToolController()`/`chooseToolController()` for the duration of the
   call (`cam_op_add` does).
2. **`Job.Create` lazily imports `Draft`, and `Draft` cannot be imported after
   `FreeCADGui` in a headless session.** Importing `FreeCADGui` under FreeCADCmd
   half-registers the Qt resource system; `draftutils/params.py` then reads
   `:/ui/preferences-*.ui` through `QtCore.QFile` and SIGSEGVs the process. The
   bridge never imports `FreeCADGui` unless `FreeCAD.GuiUp` is true, which is
   what makes `cam_job_create` survive headless.
3. **`App::PropertyPercent` (e.g. `StepOver`) rejects floats.** Pass `40`, not
   `40.0` (`cam_op_add` retries with `int` automatically).
4. Depth expressions and feed units: see the two sections above. Those two cost
   the most time to diagnose, because both fail *silently* — wrong numbers, no
   error.
