"""
Reference example for the eval harness: a single-agent support flow with local
stub tools (lookup_order, get_policy, initiate_refund, update_ticket) whose state
lives in a JSON file in the session dir. Suite: evals/suites/support_refunds.yaml.
"""
from examples.support_agent import agents  # noqa: F401 — triggers register_flow()
