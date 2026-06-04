"""Back-compat shim — canonical impl now lives in ``app.tools.connection_resolver``.

The resolver moved under ``app/`` so it ships in the Agent Runtime wheel
(``packages = ["app","frontend"]``; the ``ingest`` namespace package is NOT
deployed to Agent Runtime). The Cloud Run webhook receiver has ``app/`` on its
path, so this re-export keeps ``ingest.webhook_receiver.main`` and any existing
imports/patches of this module path working unchanged.
"""

from __future__ import annotations

from app.tools.connection_resolver import (  # noqa: F401
    _FIVETRAN_API_BASE,
    _cache,
    _fetch_schema,
    log,
    resolve_destination_schema,
)
