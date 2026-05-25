"""Functions Framework entrypoint shim.

The actual receiver lives at `ingest/webhook_receiver/main.py:handle_request`,
but Google Cloud's Functions Framework buildpack (used by
`gcloud run deploy --source=. --function=...`) requires a `main.py` at the
project root containing the target function. Rather than relocate the
receiver — which would break the `ingest/` package structure that mirrors the
design doc — we re-export it from here.

Deploy command pairs this with `--function=handle_request`.
"""

from ingest.webhook_receiver.main import handle_request

__all__ = ["handle_request"]
