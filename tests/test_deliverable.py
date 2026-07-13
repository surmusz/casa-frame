"""交付物注册表测试。"""
import pytest

from casa.deliverable import DeliverableRegistry, DeliverableSpec, ChapterSpec


@pytest.mark.asyncio
async def test_deliverable_render():
    reg = DeliverableRegistry()
    reg.register(DeliverableSpec(
        deliverable_id="full",
        label="Full Report",
        sources=["analytics", "report"],
        chapters=[ChapterSpec(chapter_id="c1", title="Analytics", source_artifact="analytics")],
    ))
    assert reg.required_artifacts("full") == {"analytics", "report"}
    out = await reg.render("full", {"analytics": {"t": 1}, "report": {"title": "R"}})
    assert out is not None
    assert b"analytics" in out.content


def test_usage_policy_from_deliverable():
    from casa.deliverable import get_deliverable_registry, reset_deliverable_registry, DeliverableSpec
    from casa.orchestration import UsagePolicy

    reset_deliverable_registry()
    get_deliverable_registry().register(DeliverableSpec(
        deliverable_id="insights", label="Insights", sources=["themes"],
    ))
    policy = UsagePolicy.from_deliverable_type("insights")
    assert "themes" in policy.required_artifacts
