"""Approval endpoints must keep their inbox row coupled to the document workflow."""

from __future__ import annotations

import re
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
APPROVAL_PERMISSION = re.compile(r"require_permission\(Permission\.([A-Z0-9_]*APPROVE)\)")


def test_each_approve_permission_route_module_opens_and_closes_approval_requests() -> None:
    routers = (API_ROOT / "src" / "modules").glob("*/router.py")
    modules = {
        router.parent.name
        for router in routers
        if APPROVAL_PERMISSION.search(router.read_text(encoding="utf-8"))
    }

    assert modules
    for module in sorted(modules):
        service_path = API_ROOT / "src" / "modules" / module / "service.py"
        assert service_path.is_file(), f"{module} approval routes need a service module"
        source = service_path.read_text(encoding="utf-8")
        assert "open_approval(" in source, f"{module} approval routes must open inbox requests"
        assert "close_approval(" in source, f"{module} approval routes must close inbox requests"
