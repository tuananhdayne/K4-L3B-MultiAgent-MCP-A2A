import asyncio
from pathlib import Path
from types import SimpleNamespace

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway


class Session:
    async def call_tool(self, name, arguments):
        assert name == "get_order"
        assert arguments == {"case_id": "L3B_CASE_001", "order_id": "order-1"}
        return SimpleNamespace(
            is_error=False,
            structured_content={
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_" + "a" * 20,
                "result_hash": "sha256:" + "b" * 64,
                "domain": "order",
                "data": {"order_id": "order-1"},
            },
            content=[],
        )


def test_gateway_reads_current_mcp_sdk_result_fields():
    root = Path(__file__).resolve().parents[1]
    gateway = EvidenceGateway(Session(), Contracts(root / "contracts" / "schemas"))
    result = asyncio.run(gateway.call("get_order", case_id="L3B_CASE_001", order_id="order-1"))
    assert result["data"]["order_id"] == "order-1"
