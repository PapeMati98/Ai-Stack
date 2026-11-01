"""coordfs: filesystem-backed coordination board for Agent Graph write scopes."""

from .constants import LEASE_FIELDS, VERSION
from .errors import CoordError
from .identity import canonicalize_resource
from .leases import CoordBoard

__version__ = VERSION
__all__ = [
    "CoordBoard",
    "CoordError",
    "LEASE_FIELDS",
    "canonicalize_resource",
    "__version__",
]
