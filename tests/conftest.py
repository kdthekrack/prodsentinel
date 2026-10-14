import os
import tempfile

# Tests never touch the database of a running ProdSentinel server.
os.environ.setdefault("EWS_DB", os.path.join(tempfile.mkdtemp(prefix="prodsentinel-test-"), "alerts.db"))
