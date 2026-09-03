# Assembly recipe (built-in Assembly workbench, FreeCAD 1.x)

**Verified headless** on FreeCAD 1.1.3: `Assembly::AssemblyObject`, links,
joint group, a Fixed joint and `assembly.solve()` all work under FreeCADCmd —
the MbD solver runs and reports `Convergence` in the console. Only the
interactive selection helpers (task panels, `Gui.Selection`) need a GUI.

## Create an assembly and insert parts

Parts are inserted as `App::Link` objects created **inside** the assembly, so
each instance has its own placement.

```python
doc = doc or App.newDocument("Assembly")
asm = doc.addObject("Assembly::AssemblyObject", "Assembly")

part_a = doc.addObject("Part::Box", "PartA")
part_a.Length, part_a.Width, part_a.Height = 20, 20, 20
part_b = doc.addObject("Part::Box", "PartB")
part_b.Length, part_b.Width, part_b.Height = 10, 10, 10
doc.recompute()

link_a = asm.newObject("App::Link", "LinkA")   # newObject puts it in the assembly
link_a.LinkedObject = part_a
link_b = asm.newObject("App::Link", "LinkB")
link_b.LinkedObject = part_b
doc.recompute()
_result = [o.Name for o in asm.Group]          # ['LinkA', 'LinkB']
```

For a part living in another file, link to the opened document's object the
same way (`App::Link` -> `LinkedObject`), or use `App::LinkGroup` for sub-assemblies.

## Joints

There is **no `Assembly::JointObject` document type** — joints are
`App::FeaturePython` objects with the `JointObject.Joint` proxy, created inside
the assembly's joint group.

```python
import JointObject, UtilsAssembly

# JointObject.JointTypes ==
# ['Fixed', 'Revolute', 'Cylindrical', 'Slider', 'Ball', 'Distance',
#  'Parallel', 'Perpendicular', 'Angle', 'RackPinion', 'Screw', 'Gears', 'Belt']
joint_group = UtilsAssembly.getJointGroup(asm)          # 'Assembly::JointGroup'
joint = joint_group.newObject("App::FeaturePython", "Fixed")
JointObject.Joint(joint, 0)                             # index into JointTypes

# References are (object, [element_name, ""]) pairs; the element is the vertex,
# edge or face that carries the joint coordinate system.
joint.Reference1 = [link_a, ["Vertex1", ""]]
joint.Reference2 = [link_b, ["Vertex1", ""]]
doc.recompute()
_result = joint.JointType                               # 'Fixed'
```

Useful joint properties: `Distance`, `Distance2`, `Angle`, `Offset1`,
`Offset2`, `Placement1`, `Placement2`, `LengthMin/Max`, `AngleMin/Max`,
`Suppressed`.

## Solve and validate

```python
status = asm.solve()          # 0 == converged
doc.recompute()
_result = {
    "solve": status,
    "b_placement": str(link_b.Placement),
    "grounded": asm.isPartGrounded(link_a),
}
```

Other useful methods on the assembly object: `isPartConnected`,
`isJointConnectingPartToGround`, `getDownstreamParts`, `undoSolve`,
`exportAsASMT`, `generateSimulation`.

Validation to run after solving:

* `asm.solve() == 0`;
* `fc_measure` with `kind: "distance"` between the two links to prove the joint
  placed them where the spec says;
* interference: `link_a.Shape.common(link_b.Shape).Volume` should be ~0 for
  parts that must not overlap.

## Bill of materials

`UtilsAssembly.getBomGroup(asm)` returns the BOM group when one exists; the
straightforward alternative is to count `asm.Group` links by `LinkedObject.Name`.
