"""Prompt-to-chart Dashboard feature (additive).

A self-contained package: a read-only connection to the client's real Azure SQL
database (DABS-CORE-SHARE) supplies chart DATA, while chart DEFINITIONS live in a
small local SQLite file. The 5 chart tools are registered into agenticAi's native
Tool registry (kept OUT of the primary agent's allow-list, so the Assistant can
never call them); a dedicated /dashboard SSE endpoint runs the LangGraph executor
scoped to just those tools and streams a chart_saved event per completed chart for
the live one-by-one reveal.

Nothing here modifies existing agenticAi behaviour.
"""
