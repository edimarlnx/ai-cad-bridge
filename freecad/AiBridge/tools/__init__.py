"""Tool modules for the AiBridge add-on.

Each module exposes ``TOOLS``: a list of dicts with ``name``, ``description``,
``inputSchema`` (JSON Schema for the arguments), ``mutating`` (whether the call
must run inside an undoable transaction) and ``handler(App, Gui, params)``.
Handlers take ``App``/``Gui`` as arguments so every one of them is testable
under FreeCADCmd, where ``Gui`` is ``None``.
"""

from . import documents  # noqa: F401
from . import execution  # noqa: F401
from . import geometry  # noqa: F401
from . import history  # noqa: F401
from . import sketching  # noqa: F401
from . import structure  # noqa: F401
from . import viewport  # noqa: F401

MODULES = (
    documents,
    structure,
    execution,
    geometry,
    viewport,
    history,
    sketching,
)


def build_registry():
    """Collect every module's TOOLS into a ``{name: tool}`` mapping."""
    registry = {}
    for module in MODULES:
        for tool in module.TOOLS:
            name = tool["name"]
            if name in registry:
                raise RuntimeError("duplicate tool name: %s" % name)
            registry[name] = tool
    return registry
