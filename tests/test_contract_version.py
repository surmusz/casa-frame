"""契约版本化测试 — Phase 2。"""
from casa.contract import (
    Contract, BaseDeliverable, BaseRequired, ContractVersionError,
    register_contract_migrator, CURRENT_CONTRACT_VERSION,
)


def test_to_dict_includes_identity_fields():
    c = Contract(
        deliverable=BaseDeliverable(type="full"),
        required=BaseRequired(mode="test"),
        session_id="s1",
        user_id="u1",
        tenant_id="t1",
    )
    d = c.to_dict()
    assert d["session_id"] == "s1"
    assert d["user_id"] == "u1"
    assert d["tenant_id"] == "t1"
    assert "created_at" in d
    assert "updated_at" in d


def test_from_dict_roundtrip():
    c = Contract(
        deliverable=BaseDeliverable(type="insights"),
        required=BaseRequired(mode="analyze"),
        session_id="s2",
        user_id="u2",
        intent_summary="test intent",
    )
    restored = Contract.from_dict(c.to_dict())
    assert restored.session_id == "s2"
    assert restored.user_id == "u2"
    assert restored.deliverable.type == "insights"
    assert restored.intent_summary == "test intent"


def test_from_dict_defaults_version_to_v1():
    data = {
        "deliverable": {"type": "full"},
        "required": {"mode": "default"},
        "session_id": "s",
        "user_id": "u",
    }
    c = Contract.from_dict(data, validate=False)
    assert c.version == 1


def test_unknown_version_raises():
    def _noop_migrator(data):
        return data

    register_contract_migrator(1, 2, _noop_migrator)
    try:
        Contract.from_dict(
            {"version": 99, "deliverable": {"type": "full"}, "required": {"mode": "x"},
             "session_id": "s", "user_id": "u"},
            validate=False,
        )
        assert False, "should raise"
    except ContractVersionError:
        pass


def test_migrator_chain():
    def v1_to_v2(data):
        data = dict(data)
        data["version"] = 2
        data["migrated"] = True
        return data

    register_contract_migrator(1, 2, v1_to_v2)
    # 当 CURRENT 为 1 时，v2 数据需 v2→current 迁移器；此处仅测 v1 路径
    c = Contract.from_dict({
        "version": 1,
        "deliverable": {"type": "full"},
        "required": {"mode": "m"},
        "session_id": "s",
        "user_id": "u",
    }, validate=False)
    assert c.version == 1
