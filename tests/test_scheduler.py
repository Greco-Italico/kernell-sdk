import pytest
import asyncio
from kernell_sdk.runtime.scheduler import Scheduler, WorkerState

class DummyMetrics:
    def inc(self, *args, **kwargs): pass
    def observe(self, *args, **kwargs): pass

class DummyClient:
    def __init__(self, url):
        self.url = url

    async def execute(self, code):
        return {"stdout": "ok"}

    async def health(self):
        return {"latency_ms": 10}

def client_factory(url):
    return DummyClient(url)

class DummyNSJail:
    async def execute(self, code):
        return {"stdout": "fallback"}

@pytest.mark.asyncio
async def test_pick_worker():
    workers = [WorkerState("w1", "http://a"), WorkerState("w2", "http://b")]
    scheduler = Scheduler(workers, DummyMetrics())

    w = await scheduler.pick_worker()
    assert w is not None

@pytest.mark.asyncio
async def test_execute_success():
    workers = [WorkerState("w1", "http://a")]
    scheduler = Scheduler(workers, DummyMetrics())

    result = await scheduler.execute("print(1)", client_factory, DummyNSJail())
    assert result["stdout"] == "ok"

@pytest.mark.asyncio
async def test_execute_fallback():
    class FailingClient:
        def __init__(self, url): pass
        async def execute(self, code): raise Exception("fail")
        async def health(self): return {"latency_ms": 10}

    def failing_factory(url):
        return FailingClient(url)

    workers = [WorkerState("w1", "http://a")]
    scheduler = Scheduler(workers, DummyMetrics())

    result = await scheduler.execute("print(1)", failing_factory, DummyNSJail())
    assert result["stdout"] == "fallback"

@pytest.mark.asyncio
async def test_eviction():
    w = WorkerState("w1", "http://a")
    w.error_rate = 1.0

    scheduler = Scheduler([w], DummyMetrics())
    assert scheduler.should_evict(w) is True
