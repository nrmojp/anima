"""Compatibility alias for anima.bootstrap.process_guard."""

from importlib import import_module as _import_module
import sys as _sys

_sys.modules[__name__] = _import_module("anima.bootstrap.process_guard")
