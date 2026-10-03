"""ASGI entrypoint for deployment platforms (for example, Vercel).

This module exports a top-level ``app`` object so hosts can import it without
executing a one-off analysis run.
"""

from tradingagents.local_api import create_app

# Vercel/ASGI runtime looks for one of: app, application, or handler.
app = create_app()
