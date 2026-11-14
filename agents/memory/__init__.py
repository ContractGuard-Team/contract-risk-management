"""Agent memory, separate for every agent (nothing is shared between agents):

- short term (short_term.py): context of one contract run, deleted when the run is finished;
- long term (long_term.py): lessons learned across runs, one SQLite file per agent.

Each agent keeps its own memory prompts in agents/<agent>/memory/.
"""
SHORT_TERM_PROMPT = "memory/short_term_summary.md"   # how this agent compresses its short-term context
LONG_TERM_PROMPT = "memory/long_term_reflection.md"  # how this agent draws lessons from a finished run
