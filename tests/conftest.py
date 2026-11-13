"""pytest: tests never talk to the real Langfuse (the local .env may hold real keys).

load_dotenv() does not override variables that already exist, so empty keys here win.
unittest discover gets the same isolation from tests/test_000_isolation.py (imported first).
"""
import os

os.environ["LANGFUSE_PUBLIC_KEY"] = ""
os.environ["LANGFUSE_SECRET_KEY"] = ""
