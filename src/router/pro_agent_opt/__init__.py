import types as _t

from src.router.agents import pro_agent

agent = _t.SimpleNamespace(root_agent=pro_agent)
root_agent = pro_agent
