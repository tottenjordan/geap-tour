import types as _t

from src.router.agents import flash_agent

agent = _t.SimpleNamespace(root_agent=flash_agent)
root_agent = flash_agent
