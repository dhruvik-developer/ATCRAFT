"""The two developer wrappers, ``Makefile`` and ``make.ps1``, do what they say.

``make.ps1`` runs every step through ``Invoke-Step``, which starts
``powershell.exe -Command <string>`` - Windows PowerShell 5.1 - unless the
command starts with ``npm`` (then ``cmd.exe /c``). Two defects hid in that:

* ``.\\make.ps1 backup`` redirected ``pg_dump -Fc`` with ``>``. That redirect ran
  in PowerShell 5.1, which decodes a native command's output as text and writes
  it back as UTF-16: the archive started ``FF FE 50 00`` instead of ``PGDMP`` and
  pg_restore could not read it. A ``>`` in a string handed to powershell.exe is
  the defect, whatever the command, so it is refused here.
* ``Invoke-Docker`` named its parameter ``$Args``. PowerShell's automatic
  ``$args`` shadows it, so every Docker target ran a bare ``docker`` - which
  prints its usage and exits 0 - and ``dev``, ``up`` and ``down`` all
  "succeeded" without touching a container.

These read the files statically: CI runs on Linux, with no PowerShell 5.1 to
execute them. Every check is fed the defect it exists for.
"""

from __future__ import annotations

import re

import pytest

from src.core.config import REPO_ROOT

pytestmark = pytest.mark.unit

MAKE_PS1 = REPO_ROOT / "make.ps1"
MAKEFILE = REPO_ROOT / "Makefile"

#: PowerShell's automatic variables (PSScriptAnalyzer's
#: PSAvoidAssignmentToAutomaticVariable list). A parameter named after one of
#: them is shadowed by it inside the function.
AUTOMATIC_VARIABLES = frozenset(
    name.lower()
    for name in [
        "_",
        "AllNodes",
        "Args",
        "ConsoleFileName",
        "Error",
        "Event",
        "EventArgs",
        "EventSubscriber",
        "ExecutionContext",
        "false",
        "foreach",
        "Home",
        "Host",
        "Input",
        "LastExitCode",
        "Matches",
        "MyInvocation",
        "NestedPromptLevel",
        "null",
        "PID",
        "PROFILE",
        "PSBoundParameters",
        "PSCmdlet",
        "PSCommandPath",
        "PSCulture",
        "PSDebugContext",
        "PSHome",
        "PSItem",
        "PSScriptRoot",
        "PSSenderInfo",
        "PSUICulture",
        "PSVersionTable",
        "PWD",
        "Sender",
        "ShellId",
        "StackTrace",
        "switch",
        "this",
        "true",
    ]
)

# The backup step as it shipped, and the helper that broke every Docker target.
OLD_BACKUP_STEP = (
    "Invoke-Step \"docker $($DcDev -join ' ') exec -T postgres pg_dump -U atcraft "
    '-Fc atcraft_erp > `"$out`""'
)
OLD_INVOKE_DOCKER = (
    "function Invoke-Docker { param([string[]]$Args) Invoke-Step \"docker $($Args -join ' ')\" }"
)


# --- A small PowerShell reader -----------------------------------------------


def _strip_comments(text: str) -> str:
    """Drop ``<# ... #>`` blocks and whole-line ``#`` comments (make.ps1's styles)."""
    text = re.sub(r"<#.*?#>", "", text, flags=re.S)
    return "\n".join("" if line.lstrip().startswith("#") else line for line in text.splitlines())


def _string_end(text: str, start: int) -> int:
    """Index just past the PowerShell string literal opening at ``start``."""
    quote = text[start]
    index = start + 1
    while index < len(text):
        char = text[index]
        if quote == '"' and char == "`":
            index += 2
            continue
        if char == quote:
            if text[index + 1 : index + 2] == quote:  # a doubled quote escapes itself
                index += 2
                continue
            return index + 1
        index += 1
    raise AssertionError(f"unterminated string at offset {start}")


def _block_end(text: str, start: int) -> int:
    """Index just past the bracket that closes the one opening at ``start``."""
    opening = text[start]
    closing = {"(": ")", "{": "}"}[opening]
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char in "'\"":
            index = _string_end(text, index)
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise AssertionError(f"unbalanced {opening!r} at offset {start}")


def _decode(literal: str) -> str:
    """The text a PowerShell string literal stands for (escapes resolved)."""
    body = literal[1:-1]
    if literal[0] == "'":
        return body.replace("''", "'")
    body = body.replace('""', '"')
    return re.sub(r"`(.)", r"\1", body)


def _has_unquoted_redirect(command: str) -> bool:
    """True when ``command`` holds a ``>`` that PowerShell would read as a redirect."""
    quote: str | None = None
    for char in command:
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == ">":
            return True
    return False


def _cmd_exe_pattern(text: str) -> re.Pattern[str] | None:
    """The rule by which Invoke-Step hands a command to cmd.exe instead."""
    match = re.search(r"\$Command\s+-match\s+'([^']+)'", text)
    return re.compile(match.group(1)) if match else None


def _invoke_step_commands(text: str) -> list[str]:
    """The literal command of every ``Invoke-Step`` call; a non-literal is an error."""
    code = _strip_comments(text)
    commands = []
    for call in re.finditer(r"(?<!function )\bInvoke-Step\b[ \t]+", code):
        start = call.end()
        assert code[start] in "'\"", (
            f"Invoke-Step is handed {code[start : start + 40]!r}: pass a string literal, "
            "so this guard can read the command"
        )
        commands.append(_decode(code[start : _string_end(code, start)]))
    return commands


def _invoke_docker_literals(text: str) -> list[str]:
    """Every string literal inside the argument of an ``Invoke-Docker`` call."""
    code = _strip_comments(text)
    literals = []
    for call in re.finditer(r"(?<!function )\bInvoke-Docker\b\s*\(", code):
        start = call.end() - 1
        argument = code[start : _block_end(code, start)]
        index = 0
        while index < len(argument):
            if argument[index] in "'\"":
                end = _string_end(argument, index)
                literals.append(_decode(argument[index:end]))
                index = end
            else:
                index += 1
    return literals


def powershell_redirects(text: str) -> list[str]:
    """Commands that make.ps1 hands to powershell.exe with a ``>`` redirect in them."""
    to_cmd = _cmd_exe_pattern(text)
    offenders = [
        command
        for command in _invoke_step_commands(text)
        if _has_unquoted_redirect(command) and not (to_cmd and to_cmd.match(command))
    ]
    offenders += [literal for literal in _invoke_docker_literals(text) if ">" in literal]
    return offenders


def shadowed_parameters(text: str) -> list[str]:
    """Parameters named after an automatic variable, which shadows them."""
    code = _strip_comments(text)
    found = []
    for block in re.finditer(r"\bparam\s*\(", code, re.I):
        start = block.end() - 1
        declared = code[start : _block_end(code, start)]
        # A declared name follows its type, the opening bracket or a comma - not
        # the '=' of a default value, which may well read an automatic variable.
        found += [
            name
            for name in re.findall(r"[(,\]]\s*\$(\w+)", declared)
            if name.lower() in AUTOMATIC_VARIABLES
        ]
    return found


def ps_case(name: str, text: str) -> str:
    """The code of make.ps1's ``'name' { ... }`` switch case, nested braces and all."""
    code = _strip_comments(text)
    match = re.search(rf"^\s*'{re.escape(name)}'\s*(?=\{{)", code, re.M)
    assert match, f"make.ps1 has no '{name}' case"
    return code[match.end() : _block_end(code, match.end())]


def make_recipe(name: str, text: str) -> str:
    """The recipe lines of a Makefile target."""
    match = re.search(rf"^{re.escape(name)}:.*\n((?:\t.*\n?)+)", text, re.M)
    assert match, f"Makefile has no {name} target"
    return match.group(1)


# --- The guards ----------------------------------------------------------------


def test_no_step_redirects_through_windows_powershell() -> None:
    text = MAKE_PS1.read_text(encoding="utf-8")
    assert _invoke_step_commands(text), "the reader found no Invoke-Step call at all"

    assert powershell_redirects(text) == []


def test_the_redirect_guard_refuses_the_backup_that_shipped() -> None:
    text = MAKE_PS1.read_text(encoding="utf-8")
    helpers = text.split("switch ($Target)", 1)[0]

    assert powershell_redirects(helpers + "\n" + OLD_BACKUP_STEP + "\n") == [
        "docker $($DcDev -join ' ') exec -T postgres pg_dump -U atcraft -Fc atcraft_erp > \"$out\""
    ]
    # Through Invoke-Docker the same redirect is one more element of the command.
    assert powershell_redirects(
        helpers + "\nInvoke-Docker ($DcDev + @('exec', 'postgres', 'pg_dump', '>', $out))\n"
    ) == [">"]
    # npm goes through cmd.exe, whose redirect is a byte-transparent handle.
    assert powershell_redirects(helpers + "\nInvoke-Step 'npm run build > build.log'\n") == []
    # A quoted '>' is an argument, not a redirect.
    assert powershell_redirects(helpers + "\nInvoke-Step \"git log --format='%h > %s'\"\n") == []


def test_no_parameter_is_shadowed_by_an_automatic_variable() -> None:
    assert shadowed_parameters(MAKE_PS1.read_text(encoding="utf-8")) == []
    assert shadowed_parameters(OLD_INVOKE_DOCKER) == ["Args"]


def test_both_wrappers_dump_the_containers_database_and_prove_the_archive() -> None:
    """Neither wrapper restates the owner or database name; both refuse a bad archive."""
    ps1 = MAKE_PS1.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    backup = ps_case("backup", ps1)
    assert "Get-LocalDatabase" in backup
    assert "Assert-PgDumpArchive" in backup
    assert "pg_restore', '--list'" in backup
    assert "Get-LocalDatabase" in ps_case("shell-db", ps1)
    for case in (backup, ps_case("shell-db", ps1)):
        assert not re.search(r"'atcraft(_erp)?'", case), case

    recipe = make_recipe("backup", makefile)
    assert "pg_restore --list" in recipe
    for target in ("backup", "shell-db"):
        body = make_recipe(target, makefile)
        assert '"$$POSTGRES_USER"' in body, target
        assert '"$$POSTGRES_DB"' in body, target
        assert ":-atcraft" not in body, target


def test_the_parity_check_refuses_a_restated_owner() -> None:
    assert re.search(r"'atcraft(_erp)?'", OLD_BACKUP_STEP + " 'atcraft'")
    old_recipe = "\t$(DC_DEV) exec postgres psql -U $${POSTGRES_USER:-atcraft}\n"
    assert ":-atcraft" in make_recipe("shell-db", "shell-db: ## psql\n" + old_recipe)
