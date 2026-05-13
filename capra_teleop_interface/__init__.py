"""Rove control interface package."""
from __future__ import annotations

import os
import sys

# The generated protobuf modules (proto/core/*_pb2.py) are emitted by
# protoc with cross-references such as ``from proto.core import JointState_pb2``.
# Those references are top-level, so the package's own directory must be on
# sys.path for them to resolve when this package is launched from the outside
# (e.g. ``python -m control_interface``). We can't rewrite the generated
# code and the .proto imports are a fixed contract, so we patch sys.path once
# at package import time.
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)
