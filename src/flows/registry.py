from typing import Callable, Dict, Tuple
from src.core.flow import AgentsFlow
from src.core.flow import FlowContext

# Builder returns (flow, flow_ctx) — description lives with the builder
FLOW_REGISTRY: Dict[str, Callable[[], Tuple[AgentsFlow, FlowContext]]] = {}

def register_flow(name: str, builder: Callable[[], Tuple[AgentsFlow, FlowContext]]):
    FLOW_REGISTRY[name] = builder

def get_flow(flow_name: str) -> Tuple[AgentsFlow, FlowContext]:
    builder = FLOW_REGISTRY.get(flow_name)
    if not builder:
        raise ValueError(f"Unknown flow: '{flow_name}'. Registered flows: {list(FLOW_REGISTRY.keys())}")
    return builder()

def _load_example_flows():
    try:
        import examples.qa_agent  # noqa: F401
    except ImportError as e:
        print(f"Warning: could not load qa_agent flow: {e}")

_load_example_flows()