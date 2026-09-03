# Part recipe (direct solid modeling)

The Part workbench is the fastest path when the model does not need a
parametric feature history: primitives plus boolean operations, straight OCCT.
**Verified headless** on FreeCAD 1.1.3.

## Primitives as document objects (parametric, editable in the tree)

```python
doc = doc or App.newDocument("Model")
box = doc.addObject("Part::Box", "Box")
box.Length, box.Width, box.Height = 100, 60, 5
cyl = doc.addObject("Part::Cylinder", "Cyl")
cyl.Radius, cyl.Height = 3, 20
cyl.Placement.Base = App.Vector(20, 30, -5)
doc.recompute()
_result = {"volume": box.Shape.Volume}
```

Other primitives: `Part::Sphere`, `Part::Cone`, `Part::Torus`, `Part::Plane`,
`Part::Prism`, `Part::Wedge`.

## Booleans

```python
cut = doc.addObject("Part::Cut", "Cut")
cut.Base = box
cut.Tool = cyl
doc.recompute()
```

`Part::Fuse` (union), `Part::Common` (intersection), `Part::MultiCommon` and
`Part::MultiFuse` for more than two shapes.

## Non-parametric shapes (fast, no history)

```python
import Part
shape = Part.makeBox(100, 60, 5).cut(
    Part.makeCylinder(3, 20, App.Vector(20, 30, -5)))
obj = doc.addObject("Part::Feature", "Bracket")
obj.Shape = shape
doc.recompute()
```

Useful shape methods: `cut`, `fuse`, `common`, `makeFillet(radius, edges)`,
`makeChamfer`, `extrude`, `revolve`, `mirror`, `removeSplitter()` (merges
coplanar faces after booleans).

## Validation

* `fc_check`: `is_valid`, `counts.solids == 1`, `is_closed` for a solid;
* `fc_measure` volume/bbox against the spec;
* after heavy booleans, `shape.removeSplitter()` before exporting.
