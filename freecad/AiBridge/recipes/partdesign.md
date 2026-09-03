# PartDesign recipe

Feature-based modeling: Body -> constrained Sketch -> Pad/Pocket/Hole -> dress-up.
**Verified headless** on FreeCAD 1.1.3 (FreeCADCmd, no GUI): body, sketch with
constraints (DoF 0), Pad, Hole and Fillet all recompute to `Up-to-date`.

Run these through `fc_exec`, then `fc_recompute`, `fc_check` and `fc_measure`.

## Body + fully constrained rectangular sketch + Pad

```python
import Part, Sketcher
doc = doc or App.newDocument("Bracket")

body = doc.addObject("PartDesign::Body", "Body")
sketch = doc.addObject("Sketcher::SketchObject", "Sketch")
body.addObject(sketch)                      # ALWAYS put the sketch in the body
# Attach to a datum plane of the body's origin: XY_Plane / XZ_Plane / YZ_Plane
sketch.AttachmentSupport = [(doc.getObject("XY_Plane"), "")]
sketch.MapMode = "FlatFace"

V = App.Vector
pts = [(0, 0), (100, 0), (100, 60), (0, 60)]
for i in range(4):
    a, b = pts[i], pts[(i + 1) % 4]
    sketch.addGeometry(Part.LineSegment(V(a[0], a[1], 0), V(b[0], b[1], 0)), False)
for i in range(4):                          # close the contour
    sketch.addConstraint(Sketcher.Constraint("Coincident", i, 2, (i + 1) % 4, 1))
sketch.addConstraint(Sketcher.Constraint("Horizontal", 0))
sketch.addConstraint(Sketcher.Constraint("Horizontal", 2))
sketch.addConstraint(Sketcher.Constraint("Vertical", 1))
sketch.addConstraint(Sketcher.Constraint("Vertical", 3))
sketch.addConstraint(Sketcher.Constraint("DistanceX", 0, 1, 0, 2, 100))   # width
sketch.addConstraint(Sketcher.Constraint("DistanceY", 1, 1, 1, 2, 60))    # height
sketch.addConstraint(Sketcher.Constraint("Coincident", 0, 1, -1, 1))      # pin to origin
doc.recompute()
assert sketch.DoF == 0 and sketch.FullyConstrained          # the success condition

pad = doc.addObject("PartDesign::Pad", "Pad")
body.addObject(pad)
pad.Profile = sketch
pad.Length = 5
doc.recompute()
_result = {"volume": body.Shape.Volume}     # 100 * 60 * 5 = 30000 mm3
```

Geometry indices in constraints: `-1` is the origin/root geometry, point ids are
`1` = start, `2` = end, `3` = center.

## Pocket

Same as Pad but `PartDesign::Pocket`, and the sketch is mapped onto a face:

```python
pocket = doc.addObject("PartDesign::Pocket", "Pocket")
body.addObject(pocket)
pocket.Profile = inner_sketch
pocket.Length = 3            # or pocket.Type = "ThroughAll"
doc.recompute()
```

## Holes (proper Hole feature, not a pocketed circle)

The Hole feature consumes a sketch made of **circles** — their centers are the
hole positions, the diameter comes from the feature.

```python
sk = doc.addObject("Sketcher::SketchObject", "SketchHoles")
body.addObject(sk)
sk.AttachmentSupport = [(pad, "Face6")]     # the top face of the pad
sk.MapMode = "FlatFace"
doc.recompute()
sk.addGeometry(Part.Circle(App.Vector(20, 30, 0), App.Vector(0, 0, 1), 3), False)
sk.addGeometry(Part.Circle(App.Vector(80, 30, 0), App.Vector(0, 0, 1), 3), False)

hole = doc.addObject("PartDesign::Hole", "Hole")
body.addObject(hole)
hole.Profile = sk
hole.Diameter = 6
hole.DepthType = "ThroughAll"               # or "Dimension" + hole.Depth
doc.recompute()
```

Face names (`Face6`) depend on the geometry — never hardcode them blindly. Find
the right one first, e.g. by picking the face whose normal is +Z and whose
`CenterOfMass.z` is the pad top:

```python
faces = pad.Shape.Faces
top = max(range(len(faces)), key=lambda i: faces[i].CenterOfMass.z)
support_name = "Face%d" % (top + 1)          # OCCT indices are 1-based
```

## Fillet / Chamfer

```python
fillet = doc.addObject("PartDesign::Fillet", "Fillet")
body.addObject(fillet)
fillet.Base = (hole, ["Edge1"])              # (previous feature, [edge names])
fillet.Radius = 2
doc.recompute()
```

`PartDesign::Chamfer` works the same with `.Size`.

## Validation

* `body.Tip` is the last feature; `body.Shape` is the current solid.
* Every feature must end with `State == ['Up-to-date']` and
  `getStatusString() == 'Valid'` — check with `fc_recompute`.
* `fc_check` on the Body: expect `is_valid`, `counts.solids == 1`.
* `fc_measure` volume/bbox against the spec.
