from __future__ import annotations

from collections import deque
from copy import deepcopy

import pytest

from hirespec_loop.fixtures import ANSWERS, REQUIREMENT, OfflineDemoClient
from hirespec_loop.prompts import PROMPTS


class ControlledClient(OfflineDemoClient):
    """Test double: script transport/schema/semantic failures by task."""

    def __init__(self, overrides=None):
        super().__init__()
        self.overrides = {key: deque(values) for key, values in (overrides or {}).items()}
        self.observed = []
        self.fail_evaluations = False

    async def generate_json(self, system_prompt, payload, output_schema):
        task = next(name for name, prompt in PROMPTS.items() if prompt == system_prompt)
        self.observed.append((task, deepcopy(payload)))
        if task == "evaluate_criterion" and self.fail_evaluations:
            raise TimeoutError("private token must never be exposed")
        if task in self.overrides and self.overrides[task]:
            response = self.overrides[task].popleft()
            if isinstance(response, Exception):
                raise response
            return deepcopy(response)
        return await super().generate_json(system_prompt, payload, output_schema)


@pytest.fixture
def client():
    return ControlledClient()


SAFE = {"status": "SAFE", "findings": [], "clarification_question": None}


async def draft_for(ai):
    return await ai.draft_spec(REQUIREMENT, ANSWERS)
