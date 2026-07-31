"""Official Python client for Cru's pipeline feature-flag service.

The 99% path is the module-level :data:`flags` client, which reads its
document URL from the ``CRU_FLAGS_URL`` environment variable:

```python
from cru_flags import flags

if flags.enabled("checkout_v2"):
    ...
```

:meth:`Client.enabled` performs no I/O, never blocks and never raises:
unknown flags, a missing ``CRU_FLAGS_URL`` and an unreachable flag service
all answer ``False``. A daemon thread refreshes the document in the
background, starting on the first lookup rather than at import.

Use :class:`Client` directly for tests, dependency injection, or to tune the
poll interval and timeout. See ``docs/design.md`` for the full specification.
"""

from cru_flags._client import ENV_VAR, LOGGER_NAME, Client, OnError, __version__, flags

__all__ = [
    "ENV_VAR",
    "LOGGER_NAME",
    "Client",
    "OnError",
    "__version__",
    "flags",
]
