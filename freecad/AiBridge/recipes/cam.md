# CAM recipe (the Path workbench, called "CAM" in FreeCAD 1.x)

Job (model + stock + tools) -> operations -> G-code through a post processor.
**Verified headless** on FreeCAD 1.1.3: `Job.Create`, the default tool
controller, a Profile operation producing 37 path commands, and the `grbl` post
processor returning real G-code — all under FreeCADCmd, no GUI.

Python module is `Path` (`import Path`); the GUI name is CAM. `import CAM`
also succeeds but it is only a namespace shim.

## Job from a solid

```python
import Path
from Path.Main import Job as PathJob

doc = doc or App.ActiveDocument
model = doc.getObject("Body")          # any solid: Body, Part::Feature, ...
job = PathJob.Create("CamJob", [model])
doc.recompute()
_result = {
    "job": job.Name,
    "stock": job.Stock.Name,
    "tools": [t.Name for t in job.Tools.Group],   # ['TC__5mm_Endmill'] by default
}
```

`Job.Create` builds a full job: a clone of the model, a `Stock` derived from its
bounding box, a `SetupSheet`, an `Operations` group and one default tool
controller. Useful job properties: `PostProcessor`, `PostProcessorArgs`,
`PostProcessorOutputFile`, `GeometryTolerance`, `Fixtures`, `SplitOutput`,
`CycleTime`.

## Tool controller

The default controller is usable straight away:

```python
tc = job.Tools.Group[0]
tc.HorizFeed = 600          # mm/min
tc.VertFeed = 200
tc.SpindleSpeed = 12000
```

To add another tool bit, copy an existing controller or use
`Path.Tool.Controller` / the tool bit library under
`<resource dir>/Mod/CAM/Tools`. Inspect it with
`fc_api_help({"module": "Path.Tool"})`.

## Operation

```python
from Path.Op import Profile as PathProfile

op = PathProfile.Create("Profile")     # attaches itself to the active/only job
op.ToolController = tc
op.Base = []                           # [] means the whole model outline
op.StepDown = 2
doc.recompute()
_result = {
    "in_job": [o.Name for o in job.Operations.Group],
    "commands": len(op.Path.Commands),   # > 0 means a real toolpath was produced
}
```

Other operation modules under `Path.Op`: `Pocket`, `PocketShape`, `Drilling`,
`Adaptive`, `Engrave`, `Helix`, `Surface`, `Slot`, `Deburr`, `MillFace`. They
all follow the same `Create(name)` pattern — list them with
`fc_api_help({"module": "Path.Op"})`.

`op.Base` takes `(object, ["FaceN", "EdgeN", ...])` tuples when the operation
must be limited to specific geometry.

## Post processing (G-code)

```python
import os
import Path.Post.Processor as PostProcessor

job.PostProcessor = "grbl"
job.PostProcessorOutputFile = os.path.expanduser("~/.cache/freecad-ai-bridge/out.nc")
post = PostProcessor.PostProcessorFactory.get_post_processor(job, "grbl")
sections = post.export()            # list of (name, gcode) tuples
gcode = "".join(text for _, text in sections)
_result = {"lines": gcode.count("\n"), "head": gcode[:200]}
```

Available post processors are the modules in `<resource dir>/Mod/CAM/Path/Post/scripts`
(grbl, linuxcnc, marlin, mach3_mach4, centroid, refactored_*, ...).

## Validation

* every operation object `State == ['Up-to-date']`;
* `len(op.Path.Commands) > 0` — an empty path means the operation found no
  geometry (wrong `Base`, tool bigger than the feature, stock mismatch);
* toolpath inside the stock: compare `op.Path.BoundBox` with
  `job.Stock.Shape.BoundBox`;
* the post processor returns non-empty G-code containing feed (`F`) and spindle
  (`S`) words;
* `job.CycleTime` gives the estimated machining time after a recompute.
