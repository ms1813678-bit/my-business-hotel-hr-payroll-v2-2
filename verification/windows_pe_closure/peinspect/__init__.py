"""Read-only PE32+ dependency inspector and deterministic closure walker."""
from .pe import PEFormatError, PEImage, parse_pe, parse_file, read_machine
from .closure import run_closure, is_apiset_name, forwarder_target, REPORT_SCHEMA
from . import apiset

__version__ = "0.1.0"
