"""Readers for the refusals ``src`` raises, shared by the message guards.

``_error_classes`` splits the ``AppError`` subclasses into those whose first
argument is the message and those that take a label and word the sentence
themselves (``NotFoundError(entity)``); ``_template`` reads a message argument
as literal text with each placeholder written ``{}``. The user-message guards
and the shared-catalogue guard both read raises through them, so the two cannot
disagree about what a message is.
"""

from __future__ import annotations

import ast


def _error_classes(trees: list[ast.Module]) -> tuple[set[str], set[str]]:
    """``(message_classes, label_classes)`` among the AppError subclasses in ``trees``.

    A label class's first argument is not the message - ``NotFoundError(entity)``,
    ``SelfApprovalError(subject)`` - because it words the sentence itself.
    """
    bases: dict[str, list[str]] = {}
    first_parameter: dict[str, str | None] = {}
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases[node.name] = [b.id for b in node.bases if isinstance(b, ast.Name)]
            init = next(
                (
                    item
                    for item in node.body
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__"
                ),
                None,
            )
            if init is not None:
                arguments = init.args.args[1:]
                first_parameter[node.name] = arguments[0].arg if arguments else None
    errors = {"AppError"}
    changed = True
    while changed:
        changed = False
        for name, parents in bases.items():
            if name not in errors and any(parent in errors for parent in parents):
                errors.add(name)
                changed = True

    def takes_label(name: str) -> bool:
        if name in first_parameter:
            return first_parameter[name] not in {"message", None}
        return any(takes_label(parent) for parent in bases.get(name, []) if parent in errors)

    labels = {name for name in errors if takes_label(name)}
    return errors - labels, labels


def _template(node: ast.expr) -> str | None:
    """A message argument with each placeholder written ``{}``; None if not literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) else "{}" for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _template(node.left), _template(node.right)
        return None if left is None or right is None else left + right
    return None


__all__ = ["_error_classes", "_template"]
