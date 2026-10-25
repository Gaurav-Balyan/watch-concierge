"""
The A2A CLIENT side (idea 2, step 2a): ask a remote agent a question, get its task back.

    ask_agent(url, question)
      1. DISCOVERY: GET <url>/.well-known/agent-card.json  -> the Agent Card (who it is, where to send messages)
      2. SEND:      JSON-RPC SendMessage with the question  -> the server runs its agent, returns the final TASK
      3. READ:      state COMPLETED -> the "answer" artifact (text, from its MODEL) and the "searched_ids" artifact
                    (data, from its CODE); FAILED / anything else -> RemoteAgentError with the server's reason

`http_client` is a hook: tests replace it with an in-process transport (no port, no second process).
"""
from dataclasses import dataclass, field

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import get_artifact_text, get_data_parts, get_message_text, new_text_message
from a2a.types import Role, SendMessageRequest, TaskState

from common import config
from common.tracing import failed, set_attrs, span


class RemoteAgentError(Exception):
    pass


@dataclass
class RemoteAnswer:
    text: str                                        # the remote MODEL's answer: untrusted text
    searched_ids: set = field(default_factory=set)   # (service, id) from the remote agent's CODE
    task_id: str = ""
    agent: str = ""


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=config.A2A_TIMEOUT)


async def ask_agent(url: str, question: str) -> RemoteAnswer:
    with span("a2a send", {"a2a.url": url}) as s:
        async with http_client() as http:
            card = await A2ACardResolver(httpx_client=http, base_url=url).get_agent_card()      # 1. discovery
            set_attrs(s, {"a2a.agent": card.name, "a2a.skills": [k.id for k in card.skills]})
            client = await create_client(agent=card, client_config=ClientConfig(streaming=False, httpx_client=http))
            try:
                task = None
                request = SendMessageRequest(message=new_text_message(question, role=Role.ROLE_USER))
                async for chunk in client.send_message(request):                               # 2. send
                    if chunk.HasField("task"):
                        task = chunk.task
            finally:
                await client.close()
        if task is None:
            failed(s, "no task in the reply")
            raise RemoteAgentError(f"{card.name} answered without a task")
        state = TaskState.Name(task.status.state)
        set_attrs(s, {"a2a.task_id": task.id, "a2a.state": state})
        if task.status.state != TaskState.TASK_STATE_COMPLETED:                                  # 3. read
            why = get_message_text(task.status.message) if task.status.HasField("message") else "no reason given"
            failed(s, why)
            raise RemoteAgentError(f"{card.name} task {state}: {why}")
        by_name = {a.name: a for a in task.artifacts}
        text = get_artifact_text(by_name["answer"]) if "answer" in by_name else ""
        ids = set()
        if "searched_ids" in by_name:
            for data in get_data_parts(by_name["searched_ids"].parts):
                ids |= {(str(p[0]), str(p[1])) for p in (data or {}).get("searched_ids", []) if len(p) == 2}
        return RemoteAnswer(text=text, searched_ids=ids, task_id=task.id, agent=card.name)