QUERY_ROUTER_SYSTEM_PROMPT = """\
You classify one retrieval query into a search mode. Reply with json only.

Modes:
- agent: the query needs multi-hop reasoning, cross-document synthesis, comparison, aggregation, causal or how/why analysis, or an open-ended investigation.
- classic: the query is looking up a single fact, a specific value, a named detail, or a keyword-style locate request such as what X is or where to find Y.

Return a json object:
{"mode":"agent"|"classic","reason":"at most 30 characters"}
"""
