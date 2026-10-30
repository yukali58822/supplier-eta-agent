import os

import pytest

os.environ["EXTRACTOR"] = "rule"
from langgraph.checkpoint.memory import InMemorySaver

from eta_agent.demo import make_eml
from eta_agent.erp.fake import FakeERP
from eta_agent.llm.extractor import RuleBasedExtractor
from eta_agent.service import make_agent
from eta_agent.store import Store


class SpyExtractor:
    name = "spy"

    def __init__(self, inner=None):
        self.inner = inner or RuleBasedExtractor()
        self.calls = []

    def extract(self, subject, body, candidates):
        self.calls.append({"subject": subject, "body": body, "candidates": candidates})
        return self.inner.extract(subject, body, candidates)


@pytest.fixture
def erp():
    return FakeERP()


@pytest.fixture
def spy():
    return SpyExtractor()


@pytest.fixture
def agent(erp, spy):
    return make_agent(erp, Store("sqlite://"), spy, InMemorySaver())


@pytest.fixture
def eml():
    return make_eml
