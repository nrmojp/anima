"""Public capability-tool surface.

The runtime historically kept these contracts in ``contracts``.  Keep
that module as the implementation while exposing the generic Anima module name
used by self-contained plugins.
"""

from anima.capabilities.contracts import *  # noqa: F401,F403
