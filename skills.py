"""
Phase 3, Step 7: skills. A ROUTE that needs a model is described by a file, not by Python code.

A skill = which INSTRUCTIONS + which TOOLS + which TOKENS (permissions), plus a turn budget:

    skills/suggest.md
    ---
    name: "suggest"
    description: "Suggest titles for one person or several ..."
    servers: ["taste"]                                   which MCP servers (-> one narrow token each)
    offer: ["get_household_taste"]                       the ONLY server tools the model is shown
    scopes: {"taste": ["taste-household-read"]}          extra permissions on those tokens
    local_tools: ["check_titles"]                        code-side tools (names from a registry)
    max_turns: 5
    ---
    You are the Consensus agent ...                      the instructions (the system prompt)

Header values are JSON (no YAML dependency). `hide` may be used instead of `offer`.
The ROUTER (code) picks the skill, so loading one costs no model call. Workflow routes (play, song,
status) are NOT skills: their rules must be guarantees in code, not instructions to a model.
"""
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent / "skills"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str
    servers: tuple[str, ...]
    offer: frozenset | None = None
    hide: frozenset = frozenset()
    scopes: dict = field(default_factory=dict)
    local_tools: tuple[str, ...] = ()
    max_turns: int = 5
    sha: str = ""                      # of the whole file: the eval fingerprint records it


class SkillError(Exception):
    pass


def parse(text: str, source: str = "?") -> Skill:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise SkillError(f"{source}: must start with a '---' header and close it with '---'")
    header, body = text[4:].split("\n---\n", 1)
    meta = {}
    for n, line in enumerate(header.splitlines(), 2):
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise SkillError(f"{source} line {n}: expected 'key: <json value>'")
        try:
            meta[key.strip()] = json.loads(value.strip())
        except json.JSONDecodeError as e:
            raise SkillError(f"{source} line {n}: value of {key.strip()!r} is not JSON ({e.msg})") from None
    for required in ("name", "description", "servers"):
        if required not in meta:
            raise SkillError(f"{source}: missing {required!r}")
    if "offer" in meta and "hide" in meta:
        raise SkillError(f"{source}: use 'offer' OR 'hide', not both")
    return Skill(
        name=meta["name"], description=meta["description"],
        instructions=" ".join(body.split()),      # one paragraph, like the old prompt strings
        servers=tuple(meta["servers"]),
        offer=frozenset(meta["offer"]) if "offer" in meta else None,
        hide=frozenset(meta.get("hide", [])),
        scopes={k: tuple(v) for k, v in meta.get("scopes", {}).items()},
        local_tools=tuple(meta.get("local_tools", [])),
        max_turns=int(meta.get("max_turns", 5)),
        sha=hashlib.sha1(text.encode("utf-8")).hexdigest()[:10],
    )


def load(name: str) -> Skill:
    path = SKILLS_DIR / f"{name}.md"
    if not path.exists():
        raise SkillError(f"no skill file {path}")
    return parse(path.read_text(encoding="utf-8"), str(path))


def build_agent(skill: Skill, agent_name: str, server_urls: dict[str, str], local_registry: dict):
    """An Agent from a skill. `local_registry`: name -> LocalTool for the skill's local_tools."""
    import agent
    missing = [s for s in skill.servers if s not in server_urls]
    if missing:
        raise SkillError(f"skill {skill.name!r} needs servers {missing}, which are not configured")
    unknown = [t for t in skill.local_tools if t not in local_registry]
    if unknown:
        raise SkillError(f"skill {skill.name!r} asks for unknown local tools {unknown}")
    return agent.Agent(agent_name, skill.instructions, {s: server_urls[s] for s in skill.servers},
                       max_turns=skill.max_turns, scopes=skill.scopes or None,
                       offer=set(skill.offer) if skill.offer is not None else None, hide=set(skill.hide),
                       local_tools=[local_registry[t] for t in skill.local_tools])