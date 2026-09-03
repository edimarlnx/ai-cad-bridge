# TechDraw recipe

2D drawings: page + template -> views -> dimensions -> export.
**Verified headless** on FreeCAD 1.1.3: page, SVG template, `DrawViewPart`
(4 visible edges on a box, `Up-to-date`/`Valid`), `DrawViewDimension` and DXF
export all work under FreeCADCmd.

**Headless limitation (measured):** the module-level page exporters for SVG and
PDF live in `TechDrawGui` (`Cannot load Gui module in console application`), so
in a headless session only DXF page export is available. In a GUI session use
`TechDrawGui.exportPageAsSvg(page, path)` / `exportPageAsPdf(page, path)`.

## Page from the default template

```python
import os, TechDraw
doc = doc or App.ActiveDocument

page = doc.addObject("TechDraw::DrawPage", "Page")
template = doc.addObject("TechDraw::DrawSVGTemplate", "Template")
template.Template = os.path.join(
    App.getResourceDir(), "Mod", "TechDraw", "Templates",
    "Default_Template_A4_Landscape.svg")
page.Template = template          # a page WITHOUT a template rejects addView()
```

Templates on this machine live in `<resource dir>/Mod/TechDraw/Templates`:
`Default_Template_A4_Landscape.svg`, plus the `ISO/` and `ASME/` folders
(e.g. `ISO/A3_Landscape_ISO5457_advanced.svg`). List the directory before
hardcoding a name.

## Part view

```python
view = doc.addObject("TechDraw::DrawViewPart", "View")
page.addView(view)
view.Source = [body]              # any solid: Body, Part::Feature, App::Link...
view.Direction = App.Vector(0, 0, 1)      # projection direction
view.Scale = 1.0
view.X, view.Y = 100, 150         # position on the page, in mm
doc.recompute()
_result = {"status": view.getStatusString(), "edges": len(view.getVisibleEdges())}
```

`view.getVisibleEdges()` / `getHiddenEdges()` returning zero edges means the
view has no geometry — the usual causes are an empty `Source` or a direction
parallel to a degenerate shape.

Other view types: `TechDraw::DrawProjGroup` (multi-view projection),
`TechDraw::DrawViewSection`, `TechDraw::DrawViewDetail`,
`TechDraw::DrawViewAnnotation`. Use `fc_api_help` with the TypeId to list their
properties.

## Dimension

```python
dim = doc.addObject("TechDraw::DrawViewDimension", "Dim")
page.addView(dim)
dim.References2D = [(view, "Edge0")]      # 2D geometry of the view, 0-based
dim.Type = "DistanceX"                    # Distance, DistanceX, DistanceY,
                                          # Radius, Diameter, Angle, Angle3Pt
doc.recompute()
_result = dim.getStatusString()           # 'Valid'
```

Read the measured value with `dim.getRawValue()` (drawing units) or format it
with `dim.FormatSpec`. `References3D` takes `(object, "EdgeN")` of the *model*
instead, which survives view regeneration better.

## Export

```python
out = os.path.expanduser("~/.cache/freecad-ai-bridge/page.dxf")
TechDraw.writeDXFPage(page, out)          # works headless
TechDraw.writeDXFView(view, out)          # single view

svg_fragment = TechDraw.viewPartAsSvg(view)   # SVG string of one view, headless
dxf_fragment = TechDraw.viewPartAsDxf(view)

# GUI session only:
# import TechDrawGui
# TechDrawGui.exportPageAsSvg(page, path)
# TechDrawGui.exportPageAsPdf(page, path)
```

## Validation

* every view `State == ['Up-to-date']` and `getStatusString() == 'Valid'`;
* `len(view.getVisibleEdges()) > 0`;
* the exported file exists and is non-empty (`fc_exec` can `os.path.getsize`).
