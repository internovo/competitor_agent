from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graph.nodes import pipeline as n
from app.graph.state import GraphState


def build_graph():
    g = StateGraph(GraphState)
    g.add_node("geocode", n.geocode)
    g.add_node("discover", n.discover)
    g.add_node("resolve", n.resolve_node)
    g.add_node("extract", n.extract)
    g.add_node("filter", n.filter_node)
    g.add_node("score", n.score)
    g.add_node("retry_thin", n.retry_thin)
    g.add_node("narrate", n.narrate)
    g.add_node("persist", n.persist)

    g.add_edge(START, "geocode")
    g.add_edge("geocode", "discover")
    g.add_edge("discover", "resolve")
    g.add_conditional_edges("resolve", n.fan_out_extract, ["extract", "filter"])
    g.add_edge("extract", "filter")
    g.add_edge("filter", "score")
    g.add_conditional_edges("score", n.route_after_score, ["retry_thin", "narrate"])
    g.add_conditional_edges("retry_thin", n.fan_out_retry, ["extract", "narrate"])
    g.add_edge("narrate", "persist")
    g.add_edge("persist", END)
    return g.compile()


graph = build_graph()
