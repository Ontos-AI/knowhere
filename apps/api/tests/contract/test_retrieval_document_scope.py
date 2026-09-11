"""Document boundaries exercised against published PostgreSQL corpus data."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from shared.models.database.document import DocumentChunk, GraphEdge, GraphNode
from shared.services.retrieval.agent_explore.dispatch import dispatch_tool_call
from shared.services.retrieval.agent_explore.budget import EpisodeBudget
from shared.services.retrieval.agent_explore.ref_resolution import resolve_finish_refs
from shared.services.retrieval.agent_explore.types import EpisodeResult
from shared.services.retrieval.cache_service import _cache_shape_digest
from shared.services.retrieval.document_scope import DocumentScope
from shared.services.retrieval.hydration.connected import hydrate_connected_target_rows
from shared.services.retrieval.hydration.result_assembly import (
    assemble_retrieval_results,
)
from tests.contract.test_retrieval_classic_map_unit_contract import _publish_document
from tests.support.retrieval_snapshot_support import contract_db_session


async def _corpus(namespace):
    docs = []
    for label in ("alpha", "beta", "gamma"):
        asset = f"{namespace}-{label}-asset"
        doc = await _publish_document(
            namespace=namespace,
            source_file_name=f"{label}.pdf",
            chunks=[
                {
                    "chunk_id": f"{namespace}-{label}-{index}",
                    "type": "text",
                    "content": f"{'scopeprobe' if index <= 2 else 'unrelated filler'} {label} evidence {index} [images/{label}.png]",
                    "path": f"{label}.pdf/Root/Section{index}/body",
                    "order": index,
                    "metadata": {
                        "connect_to": [
                            {"target": asset, "ref": f"[images/{label}.png]"}
                        ]
                    },
                }
                for index in range(1, 7)
            ]
            + [
                {
                    "chunk_id": asset,
                    "type": "image",
                    "content": f"{label} asset secret",
                    "path": f"images/{label}.png",
                    "order": 3,
                    "metadata": {
                        "summary": f"scopeprobe {label}",
                        "file_path": f"images/{label}.png",
                    },
                }
            ],
        )
        docs.append(doc)
    async with contract_db_session() as db:
        for doc in docs:
            await db.merge(
                GraphNode(
                    node_id=f"doc:{doc['document_id']}",
                    user_id="local-dev-user",
                    namespace=namespace,
                    node_kind="document",
                    owner_document_id=doc["document_id"],
                    job_result_id=doc["job_result_id"],
                    properties={"top_summary": doc["document_id"]},
                )
            )
        await db.flush()
        for left, right in ((0, 1), (1, 2), (2, 0)):
            db.add(
                GraphEdge(
                    edge_id=f"scope-{uuid4().hex}",
                    user_id="local-dev-user",
                    namespace=namespace,
                    edge_kind="related",
                    source_node_id=f"doc:{docs[left]['document_id']}",
                    target_node_id=f"doc:{docs[right]['document_id']}",
                    owner_document_id=docs[left]["document_id"],
                    job_result_id=docs[left]["job_result_id"],
                    weight=1.0,
                )
            )
        await db.commit()
    return docs


def _matrix(ids):
    a, b, c = ids
    return [
        (None, [], {a, b, c}),
        ([], [], set()),
        ([a], [], {a}),
        ([a, b], [b], {a}),
        (None, [b], {a, c}),
        ([a], [a], set()),
        (["missing"], [], set()),
        ([a, a, b], [c], {a, b}),
    ]


async def test_scope_matrix_all_corpus_tools_and_refs(developer_api_client_factory):
    namespace = f"scope-tools-{uuid4().hex[:8]}"
    async with developer_api_client_factory():
        docs = await _corpus(namespace)
        ids = [d["document_id"] for d in docs]
        async with contract_db_session() as db:
            chunks = (
                (
                    await db.execute(
                        select(DocumentChunk).where(DocumentChunk.document_id.in_(ids))
                    )
                )
                .scalars()
                .all()
            )
        refs = [{"document_id": c.document_id, "chunk_id": c.chunk_id} for c in chunks]
        for include, exclude, expected in _matrix(ids):
            scope = DocumentScope(
                None if include is None else frozenset(include), frozenset(exclude)
            )

            async def call(name, args):
                return await dispatch_tool_call(
                    f"corpus.{name}",
                    args,
                    db_factory=contract_db_session,
                    user_id="local-dev-user",
                    namespace=namespace,
                    document_scope=scope,
                )

            for name, args in [
                ("list_documents", {}),
                ("grep", {"pattern": "scopeprobe"}),
                ("grep", {"pattern": "scopeprobe", "document_ids": ids}),
                ("recall", {"query": "scopeprobe", "channels": ["term"], "top_k": 50}),
                (
                    "recall",
                    {"query": "scopeprobe", "channels": ["path_content"], "top_k": 50},
                ),
                ("recall", {"query": "scopeprobe", "document_ids": ids, "top_k": 50}),
                (
                    "node_filter",
                    {
                        "document_ids": ids,
                        "predicates": [{"field": "path", "terms": ["Section"]}],
                    },
                ),
                ("assets", {}),
                (
                    "assets",
                    {
                        "host_of": [
                            c.chunk_id for c in chunks if c.chunk_type == "image"
                        ]
                    },
                ),
                ("read", {"refs": refs, "include_assets": False}),
                ("read", {"refs": refs}),
            ]:
                result = await call(name, args)
                assert {ref["document_id"] for ref in result.refs} == expected, (
                    name,
                    include,
                    exclude,
                    result,
                )
                if expected:
                    assert result.error is None, (name, result.error)
            for doc_id in ids:
                outline = await call("outline", {"document_id": doc_id})
                assert {r["document_id"] for r in outline.refs} == ({doc_id} & expected)
                neighbors = await call("neighbors", {"document_id": doc_id})
                assert {r["document_id"] for r in neighbors.refs} == (
                    expected - {doc_id} if doc_id in expected else set()
                )
            # Explicit tool selectors cannot widen the request boundary.
            if expected:
                outside = next((doc for doc in ids if doc not in expected), None)
                if outside:
                    for name, args in [
                        ("grep", {"pattern": "scopeprobe", "document_ids": [outside]}),
                        ("recall", {"query": "scopeprobe", "document_ids": [outside]}),
                        ("assets", {"document_ids": [outside]}),
                    ]:
                        assert not (await call(name, args)).refs
            async with contract_db_session() as db:
                final_refs = await resolve_finish_refs(
                    db,
                    user_id="local-dev-user",
                    namespace=namespace,
                    refs=refs,
                    document_scope=scope,
                )
                assert {r["document_id"] for r in final_refs} == expected
                padded_refs = await resolve_finish_refs(
                    db,
                    user_id="local-dev-user",
                    namespace=namespace,
                    refs=[
                        {
                            "document_id": f" {r['document_id']} ",
                            "chunk_id": r["chunk_id"],
                        }
                        for r in refs
                    ],
                    document_scope=scope,
                )
                assert padded_refs == final_refs


@pytest.mark.parametrize("version", ["v1", "v2"])
async def test_api_scope_matrix_small_classic_and_cache(
    developer_api_client_factory, version
):
    namespace = f"scope-api-{uuid4().hex[:8]}"
    async with developer_api_client_factory() as client:
        docs = await _corpus(namespace)
        ids = [d["document_id"] for d in docs]
        for top_k in (50, 1):
            for include, exclude, expected in _matrix(ids):
                response = await client.post(
                    f"/api/{version}/retrieval/query",
                    json={
                        "namespace": namespace,
                        "query": "scopeprobe",
                        "top_k": top_k,
                        "use_agentic": False,
                        "include_document_ids": include,
                        "exclude_document_ids": exclude,
                    },
                )
                assert response.status_code == 200, response.text
                body = response.json()
                returned = {row["source"]["document_id"] for row in body["results"]}
                assert returned <= expected
                assert bool(returned) == bool(expected)
                if top_k == 50:
                    assert returned == expected
                if top_k == 1 and expected:
                    assert body["router_used"] == "classic_topk"
                else:
                    assert body["router_used"] == "small_corpus_all"


async def test_agent_scope_survives_dispatch_and_untrusted_finish(
    developer_api_client_factory, monkeypatch
):
    namespace = f"scope-agent-{uuid4().hex[:8]}"
    async with developer_api_client_factory() as client:
        docs = await _corpus(namespace)
        ids = [d["document_id"] for d in docs]
        for include, exclude in [
            (None, [ids[1], ids[2]]),
            ([ids[0], ids[1]], [ids[1]]),
        ]:
            calls = []

            class Harness:
                async def run_episode(
                    self, *, db_factory, user_id, namespace, document_scope, **kwargs
                ):
                    result = await dispatch_tool_call(
                        "corpus.list_documents",
                        {},
                        db_factory=db_factory,
                        user_id=user_id,
                        namespace=namespace,
                        document_scope=document_scope,
                    )
                    calls.append(result)
                    assert {r["document_id"] for r in result.refs} == {ids[0]}
                    return EpisodeResult(
                        refs=[
                            {
                                "document_id": doc,
                                "section_path": "Root / Section1 / body",
                            }
                            for doc in ids
                        ]
                        + [
                            {
                                "document_id": f" {ids[2]} ",
                                "chunk_id": f"{namespace}-gamma-1",
                            },
                            {
                                "document_id": f" {ids[0]} ",
                                "chunk_id": f"{namespace}-alpha-1",
                            },
                        ],
                        notes="",
                    )

            monkeypatch.setattr(
                "shared.services.retrieval.agent_explore.harness.resolve_harness",
                lambda: Harness(),
            )
            response = await client.post(
                "/api/v1/retrieval/query",
                json={
                    "namespace": namespace,
                    "query": "scopeprobe",
                    "top_k": 1,
                    "include_document_ids": include,
                    "exclude_document_ids": exclude,
                },
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert calls
            assert body["router_used"] == "agent_explore"
            assert {r["document_id"] for r in body["referenced_chunks"]} == {ids[0]}
            assert {r["source"]["document_id"] for r in body["results"]} == {ids[0]}
            assert (
                "beta" not in body["evidence_text"]
                and "gamma" not in body["evidence_text"]
            )


async def test_connected_hydration_and_assembly_reject_outside_rows(
    developer_api_client_factory,
):
    namespace = f"scope-assets-{uuid4().hex[:8]}"
    async with developer_api_client_factory():
        docs = await _corpus(namespace)
        ids = [d["document_id"] for d in docs]
        async with contract_db_session() as db:
            chunks = (
                (
                    await db.execute(
                        select(DocumentChunk).where(DocumentChunk.document_id.in_(ids))
                    )
                )
                .scalars()
                .all()
            )
            rows = [
                {
                    "document_id": c.document_id,
                    "job_result_id": c.job_result_id,
                    "chunk_id": c.chunk_id,
                    "chunk_type": c.chunk_type,
                    "content": c.content,
                    "chunk_metadata": c.chunk_metadata,
                }
                for c in chunks
                if c.chunk_type == "text"
            ]
            for include, exclude, expected in _matrix(ids):
                scope = DocumentScope(
                    None if include is None else frozenset(include), frozenset(exclude)
                )
                hydrated = await hydrate_connected_target_rows(
                    db=db,
                    rows=rows,
                    exclude_document_ids=[],
                    exclude_sections=[],
                    document_scope=scope,
                )
                assert {r["document_id"] for r in hydrated} == expected
                legacy_filtered = await hydrate_connected_target_rows(
                    db=db,
                    rows=rows,
                    exclude_document_ids=[ids[0]],
                    exclude_sections=[],
                    document_scope=scope,
                )
                assert {r["document_id"] for r in legacy_filtered} == expected - {
                    ids[0]
                }
                assembled = await assemble_retrieval_results(
                    db=db,
                    rows=rows,
                    exclude_document_ids=[],
                    exclude_sections=[],
                    document_scope=scope,
                )
                assert {r["document_id"] for r in assembled} == expected
                for row in assembled:
                    assert "asset secret" in row["content"]


def test_cache_scope_none_empty_and_set_identity():
    def digest(include, exclude=()):
        return _cache_shape_digest(
            query="same",
            top_k=10,
            exclude_document_ids=list(exclude),
            exclude_sections=[],
            include_document_ids=include,
        )

    assert (
        len(
            {
                digest(None),
                digest([]),
                digest(["a"]),
                digest(["b"]),
                digest(["a"], ["a"]),
            }
        )
        == 5
    )
    assert digest(["b", "a", "a"]) == digest(["a", "b"])


async def test_legacy_excludes_only_narrow_scope_in_database(
    developer_api_client_factory,
):
    from dataclasses import replace
    from shared.services.retrieval.execution.query_request import RetrievalQuery
    from shared.services.retrieval.search.map_unit_discovery import map_unit_discovery
    from shared.services.retrieval.search.scoped_corpus import (
        count_scoped_chunks,
        load_all_scoped_chunks,
    )

    namespace = f"scope-legacy-{uuid4().hex[:8]}"
    async with developer_api_client_factory():
        docs = await _corpus(namespace)
        ids = [d["document_id"] for d in docs]
        scope = DocumentScope(frozenset(ids), frozenset([ids[2]]))
        async with contract_db_session() as db:
            context = RetrievalQuery.from_parameters(
                db=db,
                user_id="local-dev-user",
                namespace=namespace,
                query="scopeprobe",
                top_k=1,
                exclude_document_ids=[ids[1]],
                exclude_sections=[],
            ).build_route_context()
            context = replace(context, document_scope=scope)
            assert [doc for doc in ids if context.document_scope.allows(doc)] == ids[:1]
            kwargs: dict[str, Any] = dict(
                user_id="local-dev-user",
                namespace=namespace,
                exclude_document_ids=[ids[1]],
                document_scope=scope,
            )
            assert (
                await count_scoped_chunks(db, **kwargs, allowed_chunk_types=None) == 7
            )
            rows = await load_all_scoped_chunks(
                db,
                **kwargs,
                exclude_sections=[],
                allowed_chunk_types=None,
                signal_paths=[],
                filter_mode="delete",
            )
            assert {row["document_id"] for row in rows} == {ids[0]}
            discovery = await map_unit_discovery(
                db,
                **kwargs,
                query="scopeprobe",
                top_k=20,
                exclude_sections=[],
            )
            assert {row["document_id"] for row in discovery.payload["fused_rows"]} == {
                ids[0]
            }


@pytest.mark.parametrize("provider", ["openai", "cursor"])
@pytest.mark.parametrize("boundary", ["unscoped", "exclude", "include", "empty"])
async def test_real_harness_provider_loop_scopes_postgresql_tools(
    developer_api_client_factory,
    monkeypatch,
    provider,
    boundary,
):
    from shared.services.retrieval.agent_explore.harness import (
        cursor_harness,
        openai_harness,
    )

    namespace = f"scope-provider-{uuid4().hex[:8]}"
    async with developer_api_client_factory():
        docs = await _corpus(namespace)
        ids = [doc["document_id"] for doc in docs]
        scope = {
            "unscoped": DocumentScope(),
            "exclude": DocumentScope(exclude=frozenset(ids[1:])),
            "include": DocumentScope(frozenset(ids[:2]), frozenset([ids[1]])),
            "empty": DocumentScope(frozenset()),
        }[boundary]
        expected = {doc for doc in ids if scope.allows(doc)}
        observed = []

        def verify_observation(content):
            observed.append(content)
            for doc in ids:
                assert (doc in content) == (doc in expected), (boundary, content)

        tool_calls = [
            SimpleNamespace(
                id="list",
                function=SimpleNamespace(name="corpus_list_documents", arguments="{}"),
            ),
            SimpleNamespace(
                id="grep",
                function=SimpleNamespace(
                    name="corpus_grep",
                    arguments=json.dumps(
                        {"pattern": "scopeprobe", "document_ids": ids}
                    ),
                ),
            ),
        ]

        class CompletionClient:
            def chat_completion_raw_with_usage(self, *, messages, **kwargs):
                tool_messages = [m for m in messages if m["role"] == "tool"]
                if tool_messages:
                    for message in tool_messages:
                        verify_observation(message["content"])
                    calls = [
                        SimpleNamespace(
                            id="finish",
                            function=SimpleNamespace(
                                name="finish", arguments='{"refs": []}'
                            ),
                        )
                    ]
                else:
                    calls = tool_calls
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(tool_calls=calls, content="")
                        )
                    ]
                ), {"total_tokens": 1}

        @asynccontextmanager
        async def context(value):
            yield value

        class Run:
            def __init__(self, tools):
                self.tools = tools

            async def wait(self):
                # Cursor invokes callbacks on provider threads; both callbacks
                # must cross run_coroutine_threadsafe and open their own DB session.
                contents = await asyncio.gather(
                    *[
                        asyncio.to_thread(
                            self.tools[call.function.name].execute,
                            json.loads(call.function.arguments),
                            None,
                        )
                        for call in tool_calls
                    ]
                )
                for content in contents:
                    verify_observation(content)
                self.tools["finish"].execute({"refs": []}, None)
                return SimpleNamespace(usage=SimpleNamespace(total_tokens=1))

        class Agent:
            def __init__(self, tools):
                self.tools = tools

            async def send(self, prompt):
                return Run(self.tools)

        class Agents:
            async def create(self, options):
                return context(Agent(options.local.custom_tools))

        class Client:
            @staticmethod
            async def launch_bridge(**kwargs):
                return context(SimpleNamespace(agents=Agents()))

        if provider == "openai":
            monkeypatch.setattr(
                openai_harness,
                "_resolve_client_and_model",
                lambda: (CompletionClient(), "test"),
            )
            harness = openai_harness.OpenAIHarness()
        else:
            monkeypatch.setenv("CURSOR_API_KEY", "test-provider-no-network")
            monkeypatch.setattr(
                cursor_harness,
                "_require_cursor_sdk",
                lambda: SimpleNamespace(
                    CustomTool=SimpleNamespace,
                    AgentOptions=SimpleNamespace,
                    LocalAgentOptions=SimpleNamespace,
                    AsyncClient=Client,
                ),
            )
            harness = cursor_harness.CursorHarness()
        episode = await harness.run_episode(
            db_factory=contract_db_session,
            user_id="local-dev-user",
            namespace=namespace,
            document_scope=scope,
            query="scopeprobe",
            budget=EpisodeBudget(),
        )
        assert len(observed) == 2
        assert episode.stop_reason == "finished"
        assert all(step.error is None for step in episode.steps)
