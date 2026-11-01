"""hub-caretaker package.

Run-once, stdlib-only caretaker that integrates OpenClaw as the Agent CLI
executor and the paid Muse Code subscription as an optional fallback, while
preserving the existing Hub as the system of record.
"""

from .config import CaretakerConfig, load_config, ConfigError

__all__ = [
    "CaretakerConfig",
    "load_config",
    "ConfigError",
]
