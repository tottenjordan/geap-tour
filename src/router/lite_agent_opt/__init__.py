import types as _t

from src.router.agents import lite_agent

agent = _t.SimpleNamespace(root_agent=lite_agent)
root_agent = lite_agent
