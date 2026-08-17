import types as _t

from src.router.agents import opus_agent

agent = _t.SimpleNamespace(root_agent=opus_agent)
root_agent = opus_agent
