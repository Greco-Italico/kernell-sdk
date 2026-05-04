import os

MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", 8))
EXECUTION_TIMEOUT = float(os.environ.get("EXECUTION_TIMEOUT", 2.0))
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "SUPER_SECRET_INTERNAL_TOKEN")
