"""strong-loop: an agent proposes, code disposes.

The agent proposes hypotheses. A fixed statistical gate (`gates.py`) is the only thing
that can turn one into `Evidence`. An append-only ledger on disk (`ledger.py`) is the only
memory between iterations, each of which starts with an empty context.
"""
