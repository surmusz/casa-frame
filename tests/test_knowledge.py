"""知识库测试。"""
from casa.authority import AuthorityResolver, CapabilityMatrix, CapabilityRow
from casa.config import Scope
from casa.knowledge import InMemoryKnowledgeBase, KBEntry, KBRegistry, reset_kb_registry
from casa.scope import DataStore, RefCatalog


def test_inmemory_search():
    kb = InMemoryKnowledgeBase("docs", scope=Scope.GLOBAL)
    kb.put(KBEntry(entry_id="a1", content={"text": "竞品分析报告"}))
    import asyncio
    hits = asyncio.run(kb.search("竞品"))
    assert len(hits) == 1


def test_registry_callback_backward_compat():
    reset_kb_registry()
    reg = KBRegistry()
    reg.register_callbacks(global_kb_reader=lambda path: {"path": path})
    ds = DataStore(agent_id="agent1", kb_registry=reg, kb_read=["global_kb"])
    data = ds.resolve_read("global:knowledge:platform")
    assert data["path"] == "platform"


def test_kb_permission_denied():
    reset_kb_registry()
    reg = KBRegistry()
    reg.register_callbacks(global_kb_reader=lambda p: {"ok": True})
    ds = DataStore(agent_id="a1", kb_registry=reg, kb_read=["other_kb"])
    try:
        ds.resolve_read("global:knowledge:x")
        assert False, "expected error"
    except Exception as exc:
        assert "global_kb_reader" in str(exc) or "Permission" in str(exc) or "未注入" in str(exc)


def test_ref_catalog_dynamic_kb():
    reset_kb_registry()
    reg = KBRegistry()
    kb = InMemoryKnowledgeBase("g1", scope=Scope.GLOBAL)
    kb.put(KBEntry(entry_id="platform", content={"info": True}))
    reg.register(kb)
    cat = RefCatalog.build(session_id="s1", agent_id="a1", kb_registry=reg)
    kinds = {r["kind"] for r in cat.refs}
    assert "global_knowledge" in kinds


def test_authority_kb_read():
    matrix = CapabilityMatrix()
    matrix.register(CapabilityRow(agent_id="a1", kb_read=["global_kb"]))
    resolver = AuthorityResolver(matrix=matrix)
    ok, _ = resolver.check_access("a1", kb_id="global_kb")
    assert ok
    ok2, _ = resolver.check_access("a1", kb_id="user_kb")
    assert not ok2


def test_authority_kb_read_empty_denies():
    matrix = CapabilityMatrix()
    matrix.register(CapabilityRow(agent_id="a1"))
    resolver = AuthorityResolver(matrix=matrix)
    ok, _ = resolver.check_access("a1", kb_id="global_kb")
    assert not ok


def test_kb_read_empty_list_no_access():
    reg = KBRegistry()
    reg.register(InMemoryKnowledgeBase("g1", scope=Scope.GLOBAL))
    assert reg.list_for_agent("a1", kb_read=[]) == []
    assert len(reg.list_for_agent("a1", kb_read=None)) == 1


def test_legacy_user_kb_get():
    from casa.knowledge import _LegacyUserKB

    reg = KBRegistry()
    reg.register_callbacks(user_kb_reader=lambda uid, doc: {"uid": uid, "doc": doc})
    kb = reg.list_for_agent("a1", kb_read=["user_kb"])[0]
    assert isinstance(kb, _LegacyUserKB)
    import asyncio
    entry = asyncio.run(kb.get("u1:doc1"))
    assert entry is not None
    assert entry.content["doc"] == "doc1"
