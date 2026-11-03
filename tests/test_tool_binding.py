"""도메인 모듈로 옮긴 도구가 server 전역 이름을 그대로 찾는지 확인한다."""

import builtins
import dis
import types

from ku_portal_mcp.tools import _ORDER, adopt
from ku_portal_mcp.tools.ams import TOOLS as AMS_TOOLS
from ku_portal_mcp.tools.canvas import TOOLS as CANVAS_TOOLS
from ku_portal_mcp.tools.dept import TOOLS as DEPT_TOOLS
from ku_portal_mcp.tools.kupid import TOOLS as KUPID_TOOLS


def _global_names(code: types.CodeType) -> set[str]:
    names = set()
    for instr in dis.get_instructions(code):
        if instr.opname in {"LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"}:
            names.add(instr.argval)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= _global_names(const)
    return names


def test_moved_tools_resolve_every_global_on_the_server_module():
    import ku_portal_mcp.server as server_module

    tools = (*KUPID_TOOLS, *AMS_TOOLS, *DEPT_TOOLS, *CANVAS_TOOLS)
    assert [fn.__name__ for fn in tools]  # imported
    assert len(_ORDER) == 32

    missing = {}
    for fn in tools:
        adopted = adopt(fn, server_module.__dict__)
        unknown = sorted(
            name
            for name in _global_names(adopted.__code__)
            if name not in server_module.__dict__ and name not in builtins.__dict__
        )
        if unknown:
            missing[fn.__name__] = unknown

    assert missing == {}
