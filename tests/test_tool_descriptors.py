"""Pin the public MCP tool descriptors (names, arguments, descriptions).

Moving tool bodies out of server.py must not change what MCP clients see.
Regenerate the fixture only for an intentional API change:

    python tests/test_tool_descriptors.py --update
"""

import asyncio
import json
import sys
from pathlib import Path

import ku_portal_mcp.server as server_module

FIXTURE = Path(__file__).with_name("fixtures") / "tool_descriptors.json"


def current_descriptors() -> dict:
    tools = asyncio.run(server_module.server.list_tools())
    return {
        "instructions": server_module.server._mcp_server.instructions,
        "tools": [tool.model_dump(mode="json", by_alias=True) for tool in tools],
    }


def test_tool_descriptors_match_fixture():
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    actual = json.loads(json.dumps(current_descriptors(), ensure_ascii=False))
    assert [t["name"] for t in actual["tools"]] == [t["name"] for t in expected["tools"]]
    assert actual == expected


if __name__ == "__main__" and "--update" in sys.argv:
    FIXTURE.parent.mkdir(exist_ok=True)
    FIXTURE.write_text(
        json.dumps(current_descriptors(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"updated {FIXTURE}")
