"""The fake 'ok' catalog, except that its FIRST start dies at once (like a server that is restarting).
For the node-retry drill: the graph must retry the workflow node and then succeed.
The start count lives in EVAL_STATE/flaky_catalog_starts (a test deletes it first)."""
import _paths  # noqa: F401
import sys

import eval_servers

counter = eval_servers.EVAL_STATE / "flaky_catalog_starts"
eval_servers.EVAL_STATE.mkdir(parents=True, exist_ok=True)
starts = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(starts))
if starts == 1:
    sys.exit(3)                     # died before answering: the client sees "Connection closed"
eval_servers.serve_catalog("ok")