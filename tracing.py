"""
Phase 3, Step 4: OpenTelemetry tracing. "Who did what, inside which request, and how long it took."

THE WORDS
    trace      one request, start to end ("play The Friend 2025"). Has a trace id.
    span       one piece of work inside it: an agent run, one model call, one tool call,
               one connection. Has a name, a start and end time, attributes, and a PARENT.
    parent     the span that was open when this one started. Parents make the tree:
                   agent supervisor
                     execute_tool ask_catalog_agent
                       agent catalog            <- a whole agent, nested inside a tool call
                         execute_tool search_title
    attribute  a key/value fact on a span: gen_ai.agent.name=catalog, mcp.server=catalog,
               gen_ai.usage.input_tokens=812, tool.is_error=false ...
    context    "which span is open right now". Python keeps it in a contextvar, which
               asyncio carries into awaited code. That is why the Catalog agent's spans land
               UNDER ask_catalog_agent without any code passing a span around.
    exporter   where finished spans go: printed as a tree (TRACING=print), or sent to Jaeger
               over OTLP (TRACING=jaeger), or kept in memory (the tests).

WHAT IS NEVER RECORDED
    Tokens. Tool RESULTS only with TRACE_CONTENT=1 (they can hold watch history). Prompts and
    model text are not recorded. A trace is data that leaves the process: treat it like a log.

Without setup() every span call is a cheap no-op (the OpenTelemetry API's default).
"""
import json
from collections import defaultdict
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (BatchSpanProcessor, SimpleSpanProcessor, SpanExporter,
                                            SpanExportResult)
from opentelemetry.trace import Status, StatusCode

import config

tracer = trace.get_tracer("watch_concierge")
_provider: TracerProvider | None = None
_tree: "TreeExporter | None" = None
_jaeger: "CountingExporter | None" = None


@contextmanager
def span(name: str, attrs: dict | None = None):
    """Open a span as a child of whatever span is open now. None-valued attributes are skipped."""
    with tracer.start_as_current_span(name) as s:
        set_attrs(s, attrs or {})
        yield s


def set_attrs(s, attrs: dict):
    for key, value in attrs.items():
        if value is None:
            continue
        if isinstance(value, (dict, list, tuple)) and not (
                isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value)):
            value = json.dumps(value, sort_keys=True)[:500]      # attributes must be simple values
        s.set_attribute(key, value)


def failed(s, description: str):
    s.set_status(Status(StatusCode.ERROR, description[:200]))


def content(text: str) -> str | None:
    """A tool result for a span attribute, only if TRACE_CONTENT=1."""
    return " ".join(text.split())[:300] if config.TRACE_CONTENT else None


# ---------------------------------------------------------------- the printed tree

class TreeExporter(SpanExporter):
    """Keeps finished spans; print_tree() shows each trace as an indented tree with durations."""

    def __init__(self):
        self.spans = []

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def print_tree(self):
        if not self.spans:
            return
        children = defaultdict(list)
        ids = {s.context.span_id for s in self.spans}
        roots = []
        for s in self.spans:
            parent = s.parent.span_id if s.parent else None
            (children[parent] if parent in ids else roots).append(s)

        def line(s, prefix, last):
            ms = (s.end_time - s.start_time) / 1e6
            dur = f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"
            label = f"{prefix}{'└─ ' if last else '├─ '}{s.name}"
            print(f"{label:<58} {dur:>7}  {summary(s)}")
            kids = sorted(children[s.context.span_id], key=lambda k: k.start_time)
            for i, k in enumerate(kids):
                line(k, prefix + ("   " if last else "│  "), i == len(kids) - 1)

        for root in sorted(roots, key=lambda r: r.start_time):
            print(f"\n=== trace {root.context.trace_id:032x} ===")
            line(root, "", True)
        self.spans.clear()


def summary(s) -> str:
    a = s.attributes or {}
    bits = []
    if "enduser.id" in a:
        bits.append(f"as {a['enduser.id']}")
    if "keycloak.audience" in a:
        bits.append(f"aud={a['keycloak.audience']}")
    if "gen_ai.usage.input_tokens" in a:
        bits.append(f"in={a['gen_ai.usage.input_tokens']} out={a.get('gen_ai.usage.output_tokens')}")
    if "gen_ai.response.finish_reasons" in a:
        bits.append("-> " + ",".join(a["gen_ai.response.finish_reasons"]))
    if "tool.args" in a:
        args = str(a["tool.args"])
        bits.append(args if len(args) <= 60 else args[:57] + "...")
    if a.get("tool.blocked"):
        bits.append(f"BLOCKED ({a['tool.blocked']})")
    elif "tool.executed" in a and not a["tool.executed"]:
        bits.append("NOT RUN")
    if a.get("agent.outcome"):
        bits.append(str(a["agent.outcome"]))
    if not s.status.is_ok:
        error = " ".join((s.status.description or "").split())
        error = error.replace("Error executing tool ", "")
        bits.append(f"ERROR {error[:70]}{'...' if len(error) > 70 else ''}".strip())
    return "  ".join(bits)


class CountingExporter(SpanExporter):
    """Wraps the OTLP exporter and remembers whether sending worked, so we never claim
    "sent to Jaeger" when Jaeger was not there (the same honesty rule as for the agent)."""

    def __init__(self, inner: SpanExporter):
        self.inner, self.sent, self.failed = inner, 0, 0

    def export(self, spans):
        result = self.inner.export(spans)
        if result == SpanExportResult.SUCCESS:
            self.sent += len(spans)
        else:
            self.failed += len(spans)
        return result

    def shutdown(self):
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000):
        return self.inner.force_flush(timeout_millis)


# ---------------------------------------------------------------- setup

def setup(service_name: str = "watch-concierge", extra_exporter: SpanExporter | None = None,
          allow_print: bool = True):
    """Install the SDK according to TRACING. Call once, at program start.

    MCP servers pass allow_print=False: over stdio their stdout IS the protocol channel, and a
    printed tree would corrupt it. They export to Jaeger only.
    """
    global _provider, _tree, _jaeger
    modes = set(config.TRACING) - (set() if allow_print else {"print"})
    if not modes and extra_exporter is None:
        return                                     # tracing off: spans stay no-ops
    _provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if "print" in modes:
        _tree = TreeExporter()
        _provider.add_span_processor(SimpleSpanProcessor(_tree))
    if "jaeger" in modes:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        _jaeger = CountingExporter(OTLPSpanExporter(endpoint=config.OTLP_ENDPOINT.rstrip("/") + "/v1/traces",
                                                    timeout=3))
        _provider.add_span_processor(BatchSpanProcessor(_jaeger))
    if extra_exporter is not None:
        _provider.add_span_processor(SimpleSpanProcessor(extra_exporter))
    trace.set_tracer_provider(_provider)


def finish():
    """Print the tree (TRACING=print) and send what is left to Jaeger before the program exits."""
    if _provider is None:
        return
    _provider.force_flush()
    if _tree is not None:
        _tree.print_tree()
    if _jaeger is not None and _tree is not None:       # only the agent prints; servers stay quiet
        if _jaeger.failed:
            print(f"\n(tracing: could NOT reach {config.OTLP_ENDPOINT}: {_jaeger.failed} spans lost. "
                  "Is Jaeger running? See SETUP.md)")
        else:
            print(f"\n(tracing: {_jaeger.sent} spans sent to Jaeger: open http://localhost:16686, "
                  "service 'watch-concierge (team)' or '(single agent)')")