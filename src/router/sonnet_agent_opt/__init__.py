import types as _t

from src.router.agents import sonnet_agent

agent = _t.SimpleNamespace(root_agent=sonnet_agent)
root_agent = sonnet_agent
