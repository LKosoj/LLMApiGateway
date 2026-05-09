import asyncio
import threading

import httpx
from fastapi import FastAPI, Request

from llm_gateway_core.db.api_keys_db import ApiKeyRecord
from llm_gateway_core.api.v1.operation_proxy import record_operation_usage
from llm_gateway_core.middleware import auth as auth_middleware
from llm_gateway_core.middleware import chat_logging
from llm_gateway_core.services.access_control import UsdBudgetLedger
from tests._async_compat import run_async


class _FakeApiKeysDB:
    def __init__(self, record: ApiKeyRecord) -> None:
        self._record = record
        self._lock = threading.Lock()
        self.spent_recorded = 0.0

    def get_by_key(self, token: str) -> ApiKeyRecord | None:
        if token != self._record.api_key:
            return None
        return ApiKeyRecord(
            id=self._record.id,
            name=self._record.name,
            api_key=self._record.api_key,
            budget_usd=self._record.budget_usd,
            spent_usd=self._record.spent_usd,
            rpm=self._record.rpm,
            tpm=self._record.tpm,
            allowed_models=list(self._record.allowed_models),
            disabled=self._record.disabled,
            metadata=dict(self._record.metadata),
            created_at=self._record.created_at,
            last_used_at=self._record.last_used_at,
        )

    def record_spent(self, key_id: int, amount: float) -> None:
        assert key_id == self._record.id
        with self._lock:
            self.spent_recorded += amount


class _FakeTokensUsageDB:
    def __init__(self) -> None:
        self.records = []
        self._lock = threading.Lock()

    def insert_usage(self, tokens_usage: dict) -> None:
        with self._lock:
            self.records.append(dict(tokens_usage))


class _FailingTokensUsageDB:
    def insert_usage(self, _tokens_usage: dict) -> None:
        raise RuntimeError("usage db down")


def _build_budget_app(
    *,
    api_keys_db: _FakeApiKeysDB,
    tokens_usage_db: _FakeTokensUsageDB,
    ledger: UsdBudgetLedger,
) -> tuple[FastAPI, threading.Event, dict[str, int]]:
    app = FastAPI()
    app.state.api_keys_db = api_keys_db
    app.state.usd_budget_ledger = ledger
    counters = {"admitted": 0}
    release_successful_requests = threading.Event()

    app.middleware("http")(auth_middleware.api_key_auth)
    app.middleware("http")(chat_logging.log_chat_completions)

    @app.post("/v1/chat/completions")
    async def chat_completions():
        counters["admitted"] += 1
        await asyncio.to_thread(release_successful_requests.wait)
        return {
            "id": "chatcmpl-budget",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"},
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "cost": 5.0,
            },
        }

    return app, release_successful_requests, counters


def _build_operation_budget_app(
    *,
    api_keys_db: _FakeApiKeysDB,
    tokens_usage_db: _FakeTokensUsageDB,
    ledger: UsdBudgetLedger,
) -> tuple[FastAPI, threading.Event, dict[str, int]]:
    app = FastAPI()
    app.state.api_keys_db = api_keys_db
    app.state.tokens_usage_db = tokens_usage_db
    app.state.usd_budget_ledger = ledger
    app.state.rate_limiter = None
    counters = {"admitted": 0}
    release_successful_requests = threading.Event()

    app.middleware("http")(auth_middleware.api_key_auth)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        counters["admitted"] += 1
        await asyncio.to_thread(release_successful_requests.wait)
        payload = {
            "data": [{"embedding": [0.1], "index": 0}],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 0,
                "total_tokens": 1,
                "cost": 5.0,
            },
        }
        await record_operation_usage(
            request,
            payload,
            gateway_model="demo-embedding",
            operation="embeddings",
        )
        return payload

    return app, release_successful_requests, counters


def test_usd_budget_reservation_rejects_concurrent_overspend(monkeypatch):
    record = ApiKeyRecord(
        id=7,
        name="team-budget",
        api_key="lgk_budget",
        budget_usd=10.0,
        spent_usd=0.0,
        rpm=None,
        tpm=None,
        allowed_models=[],
        disabled=False,
        metadata={},
        created_at="",
        last_used_at=None,
    )
    api_keys_db = _FakeApiKeysDB(record)
    tokens_usage_db = _FakeTokensUsageDB()
    ledger = UsdBudgetLedger(default_estimate_usd=5.0)
    app, release_successful_requests, counters = _build_budget_app(
        api_keys_db=api_keys_db,
        tokens_usage_db=tokens_usage_db,
        ledger=ledger,
    )

    monkeypatch.setattr(auth_middleware.settings, "gateway_api_key", "master-key")
    monkeypatch.setattr(chat_logging.settings, "log_chat_messages", False)
    monkeypatch.setattr(chat_logging.state, "tokens_usage_db", tokens_usage_db)
    monkeypatch.setattr(chat_logging.state, "api_keys_db", api_keys_db)
    monkeypatch.setattr(chat_logging.state, "usd_budget_ledger", ledger)
    monkeypatch.setattr(chat_logging.state, "rate_limiter", None)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            tasks = [
                asyncio.create_task(
                    client.post(
                        "/v1/chat/completions",
                        json={"model": "demo", "messages": []},
                        headers={"Authorization": "Bearer lgk_budget"},
                    )
                )
                for _ in range(5)
            ]

            rejected_statuses = []
            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline:
                rejected_statuses = [
                    task.result().status_code
                    for task in tasks
                    if task.done()
                    and task.exception() is None
                    and task.result().status_code in {402, 429}
                ]
                if len(rejected_statuses) == 3:
                    break
                await asyncio.sleep(0.01)

            assert len(rejected_statuses) == 3
            assert counters["admitted"] == 2
            assert ledger.reserved_for(record.id) == 10.0

            release_successful_requests.set()
            responses = await asyncio.gather(*tasks)
            return [response.status_code for response in responses]

    statuses = run_async(scenario())

    assert statuses.count(200) == 2
    assert statuses.count(429) == 3
    assert api_keys_db.spent_recorded == 10.0
    assert sum(1 for record in tokens_usage_db.records if record["cost"] == 5.0) == 2
    assert ledger.reserved_for(record.id) == 0.0


def test_usd_budget_reservation_rejects_concurrent_operation_overspend(monkeypatch):
    record = ApiKeyRecord(
        id=8,
        name="team-operation-budget",
        api_key="lgk_operation_budget",
        budget_usd=10.0,
        spent_usd=0.0,
        rpm=None,
        tpm=None,
        allowed_models=[],
        disabled=False,
        metadata={},
        created_at="",
        last_used_at=None,
    )
    api_keys_db = _FakeApiKeysDB(record)
    tokens_usage_db = _FakeTokensUsageDB()
    ledger = UsdBudgetLedger(default_estimate_usd=5.0)
    app, release_successful_requests, counters = _build_operation_budget_app(
        api_keys_db=api_keys_db,
        tokens_usage_db=tokens_usage_db,
        ledger=ledger,
    )

    monkeypatch.setattr(auth_middleware.settings, "gateway_api_key", "master-key")

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            tasks = [
                asyncio.create_task(
                    client.post(
                        "/v1/embeddings",
                        json={"model": "demo-embedding", "input": "hello"},
                        headers={"Authorization": "Bearer lgk_operation_budget"},
                    )
                )
                for _ in range(5)
            ]

            rejected_statuses = []
            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline:
                rejected_statuses = [
                    task.result().status_code
                    for task in tasks
                    if task.done()
                    and task.exception() is None
                    and task.result().status_code in {402, 429}
                ]
                if len(rejected_statuses) == 3:
                    break
                await asyncio.sleep(0.01)

            assert len(rejected_statuses) == 3
            assert counters["admitted"] == 2
            assert ledger.reserved_for(record.id) == 10.0

            release_successful_requests.set()
            responses = await asyncio.gather(*tasks)
            return [response.status_code for response in responses]

    statuses = run_async(scenario())

    assert statuses.count(200) == 2
    assert statuses.count(429) == 3
    assert api_keys_db.spent_recorded == 10.0
    assert sum(1 for record in tokens_usage_db.records if record["cost"] == 5.0) == 2
    assert ledger.reserved_for(record.id) == 0.0


def test_usd_budget_reservation_covers_budget_accounted_audio_and_pdf_routes():
    assert auth_middleware._path_uses_usd_budget_reservation("/v1/audio/speech")
    assert auth_middleware._path_uses_usd_budget_reservation("/v1/pdf/convert")
    assert auth_middleware._path_uses_usd_budget_reservation("/v1/pdf/jobs")


def test_chat_usage_insert_failure_still_commits_budget_reservation(monkeypatch):
    record = ApiKeyRecord(
        id=9,
        name="team-chat-insert-failure",
        api_key="lgk_chat_insert_failure",
        budget_usd=10.0,
        spent_usd=0.0,
        rpm=None,
        tpm=None,
        allowed_models=[],
        disabled=False,
        metadata={},
        created_at="",
        last_used_at=None,
    )
    api_keys_db = _FakeApiKeysDB(record)
    ledger = UsdBudgetLedger(default_estimate_usd=5.0)
    ledger.sync_record(record.id, budget_usd=record.budget_usd, spent_usd=record.spent_usd)
    assert ledger.reserve(record.id, 5.0)

    monkeypatch.setattr(chat_logging.state, "tokens_usage_db", _FailingTokensUsageDB())
    monkeypatch.setattr(chat_logging.state, "api_keys_db", api_keys_db)
    monkeypatch.setattr(chat_logging.state, "usd_budget_ledger", ledger)
    monkeypatch.setattr(chat_logging.state, "rate_limiter", None)

    chat_logging.record_tokens_usage(
        {
            "prompt_tokens": 2,
            "completion_tokens": 3,
            "cost": 5.0,
            "api_key_id": record.id,
            "_usd_budget_reserved": True,
        }
    )

    assert api_keys_db.spent_recorded == 5.0
    assert ledger.reserved_for(record.id) == 0.0
