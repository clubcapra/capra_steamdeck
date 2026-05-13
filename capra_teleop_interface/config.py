"""Config file loader for capra_teleop_interface.

Loads a YAML file and exposes values as argparse-compatible defaults.
CLI arguments always take priority over config file values.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

try:
    import yaml as _yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


def load(path: Path) -> dict[str, Any]:
    """Parse a YAML config file and return a flat dict of arg-compatible keys.

    The YAML may use a flat structure (keys matching argparse dest names) or
    nest options under arbitrary section headers for readability — either way
    the resulting dict is flattened to a single level.

    Keys that do not correspond to a known argparse dest are silently ignored
    by ``argparse.set_defaults(**cfg)``.
    """
    if not _YAML_AVAILABLE:
        raise RuntimeError(
            "pyyaml is required for --config support.  "
            "Install it with: pip install pyyaml"
        )
    with open(path) as fh:
        data = _yaml.safe_load(fh) or {}

    flat: dict[str, Any] = {}
    for key, val in data.items():
        if isinstance(val, dict):
            flat.update(val)
        else:
            flat[key] = val

    if "log_dir" in flat:
        flat["log_dir"] = Path(flat["log_dir"])

    return flat


def apply(ns: argparse.Namespace, cfg: dict[str, Any]) -> argparse.Namespace:
    """Write config values into *ns* for any key whose current value is None.

    Only keys already present as attributes on *ns* are touched.  This means
    config file keys that have no matching ``add_argument`` are silently
    dropped without raising.
    """
    for key, val in cfg.items():
        if hasattr(ns, key) and getattr(ns, key) is None:
            setattr(ns, key, val)
    return ns
