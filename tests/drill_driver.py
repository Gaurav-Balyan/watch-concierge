"""
Runs ONE durable graph request in its own process, for the crash drills in test_resilience.py.
Fake catalog + fake LIVE TV (every pretend launch is written to launches.log). No Groq, no TV.

    python tests/drill_driver.py <state dir> run "<request>" <request id>
    python tests/drill_driver.py <state dir> resume - <request id>
    CHAOS_CRASH=play:after python tests/drill_driver.py ...     (the process dies mid-request)
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from concierge import engine as agent
from common import config
from concierge import graph

config.STATE_DIR = Path(sys.argv[1])               # the checkpoints of THIS drill
agent.SERVERS = {"catalog": "evals/srv_catalog_ok.py", "device": "evals/srv_device_live.py"}
command, request, request_id = sys.argv[2], sys.argv[3], sys.argv[4]
answer = asyncio.run(graph.run_durable(request if command == "run" else None, request_id))
print("ANSWER: " + answer)
