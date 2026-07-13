"""授权 / 能力矩阵测试。"""
from casa.authority import (
    AuthorityResolver,
    CapabilityMatrix,
    CapabilityRow,
    DataGrant,
    InMemoryGrantStore,
    ToolGrant,
)
from casa.orchestration import Stage


def test_capability_matrix_register_and_check():
    matrix = CapabilityMatrix()
    matrix.register(CapabilityRow(
        agent_id="analyst",
        tool_ids=["read_artifact"],
        data_read=["raw_data"],
        data_write="analytics",
    ))
    ok, _ = matrix.check_tool_grant("analyst", "read_artifact")
    assert ok
    ok, _ = matrix.check_data_read_grant("analyst", "raw_data")
    assert ok
    ok, err = matrix.check_data_read_grant("analyst", "secret")
    assert not ok
    assert err


def test_capability_row_model_fields_serialize():
    row = CapabilityRow(
        agent_id="analyst",
        model_preference="claude-sonnet-4",
        context_limit_tokens=200000,
    )
    d = row.to_dict()
    assert d["model_preference"] == "claude-sonnet-4"
    assert d["context_limit_tokens"] == 200000


def test_stage_to_dict_includes_model():
    stage = Stage(
        stage_id="s1",
        agent_id="a1",
        model_preference="gpt-4o-mini",
        context_limit_tokens=64000,
    )
    d = stage.to_dict()
    assert d["model_preference"] == "gpt-4o-mini"
    assert d["context_limit_tokens"] == 64000


def test_delete_all_tools_reverts_to_code_default():
    """删光 tool grant 后撤销 DB 覆盖，回退代码默认。"""
    matrix = CapabilityMatrix()
    matrix.register(CapabilityRow(agent_id="a", tool_ids=["t1"]))
    store = InMemoryGrantStore()
    store.save_tool_grant(ToolGrant(agent_id="a", tool_id="t0", enabled=True))
    store.delete_tool_grant("a", "t0")
    resolver = AuthorityResolver(matrix=matrix, grant_store=store)
    assert not store.has_tool_grant_config("a")
    assert resolver.resolve_tools("a") == ["t1"]


def test_explicit_empty_tools_honored_when_marker_retained():
    """保留配置标记时，显式空 tool 列表优先于代码默认。"""
    matrix = CapabilityMatrix()
    matrix.register(CapabilityRow(agent_id="a", tool_ids=["t1"]))
    store = InMemoryGrantStore()
    store._tool_grants["a"] = {}
    resolver = AuthorityResolver(matrix=matrix, grant_store=store)
    assert store.has_tool_grant_config("a")
    assert resolver.resolve_tools("a") == []


def test_explicit_empty_data_grant_overrides_code():
    """grant_data 行存在时，空 read/write 为显式拒绝。"""
    matrix = CapabilityMatrix()
    matrix.register(CapabilityRow(agent_id="a", data_read=["x"], data_write="y"))
    store = InMemoryGrantStore()
    store.save_data_grant(DataGrant(agent_id="a", read_artifacts=[], write_artifact=""))
    resolver = AuthorityResolver(matrix=matrix, grant_store=store)
    assert store.has_data_grant_config("a")
    assert resolver.resolve_data_grants("a")["read"] == []
    assert resolver.resolve_data_grants("a")["write"] == ""
