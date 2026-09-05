#!/usr/bin/env python3
"""Tests for bridge/cam_sim_video.py — the 2.5D machining simulator.

Runs on the host python3, with no FreeCAD and no ffmpeg: the simulator's core
is the heightmap, and every assertion below measures it. The module needs numpy
and Pillow, which are optional extras of this repo; when they are absent the
suite says so and skips instead of failing.

    python3 tests/test_sim_video.py
"""

import importlib.util
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = os.path.join(HERE, os.pardir, "bridge", "cam_sim_video.py")

FAILURES = []
CHECKS = [0]


def load_module():
    spec = importlib.util.spec_from_file_location("cam_sim_video", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(label, condition, detail=""):
    CHECKS[0] += 1
    if condition:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


def close(label, value, expected, tolerance):
    check("%s (%.4f ~ %.4f +/- %.4f)" % (label, value, expected, tolerance),
          abs(value - expected) <= tolerance)


def simulate(module, gcode, tool, resolution=0.05, stock=(10.0, 10.0, 5.0)):
    """Run one snippet on a fresh stock and hand back (stock, simulation)."""
    block = module.Stock(stock[0], stock[1], stock[2],
                         origin="center", resolution=resolution)
    simulation = module.Simulation(block, rapid_feed=2000.0)
    simulation.run_file("test.gcode", gcode, tool)
    return block, simulation


def test_straight_slot(module):
    """A 2 mm end mill at Z-1 leaves a 2 mm wide slot exactly 1 mm deep."""
    gcode = "\n".join([
        "G21 G90 G17",
        "M3 S10000",
        "G0 X-3.000 Y0.000 Z5.000",
        "G1 Z-1.000 F150",
        "G1 X3.000 Y0.000 F400",
        "G0 Z5.000",
        "M5",
    ])
    block, _simulation = simulate(module, gcode, "endmill:2.0", resolution=0.05)
    heightmap = block.heightmap
    close("slot floor", float(heightmap.min()), -1.0, 1e-4)

    # Cross-section at X = 0: how many cells reached the floor.
    column, _row = block.cell_of(0.0, 0.0)
    cut = heightmap[:, column] <= -0.99
    cells = int(cut.sum())
    expected = 2.0 / block.resolution  # 40 cells
    check("slot width %d cells (expected %d +/- 2)" % (cells, expected),
          abs(cells - expected) <= 2)

    # Nothing outside the slot: 3 mm away in Y the stock is untouched.
    _column, row_far = block.cell_of(0.0, 3.0)
    close("stock untouched away from the slot",
          float(heightmap[row_far, column]), 0.0, 1e-6)


def test_full_circle_arc(module):
    """A G2 full circle (I/J form) carves a closed ring, not a dot."""
    gcode = "\n".join([
        "G21 G90 G17",
        "G0 X2.000 Y0.000 Z5.000",
        "G1 Z-0.500 F150",
        "G2 X2.000 Y0.000 I-2.000 J0.000 F400",
        "G0 Z5.000",
    ])
    block, _simulation = simulate(module, gcode, "endmill:1.0", resolution=0.05)
    heightmap = block.heightmap
    close("ring floor", float(heightmap.min()), -0.5, 1e-4)

    # Cut on the ring in all four quadrants, untouched at the centre.
    for x, y in ((2.0, 0.0), (-2.0, 0.0), (0.0, 2.0), (0.0, -2.0)):
        column, row = block.cell_of(x, y)
        check("ring cut at (%.0f, %.0f)" % (x, y), heightmap[row, column] <= -0.49,
              "z=%.4f" % heightmap[row, column])
    column, row = block.cell_of(0.0, 0.0)
    close("circle centre untouched", float(heightmap[row, column]), 0.0, 1e-6)


def test_vbit_plunge(module):
    """A 30 deg V-bit at Z-1 opens 2*1*tan(15) + tip = 0.636 mm across."""
    gcode = "\n".join([
        "G21 G90 G17",
        "G0 X0.000 Y0.000 Z5.000",
        "G1 Z-1.000 F150",
        "G0 Z5.000",
    ])
    resolution = 0.01
    block, _simulation = simulate(module, gcode, "vbit:30:0.1", resolution=resolution)
    heightmap = block.heightmap
    close("cone tip depth", float(heightmap.min()), -1.0, 1e-4)

    _column, row = block.cell_of(0.0, 0.0)
    touched = heightmap[row, :] < -1e-6
    diameter = int(touched.sum()) * resolution
    expected = 2.0 * 1.0 * math.tan(math.radians(15.0)) + 0.1
    close("cone diameter at the surface", diameter, expected, 3 * resolution)


def test_radius_arc(module):
    """The R form of an arc: |R| picks the radius, the direction picks the side."""
    template = "\n".join([
        "G21 G90 G17",
        "G0 X0.000 Y0.000 Z5.000",
        "G1 Z-0.500 F150",
        "%s X0.000 Y2.000 R1.000 F400",
        "G0 Z5.000",
    ])
    # A semicircle centred on (0, 1): CCW (G3) bulges to +X, CW (G2) to -X.
    for code, inside, outside in (("G3", (1.0, 1.0), (-1.0, 1.0)),
                                  ("G2", (-1.0, 1.0), (1.0, 1.0))):
        block, _simulation = simulate(module, template % code, "endmill:0.5",
                                      resolution=0.02)
        heightmap = block.heightmap
        close("%s R-arc floor" % code, float(heightmap.min()), -0.5, 1e-4)
        column, row = block.cell_of(*inside)
        check("%s R-arc passes through (%.0f, %.0f)" % ((code,) + inside),
              heightmap[row, column] <= -0.49, "z=%.4f" % heightmap[row, column])
        column, row = block.cell_of(*outside)
        close("%s R-arc does not touch (%.0f, %.0f)" % ((code,) + outside),
              float(heightmap[row, column]), 0.0, 1e-6)


def test_rapid_below_zero_counts_as_crash(module):
    """A G0 with the tool in the material is a crash, and it is counted."""
    gcode = "\n".join([
        "G21 G90 G17",
        "G0 X-3.000 Y0.000 Z5.000",
        "G1 Z-1.000 F150",
        "G0 X3.000 Y0.000",
        "G0 Z5.000",
    ])
    block, simulation = simulate(module, gcode, "endmill:2.0", resolution=0.1)
    check("crash counted", simulation.crash_rapids == 1,
          "got %d" % simulation.crash_rapids)
    # The rapid still removed material along its way.
    column, row = block.cell_of(0.0, 0.0)
    close("crash rapid removed material", float(block.heightmap[row, column]), -1.0, 1e-4)

    clean = "\n".join([
        "G21 G90 G17",
        "G0 X0.000 Y0.000 Z5.000",
        "G1 Z-1.000 F150",
        "G0 Z5.000",
    ])
    _block, simulation = simulate(module, clean, "endmill:2.0", resolution=0.1)
    check("no crash on a safe file", simulation.crash_rapids == 0,
          "got %d" % simulation.crash_rapids)

    # A GRBL peck cycle rapids back down into the hole it just drilled. That is
    # below Z0 but removes nothing, so it must not be reported as a crash.
    peck = "\n".join([
        "G21 G90 G17",
        "G0 X0.000 Y0.000 Z5.000",
        "G1 Z-1.000 F150",
        "G0 Z0.000",        # peck retract
        "G0 Z-0.900",       # rapid back to just above the last depth
        "G1 Z-2.000 F150",
        "G0 Z5.000",
    ])
    _block, simulation = simulate(module, peck, "endmill:2.0", resolution=0.1)
    check("peck re-entry is not a crash", simulation.crash_rapids == 0,
          "got %d at lines %s" % (simulation.crash_rapids, simulation.crash_lines))


def test_units_and_incremental(module):
    """G20 inches and G91 incremental both land where they should."""
    gcode = "\n".join([
        "G20 G90 G17",          # inches
        "G0 X0.0 Y0.0 Z0.2",
        "G1 Z-0.03937 F10",     # -1.000 mm
        "G91",                  # incremental from here
        "G1 X0.03937 F10",      # +1.000 mm in X
        "G90",
        "G0 Z0.2",
    ])
    block, _simulation = simulate(module, gcode, "endmill:1.0", resolution=0.05)
    close("inch plunge depth", float(block.heightmap.min()), -1.0, 1e-3)
    column, row = block.cell_of(1.0, 0.0)
    check("incremental move reached X = 1 mm", block.heightmap[row, column] <= -0.99,
          "z=%.4f" % block.heightmap[row, column])


def test_unknown_codes_warn_once(module):
    """Unknown codes are ignored, with exactly one warning per code."""
    gcode = "\n".join([
        "G21 G90 G17",
        "G0 X0 Y0 Z5",
        "G77 X1",
        "G77 X2",
        "G1 Z-0.5 F100",
    ])
    _block, simulation = simulate(module, gcode, "endmill:1.0", resolution=0.1)
    warnings = [item for item in simulation.warnings if "G77" in item]
    check("one warning for the unknown G77", len(warnings) == 1, str(simulation.warnings))


def test_machine_time(module):
    """Machine time is length over feed, rapids at the rapid feed."""
    gcode = "\n".join([
        "G21 G90 G17",
        "G0 X0 Y0 Z0",          # no motion from the origin
        "G1 X100.000 F600",     # 100 mm at 600 mm/min = 10 s
        "G0 X0.000",            # 100 mm at 2000 mm/min = 3 s
    ])
    _block, simulation = simulate(module, gcode, "endmill:1.0",
                                  resolution=0.5, stock=(200.0, 10.0, 5.0))
    close("machine seconds", simulation.machine_seconds, 13.0, 0.05)


def test_render_frame(module):
    """One frame renders to an RGB image of the declared, even-sided size."""
    block = module.Stock(10.0, 10.0, 5.0, origin="center", resolution=0.1)
    block.heightmap[10:40, 10:40] = -1.5
    renderer = module.Renderer(block, deepest=2.0, hud_height=60)
    check("even width", renderer.width % 2 == 0, str(renderer.width))
    check("even height", renderer.height % 2 == 0, str(renderer.height))
    check("body plus HUD", renderer.height == block.ny + renderer.hud_height,
          "%d vs %d + %d" % (renderer.height, block.ny, renderer.hud_height))

    image = renderer.frame(
        block.heightmap,
        {"line1": "test.gcode  end mill", "line2": "t 0:00:10  Z -1.500"},
        tool=(0.0, 0.0, 1.5), banner="tool change")
    check("RGB mode", image.mode == "RGB", image.mode)
    check("frame size", image.size == (renderer.width, renderer.height), str(image.size))
    pixels = image.load()
    check("cut area is darker than the untouched stock",
          sum(pixels[20, block.ny - 20]) < sum(pixels[block.nx - 5, 5]))
    check("HUD bar drawn", sum(pixels[5, renderer.height - 3]) < 200)


def main():
    print("### cam_sim_video simulator tests")
    try:
        module = load_module()
    except Exception as exc:  # pragma: no cover
        print("  FAIL could not import bridge/cam_sim_video.py: %s" % exc)
        return 1
    if module.MISSING_DEPS:
        print("  SKIP cam_sim_video needs %s (optional extras); install them "
              "with: python3 -m pip install --user numpy Pillow"
              % " and ".join(module.MISSING_DEPS))
        return 0

    for test in (test_straight_slot, test_full_circle_arc, test_vbit_plunge,
                 test_radius_arc, test_rapid_below_zero_counts_as_crash,
                 test_units_and_incremental, test_unknown_codes_warn_once,
                 test_machine_time, test_render_frame):
        print("- %s" % test.__doc__.splitlines()[0])
        test(module)

    print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
