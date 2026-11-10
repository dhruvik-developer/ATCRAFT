"""An effect keeps the hooks rule on, or says in this file why it cannot.

``react-hooks/exhaustive-deps`` is the only mechanical check on a hook's
dependency list: it is what notices an effect reading a value it does not
re-run for. 48 of the 52 places that switched it off in ``apps/web/src`` did so
for one effect - "seed the form once per opening" - written several ways, with
the rule for a record still loading implemented incompatibly: some reset to
blank while the read was in flight, some skipped, and the skip left the record
edited last in a dialog that stays mounted, where Save wrote it onto the next
one. The disable on the MRP run's transfer route hid a second defect the same
way. ``hooks/use-form-seed.ts`` is now that effect, once, with the rule on.

So a disable of the rule - ``eslint-disable-next-line``, ``eslint-disable-line``
or a block ``eslint-disable`` naming it - is refused anywhere in
``apps/web/src`` except the uses listed in ``ALLOWED``, each with the reason the
rule cannot be satisfied there. An entry's count is exact, so a fixed site also
shrinks the list.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit.test_frontend_copy_internals import GENERATED

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"
SEED_HOOK = WEB_SRC / "hooks" / "use-form-seed.ts"

_DISABLE = re.compile(
    r"eslint-disable(?:-next-line|-line)?\b[^\n]*?\breact-hooks/exhaustive-deps\b"
)

#: path under apps/web/src -> (how many disables, why the rule cannot hold there)
ALLOWED: dict[str, tuple[int, str]] = {
    "hooks/use-list-query.ts": (
        1,
        "the filters object is typically written inline, so the memo depends on "
        "its serialised form; depending on the object would change the query key "
        "on every render",
    ),
}


def rule_disables(source: str) -> list[int]:
    """The line numbers that switch ``react-hooks/exhaustive-deps`` off."""
    return [source.count("\n", 0, match.start()) + 1 for match in _DISABLE.finditer(source)]


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.ts*")
        if path.suffix in {".ts", ".tsx"} and "node_modules" not in path.parts and path != GENERATED
    )


def test_the_shared_seed_hook_exists() -> None:
    assert SEED_HOOK.is_file(), "hooks/use-form-seed.ts is where a form is seeded per opening"
    assert "export function useFormSeed(" in SEED_HOOK.read_text(encoding="utf-8")
    assert len(_sources()) > 300, "source discovery found too few files"


def test_no_hook_switches_the_dependency_rule_off_without_a_reason() -> None:
    found = {
        path.relative_to(WEB_SRC).as_posix(): lines
        for path in _sources()
        if (lines := rule_disables(path.read_text(encoding="utf-8")))
    }
    unexpected = {
        name: lines for name, lines in found.items() if len(lines) != ALLOWED.get(name, (0, ""))[0]
    }
    stale = sorted(name for name in ALLOWED if name not in found)
    assert not unexpected and not stale, (
        "react-hooks/exhaustive-deps is switched off where this guard does not "
        f"expect it:\n  {unexpected}\nAllowlisted files with no disable left: {stale}\n\n"
        "To seed a form once per opening, call useFormSeed(open, recordId, seed) from "
        "hooks/use-form-seed.ts. Otherwise satisfy the rule, or add the file to "
        "ALLOWED with the number of disables and the reason the rule cannot hold."
    )


def test_every_allowance_gives_a_reason() -> None:
    for name, (count, reason) in ALLOWED.items():
        assert count > 0, name
        assert len(reason.split()) >= 8, f"{name}: say why the rule cannot hold there"


# -- the rule rejects what it exists to catch ---------------------------------


def test_the_rule_finds_every_way_of_switching_it_off() -> None:
    source = "\n".join(
        [
            "  useEffect(() => {",
            "    if (open) reset(brand ? valuesFrom(brand) : emptyBrand())",
            "    // eslint-disable-next-line react-hooks/exhaustive-deps",
            "  }, [open, editingId, reset])",
            "  }, [role.data?.id]) // eslint-disable-line react-hooks/exhaustive-deps",
            "/* eslint-disable react-hooks/exhaustive-deps */",
            "  // eslint-disable-next-line no-console, react-hooks/exhaustive-deps",
        ]
    )
    assert rule_disables(source) == [3, 5, 6, 7]


def test_the_rule_leaves_other_disables_and_mentions_alone() -> None:
    source = "\n".join(
        [
            "  // eslint-disable-next-line react-hooks/rules-of-hooks",
            "  // react-hooks/exhaustive-deps cannot see through the ref, which is fine",
            "  // eslint-disable-next-line no-console",
            "  useFormSeed(open, editingId, () => reset(valuesFrom(brand)))",
        ]
    )
    assert rule_disables(source) == []
