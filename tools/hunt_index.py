"""Local-only Ruby/Rails audit indexes for ``python -m tools.hunt``.

This module deliberately does not extend Veripsa's product graph.  It reads the
same guarded Ruby source set, parses each selected Ruby file once, and emits
evidence-bounded indexes for:

* Rails routes
* authorization call sites
* Sidekiq enqueue sites and workers
* statically resolvable execution paths

The implementation is intentionally conservative.  A receiver, route target,
worker, or service method is connected only when the source text identifies one
unique local definition.  Dynamic and ambiguous shapes stay unresolved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable


_AUTH_CALLS = frozenset(
    {
        "authorize",
        "authorize!",
        "can?",
        "cannot?",
        "allowed?",
        "denied?",
        "policy",
        "pundit_authorize",
    }
)
_ENQUEUE_METHODS = frozenset(
    {"perform_async", "perform_in", "perform_at", "push", "push_bulk", "delay"}
)
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
_TRANSPARENT_ROUTE_BLOCKS = frozenset({"draw"})
_UNSUPPORTED_ROUTE_BLOCKS = frozenset(
    {"constraints", "concern", "concerns", "controller", "defaults", "mount"}
)
_WORKER_MIXINS = frozenset(
    {"Sidekiq::Worker", "ApplicationWorker", "Gitlab::SidekiqMiddleware"}
)
_SERVICE_TERMINALS = frozenset({"call", "execute", "run", "start", "schedule"})
_MAX_RAW_EXPRESSION = 320
_MAX_EXECUTION_PATHS = 5_000
HUNT_INDEX_EXTRACTOR_VERSION = "hunt-ruby-rails-v1"


@dataclass(frozen=True)
class WarningRecord:
    file: str | None
    line: int | None
    category: str
    reason: str
    severity: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "category": self.category,
            "reason": self.reason,
            "severity": self.severity,
        }


@dataclass
class RubyFile:
    path: Path
    relative_path: str
    source: bytes
    root: Any
    selected: bool


@dataclass(frozen=True)
class MethodKey:
    class_name: str
    method_name: str
    singleton: bool

    def label(self) -> str:
        separator = "." if self.singleton else "#"
        return f"{self.class_name}{separator}{self.method_name}"


@dataclass
class MethodDefinition:
    key: MethodKey
    file: str
    line: int
    parameters: list[str]
    source_file: RubyFile
    body_node: Any


@dataclass
class ClassOccurrence:
    name: str
    kind: str
    file: str
    line: int
    superclass: str | None
    source_file: RubyFile
    node: Any


@dataclass
class CallSite:
    file: str
    line: int
    column: int
    enclosing_class: str | None
    lexical_scopes: tuple[str, ...]
    enclosing_method: str | None
    method_key: MethodKey | None
    receiver: str | None
    receiver_bounded: bool
    receiver_node: Any | None
    call_name: str
    argument_nodes: list[Any]
    node: Any
    source_file: RubyFile
    class_level: bool


@dataclass(frozen=True)
class ServiceCall:
    source_method: MethodKey
    file: str
    line: int
    column: int
    receiver_class: str
    receiver_absolute: bool
    lexical_scopes: tuple[str, ...]
    method_name: str
    singleton: bool
    evidence: str


@dataclass(frozen=True)
class ConstantReference:
    name: str
    absolute: bool


@dataclass
class RouteContext:
    path_prefix: str = ""
    controller_modules: tuple[str, ...] = ()
    namespace_stack: tuple[str, ...] = ()
    resource: "ResourceContext | None" = None
    route_scope: str | None = None


@dataclass
class ResourceContext:
    name: str
    singular: bool
    base_path: str
    controller_class: str

    @property
    def param_name(self) -> str:
        return _singularize(self.name)

    @property
    def member_path(self) -> str:
        if self.singular:
            return self.base_path
        return _join_path(self.base_path, ":id")

    @property
    def nested_prefix(self) -> str:
        if self.singular:
            return self.base_path
        return _join_path(self.base_path, f":{self.param_name}_id")


@dataclass
class Inventory:
    files: list[RubyFile] = field(default_factory=list)
    classes: list[ClassOccurrence] = field(default_factory=list)
    methods: list[MethodDefinition] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)
    warnings: list[WarningRecord] = field(default_factory=list)
    resolution_blockers: list[str] = field(default_factory=list)


def _text(ruby_file: RubyFile, node: Any | None) -> str | None:
    if node is None:
        return None
    return ruby_file.source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _line(node: Any) -> int:
    return int(node.start_point.row) + 1


def _column(node: Any) -> int:
    return int(node.start_point.column) + 1


def _bounded_expression(ruby_file: RubyFile, node: Any) -> tuple[str | None, bool]:
    raw = (_text(ruby_file, node) or "").strip()
    if len(raw) > _MAX_RAW_EXPRESSION or "\x00" in raw:
        return None, False
    return raw, True


def _field(node: Any, name: str) -> Any | None:
    try:
        return node.child_by_field_name(name)
    except Exception:
        return None


def _method_name(ruby_file: RubyFile, node: Any) -> str | None:
    method = _field(node, "method")
    if method is None:
        return None
    raw, bounded = _bounded_expression(ruby_file, method)
    return raw if bounded and raw else None


def _argument_nodes(node: Any) -> list[Any]:
    arguments = _field(node, "arguments")
    if arguments is None:
        return []
    return list(arguments.named_children)


def _pair_parts(node: Any, ruby_file: RubyFile) -> tuple[str | None, Any | None]:
    if node.type != "pair":
        return None, None
    key_node = _field(node, "key")
    value_node = _field(node, "value")
    key = (_text(ruby_file, key_node) or "").strip()
    if key.startswith(":"):
        key = key[1:]
    if (key.startswith('"') and key.endswith('"')) or (
        key.startswith("'") and key.endswith("'")
    ):
        key = key[1:-1]
    return key or None, value_node


def _option_pairs(argument_nodes: Iterable[Any], ruby_file: RubyFile) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def add_pairs(node: Any) -> None:
        if node.type == "pair":
            key, value = _pair_parts(node, ruby_file)
            if key and key not in result:
                result[key] = value
            return
        if node.type in {"hash", "bare_assoc_hash"}:
            for child in node.named_children:
                add_pairs(child)

    for argument in argument_nodes:
        add_pairs(argument)
    return result


def _positional_arguments(argument_nodes: Iterable[Any]) -> list[Any]:
    return [
        node
        for node in argument_nodes
        if node.type not in {"pair", "hash", "bare_assoc_hash"}
    ]


def _static_literal(ruby_file: RubyFile, node: Any | None) -> str | None:
    if node is None:
        return None
    raw, bounded = _bounded_expression(ruby_file, node)
    if not bounded or raw is None:
        return None
    if node.type in {"simple_symbol", "bare_symbol", "hash_key_symbol"}:
        return raw[1:] if raw.startswith(":") else raw
    if node.type == "string":
        if "#{" in raw or len(raw) < 2 or raw[0] not in {"'", '"'} or raw[-1] != raw[0]:
            return None
        return raw[1:-1]
    return None


def _static_array(ruby_file: RubyFile, node: Any | None) -> list[str] | None:
    if node is None or node.type not in {"array", "symbol_array"}:
        return None
    values: list[str] = []
    for child in node.named_children:
        value = _static_literal(ruby_file, child)
        if value is None:
            return None
        values.append(value)
    return values


def _static_constant_reference(
    ruby_file: RubyFile,
    node: Any | None,
) -> ConstantReference | None:
    if node is None or node.type not in {"constant", "scope_resolution"}:
        return None
    raw, bounded = _bounded_expression(ruby_file, node)
    if not bounded or raw is None:
        return None
    absolute = raw.startswith("::")
    normalized = raw[2:] if absolute else raw
    if re.fullmatch(r"[A-Z]\w*(?:::[A-Z]\w*)*", normalized):
        return ConstantReference(normalized, absolute)
    return None


def _static_constant(ruby_file: RubyFile, node: Any | None) -> str | None:
    reference = _static_constant_reference(ruby_file, node)
    return reference.name if reference else None


def _constant_lookup_names(
    reference: ConstantReference,
    lexical_scopes: tuple[str, ...],
) -> list[str]:
    if reference.absolute:
        return [reference.name]
    candidates = [
        f"{scope}::{reference.name}"
        for scope in lexical_scopes
    ]
    candidates.append(reference.name)
    return list(dict.fromkeys(candidates))


def _static_resource_expression(ruby_file: RubyFile, node: Any | None) -> str | None:
    if node is None:
        return None
    if node.type not in {
        "identifier",
        "constant",
        "scope_resolution",
        "instance_variable",
        "class_variable",
        "global_variable",
        "self",
    }:
        return None
    raw, ok = _bounded_expression(ruby_file, node)
    return raw if ok else None


def _matches_pattern(relative_path: str, pattern: str) -> bool:
    normalized = pattern.replace(os.sep, "/")
    if fnmatch.fnmatchcase(relative_path, normalized):
        return True
    if normalized.startswith("**/"):
        return fnmatch.fnmatchcase(relative_path, normalized[3:])
    return False


def _selected(
    relative_path: str,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
) -> bool:
    if include_patterns and not any(
        _matches_pattern(relative_path, pattern) for pattern in include_patterns
    ):
        return False
    return not any(_matches_pattern(relative_path, pattern) for pattern in exclude_patterns)


def _first_error_node(root: Any) -> Any | None:
    queue = [root]
    while queue:
        node = queue.pop(0)
        if node.type == "ERROR" or getattr(node, "is_missing", False):
            return node
        queue[0:0] = list(node.children)
    return None


def _qualified_name(
    raw_name: str,
    enclosing_name: str | None,
) -> str:
    absolute = raw_name.startswith("::")
    normalized = raw_name[2:] if absolute else raw_name
    if absolute or not enclosing_name:
        return normalized
    return f"{enclosing_name}::{normalized}"


def _parameters(ruby_file: RubyFile, node: Any | None) -> list[str]:
    if node is None:
        return []
    values: list[str] = []
    for child in node.named_children:
        raw, ok = _bounded_expression(ruby_file, child)
        values.append(raw if ok and raw is not None else "<unavailable>")
    return values


def _collect_inventory(
    target: Path,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
) -> Inventory:
    # Local import avoids pulling extractor machinery into callers that only
    # inspect schemas, while reusing the product's ABI-safe loader and file
    # safety guards.
    import code_graph_extract as graph_extract
    from tree_sitter import Parser

    inventory = Inventory()
    ruby_language = graph_extract._ts_languages().get("ruby")
    if ruby_language is None:
        inventory.resolution_blockers.append("<ruby grammar unavailable>")
        inventory.warnings.append(
            WarningRecord(
                None,
                None,
                "ruby_grammar_unavailable",
                "tree-sitter Ruby grammar is unavailable; Ruby audit indexes cannot be built",
                "error",
            )
        )
        return inventory

    parser = Parser(ruby_language)
    guarded = list(graph_extract._iter_source_files(str(target)))
    guarded_ruby: dict[str, tuple[Path, bool]] = {}
    for absolute, extension in guarded:
        if extension != ".rb":
            continue
        relative = os.path.relpath(absolute, target).replace(os.sep, "/")
        guarded_ruby[relative] = (
            Path(absolute),
            _selected(relative, include_patterns, exclude_patterns),
        )

    for relative in sorted(guarded_ruby):
        absolute, selected = guarded_ruby[relative]
        try:
            source = absolute.read_bytes()
        except OSError as exc:
            inventory.resolution_blockers.append(relative)
            inventory.warnings.append(
                WarningRecord(
                    relative,
                    None,
                    "ruby_read_failed",
                    f"unable to read selected Ruby file: {type(exc).__name__}",
                    "warning",
                )
            )
            continue
        try:
            root = parser.parse(source).root_node
        except Exception as exc:
            inventory.resolution_blockers.append(relative)
            inventory.warnings.append(
                WarningRecord(
                    relative,
                    None,
                    "ruby_parse_failed",
                    f"tree-sitter failed to parse selected Ruby file: {type(exc).__name__}",
                    "warning",
                )
            )
            continue
        has_error = bool(getattr(root, "has_error", False))
        error = _first_error_node(root) if has_error else None
        if has_error:
            inventory.resolution_blockers.append(relative)
            inventory.warnings.append(
                WarningRecord(
                    relative,
                    _line(error) if error is not None else None,
                    "ruby_parse_error",
                    "Ruby file contains an ERROR or missing syntax node; no audit relationships were confirmed from it",
                    "warning",
                )
            )
            continue
        inventory.files.append(
            RubyFile(absolute, relative, source, root, selected)
        )

    for ruby_file in inventory.files:
        _visit_inventory_node(
            inventory,
            ruby_file,
            ruby_file.root,
            None,
            (),
            None,
        )
    return inventory


def _visit_inventory_node(
    inventory: Inventory,
    ruby_file: RubyFile,
    node: Any,
    enclosing_name: str | None,
    lexical_scopes: tuple[str, ...],
    method_key: MethodKey | None,
) -> None:
    if node.type in {"class", "module"}:
        name_node = _field(node, "name")
        raw_name = (_text(ruby_file, name_node) or "").strip()
        if not raw_name or len(raw_name) > _MAX_RAW_EXPRESSION:
            if raw_name:
                inventory.warnings.append(
                    WarningRecord(
                        ruby_file.relative_path,
                        _line(node),
                        "ruby_definition_unresolved",
                        "class/module name exceeds the audit expression limit",
                        "info",
                    )
                )
            return
        qualified = _qualified_name(raw_name, enclosing_name)
        superclass_node = _field(node, "superclass")
        superclass = (_text(ruby_file, superclass_node) or "").strip()
        if superclass.startswith("<"):
            superclass = superclass[1:].strip()
        inventory.classes.append(
            ClassOccurrence(
                name=qualified,
                kind=node.type,
                file=ruby_file.relative_path,
                line=_line(node),
                superclass=superclass or None,
                source_file=ruby_file,
                node=node,
            )
        )
        body = _field(node, "body")
        if body is not None:
            child_lexical_scopes = (qualified, *lexical_scopes)
            for child in body.named_children:
                _visit_inventory_node(
                    inventory,
                    ruby_file,
                    child,
                    qualified,
                    child_lexical_scopes,
                    None,
                )
        return

    if node.type in {"method", "singleton_method"}:
        if not enclosing_name:
            # Top-level Ruby methods are not controller/service/worker methods.
            return
        name_node = _field(node, "name")
        method_name = (_text(ruby_file, name_node) or "").strip()
        if not method_name or len(method_name) > _MAX_RAW_EXPRESSION:
            return
        singleton = node.type == "singleton_method"
        if singleton:
            object_node = _field(node, "object")
            object_text = (_text(ruby_file, object_node) or "").strip()
            if object_text != "self":
                # A method defined on some other object is not a local class method.
                return
        key = MethodKey(enclosing_name, method_name, singleton)
        definition = MethodDefinition(
            key=key,
            file=ruby_file.relative_path,
            line=_line(node),
            parameters=_parameters(ruby_file, _field(node, "parameters")),
            source_file=ruby_file,
            body_node=_field(node, "body"),
        )
        inventory.methods.append(definition)
        if definition.body_node is not None:
            for child in definition.body_node.named_children:
                _visit_inventory_node(
                    inventory,
                    ruby_file,
                    child,
                    enclosing_name,
                    lexical_scopes,
                    key,
                )
        return

    if node.type == "call":
        name = _method_name(ruby_file, node)
        if name:
            receiver_node = _field(node, "receiver")
            if receiver_node is None:
                receiver, receiver_bounded = None, True
            else:
                receiver, receiver_bounded = _bounded_expression(
                    ruby_file, receiver_node
                )
            inventory.calls.append(
                CallSite(
                    file=ruby_file.relative_path,
                    line=_line(node),
                    column=_column(node),
                    enclosing_class=enclosing_name,
                    lexical_scopes=lexical_scopes,
                    enclosing_method=method_key.method_name if method_key else None,
                    method_key=method_key,
                    receiver=receiver,
                    receiver_bounded=receiver_bounded,
                    receiver_node=receiver_node,
                    call_name=name,
                    argument_nodes=_argument_nodes(node),
                    node=node,
                    source_file=ruby_file,
                    class_level=bool(enclosing_name and method_key is None),
                )
            )

    for child in node.named_children:
        _visit_inventory_node(
            inventory,
            ruby_file,
            child,
            enclosing_name,
            lexical_scopes,
            method_key,
        )


def _method_map(inventory: Inventory) -> dict[MethodKey, list[MethodDefinition]]:
    result: dict[MethodKey, list[MethodDefinition]] = {}
    for definition in inventory.methods:
        result.setdefault(definition.key, []).append(definition)
    return result


def _class_map(inventory: Inventory) -> dict[str, list[ClassOccurrence]]:
    result: dict[str, list[ClassOccurrence]] = {}
    for occurrence in inventory.classes:
        result.setdefault(occurrence.name, []).append(occurrence)
    return result


def _auth_inference(call: CallSite) -> tuple[str | None, str | None, str, str | None]:
    args = call.argument_nodes
    ruby_file = call.source_file
    receiver = call.receiver
    name = call.call_name

    def action_at(index: int) -> str | None:
        return _static_literal(ruby_file, args[index]) if index < len(args) else None

    def resource_at(index: int) -> str | None:
        return _static_resource_expression(ruby_file, args[index]) if index < len(args) else None

    action: str | None = None
    resource: str | None = None
    confidence = "high"
    reason: str | None = None

    if name == "authorize!":
        action, resource = action_at(0), resource_at(1)
    elif name == "authorize":
        first = action_at(0)
        second = action_at(1)
        if first and not second:
            action, resource = first, resource_at(1)
        elif second and not first:
            action, resource = second, resource_at(0)
        else:
            confidence = "low"
            reason = "authorize argument order is dynamic or ambiguous"
    elif name in {"can?", "cannot?"}:
        if receiver is None and len(args) >= 3:
            action, resource = action_at(1), resource_at(2)
        else:
            action, resource = action_at(0), resource_at(1)
            if receiver not in {None, "current_user"}:
                confidence = "medium"
    elif name in {"allowed?", "denied?"}:
        if receiver == "Ability" or len(args) >= 3:
            action, resource = action_at(1), resource_at(2)
        else:
            action, resource = action_at(0), resource_at(1)
            confidence = "medium"
    elif name == "policy":
        resource = resource_at(0)
        confidence = "medium"
        reason = "policy call does not encode an authorization action"
    elif name == "pundit_authorize":
        resource, action = resource_at(0), action_at(1)
        confidence = "medium"

    if reason is None and action is None and name != "policy":
        confidence = "low"
        reason = "authorization action is dynamic or unavailable"
    if reason is None and resource is None:
        confidence = "low"
        reason = "authorization resource is dynamic or unavailable"
    return action, resource, confidence, reason


def _authorization_calls(
    inventory: Inventory,
) -> tuple[list[dict[str, Any]], dict[MethodKey, list[dict[str, Any]]], list[WarningRecord]]:
    records: list[dict[str, Any]] = []
    by_method: dict[MethodKey, list[dict[str, Any]]] = {}
    warnings: list[WarningRecord] = []
    for call in inventory.calls:
        if not call.source_file.selected or call.call_name not in _AUTH_CALLS:
            continue
        # Explicitly retain the requested receiver-specific forms.  Other
        # receivers are still call-site evidence, but inference is conservative.
        raw_arguments: list[str | None] = []
        expressions_ok = True
        for argument in call.argument_nodes:
            raw, ok = _bounded_expression(call.source_file, argument)
            raw_arguments.append(raw)
            expressions_ok = expressions_ok and ok
        action, resource, confidence, reason = _auth_inference(call)
        if not expressions_ok:
            reason = reason or "one or more authorization arguments exceeded the audit expression limit"
            confidence = "low"
        if not call.receiver_bounded:
            receiver_reason = "authorization receiver exceeds the audit expression limit"
            reason = f"{reason}; {receiver_reason}" if reason else receiver_reason
            confidence = "low"
        record = {
            "source_file": call.file,
            "source_line": call.line,
            "source_column": call.column,
            "enclosing_class_or_module": call.enclosing_class,
            "enclosing_method": call.enclosing_method,
            "receiver": call.receiver,
            "call_name": call.call_name,
            "raw_arguments": raw_arguments,
            "inferred_action": action,
            "inferred_resource": resource,
            "confidence": confidence,
            "unresolved_reason": reason,
        }
        records.append(record)
        if call.method_key is not None:
            by_method.setdefault(call.method_key, []).append(record)
        if reason:
            warnings.append(
                WarningRecord(
                    call.file,
                    call.line,
                    "authorization_call_unresolved",
                    reason,
                    "info",
                )
            )
    records.sort(
        key=lambda item: (
            item["source_file"],
            item["source_line"],
            item["source_column"],
            item["call_name"],
            item["receiver"] or "",
        )
    )
    for values in by_method.values():
        values.sort(
            key=lambda item: (
                item["source_line"],
                item["source_column"],
                item["call_name"],
            )
        )
    return records, by_method, warnings


def _camelize(value: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in value.split("_") if part)


def _singularize(value: str) -> str:
    if value.endswith("ies") and len(value) > 3:
        return f"{value[:-3]}y"
    if value.endswith("sses"):
        return value[:-2]
    if value.endswith("s") and not value.endswith("ss"):
        return value[:-1]
    return value


def _pluralize(value: str) -> str:
    if value.endswith("s"):
        return value
    if value.endswith("y") and len(value) > 1 and value[-2].lower() not in "aeiou":
        return f"{value[:-1]}ies"
    return f"{value}s"


def _join_path(*parts: str) -> str:
    segments: list[str] = []
    for part in parts:
        if not part:
            continue
        segments.extend(segment for segment in part.split("/") if segment)
    return "/" + "/".join(segments) if segments else "/"


def _controller_class(path: str, modules: tuple[str, ...]) -> str:
    parts = [part for part in path.strip("/").split("/") if part]
    module_parts = [_camelize(part) for part in modules]
    explicit_parts = [_camelize(part) for part in parts]
    if explicit_parts[: len(module_parts)] == module_parts:
        full = explicit_parts
    else:
        full = module_parts + explicit_parts
    if not full:
        return ""
    full[-1] = f"{full[-1]}Controller"
    return "::".join(full)


def _route_file(relative_path: str) -> bool:
    return (
        relative_path == "config/routes.rb"
        or relative_path.startswith("config/routes/")
        or Path(relative_path).name.endswith("_routes.rb")
    )


def _block_body(node: Any) -> Any | None:
    block = _field(node, "block")
    if block is None:
        return None
    body = _field(block, "body")
    if body is not None:
        return body
    for child in block.named_children:
        if child.type in {"body_statement", "block_body"}:
            return child
    return None


def _route_record(
    ruby_file: RubyFile,
    node: Any,
    verb: str | None,
    path: str | None,
    controller: str | None,
    action: str | None,
    context: RouteContext,
    confidence: str,
    unresolved_reason: str | None,
) -> dict[str, Any]:
    return {
        "source_file": ruby_file.relative_path,
        "source_line": _line(node),
        "source_column": _column(node),
        "http_verb": verb,
        "path": path,
        "controller": controller,
        "action": action,
        "namespace_stack": list(context.namespace_stack),
        "controller_file": None,
        "action_line": None,
        "confidence": confidence,
        "unresolved_reason": unresolved_reason,
    }


def _route_warning(
    warnings: list[WarningRecord],
    ruby_file: RubyFile,
    node: Any,
    reason: str,
    category: str = "rails_route_unresolved",
) -> None:
    warnings.append(
        WarningRecord(ruby_file.relative_path, _line(node), category, reason, "info")
    )


_PLURAL_ROUTES = (
    ("GET", "", "index"),
    ("POST", "", "create"),
    ("GET", "new", "new"),
    ("GET", ":id", "show"),
    ("GET", ":id/edit", "edit"),
    ("PATCH", ":id", "update"),
    ("PUT", ":id", "update"),
    ("DELETE", ":id", "destroy"),
)
_SINGULAR_ROUTES = (
    ("GET", "", "show"),
    ("POST", "", "create"),
    ("GET", "new", "new"),
    ("GET", "edit", "edit"),
    ("PATCH", "", "update"),
    ("PUT", "", "update"),
    ("DELETE", "", "destroy"),
)


def _emit_resource_routes(
    routes: list[dict[str, Any]],
    warnings: list[WarningRecord],
    ruby_file: RubyFile,
    node: Any,
    context: RouteContext,
    singular: bool,
) -> tuple[RouteContext | None, str | None]:
    arguments = _argument_nodes(node)
    positionals = _positional_arguments(arguments)
    options = _option_pairs(arguments, ruby_file)
    name = _static_literal(ruby_file, positionals[0]) if positionals else None
    if not name:
        reason = "resource name is dynamic or unavailable"
        routes.append(
            _route_record(ruby_file, node, None, None, None, None, context, "low", reason)
        )
        _route_warning(warnings, ruby_file, node, reason)
        return None, reason

    supported_options = {"path", "controller", "only", "except"}
    unsupported_options = sorted(set(options) - supported_options)
    if unsupported_options:
        reason = (
            "resources declaration uses unsupported route-shaping options: "
            + ", ".join(unsupported_options)
        )
        routes.append(
            _route_record(
                ruby_file, node, None, None, None, None, context, "low", reason
            )
        )
        _route_warning(warnings, ruby_file, node, reason)
        return None, reason

    parent_prefix = context.path_prefix
    if context.resource is not None:
        parent_prefix = context.resource.nested_prefix
    path_option = _static_literal(ruby_file, options.get("path"))
    if "path" in options and path_option is None:
        reason = "resources path: option is dynamic"
        routes.append(
            _route_record(
                ruby_file, node, None, None, None, None, context, "low", reason
            )
        )
        _route_warning(warnings, ruby_file, node, reason)
        return None, reason
    route_name = path_option if "path" in options else name
    base_path = _join_path(parent_prefix, route_name)
    controller_option = _static_literal(ruby_file, options.get("controller"))
    if "controller" in options and controller_option is None:
        reason = "resources controller: option is dynamic"
        routes.append(
            _route_record(
                ruby_file, node, None, base_path, None, None, context, "low", reason
            )
        )
        _route_warning(warnings, ruby_file, node, reason)
        return None, reason
    controller_path = (
        controller_option
        if "controller" in options
        else (_pluralize(name) if singular else name)
    )
    controller = _controller_class(controller_path, context.controller_modules)
    resource = ResourceContext(name, singular, base_path, controller)

    only_node = options.get("only")
    except_node = options.get("except")
    only = _static_array(ruby_file, only_node) if only_node is not None else None
    excluded = _static_array(ruby_file, except_node) if except_node is not None else None
    if only_node is not None and only is None:
        reason = "resources only: option is dynamic"
        routes.append(
            _route_record(
                ruby_file, node, None, base_path, controller, None, context, "low", reason
            )
        )
        _route_warning(warnings, ruby_file, node, reason)
        return (
            RouteContext(
                path_prefix=context.path_prefix,
                controller_modules=context.controller_modules,
                namespace_stack=context.namespace_stack,
                resource=resource,
            ),
            reason,
        )
    if except_node is not None and excluded is None:
        reason = "resources except: option is dynamic"
        routes.append(
            _route_record(
                ruby_file, node, None, base_path, controller, None, context, "low", reason
            )
        )
        _route_warning(warnings, ruby_file, node, reason)
        return (
            RouteContext(
                path_prefix=context.path_prefix,
                controller_modules=context.controller_modules,
                namespace_stack=context.namespace_stack,
                resource=resource,
            ),
            reason,
        )

    route_defs = _SINGULAR_ROUTES if singular else _PLURAL_ROUTES
    allowed = set(only) if only is not None else {item[2] for item in route_defs}
    if excluded:
        allowed.difference_update(excluded)
    for verb, suffix, action in route_defs:
        if action not in allowed:
            continue
        routes.append(
            _route_record(
                ruby_file,
                node,
                verb,
                _join_path(base_path, suffix),
                controller,
                action,
                context,
                "high",
                None,
            )
        )

    nested_context = RouteContext(
        path_prefix=context.path_prefix,
        controller_modules=context.controller_modules,
        namespace_stack=context.namespace_stack,
        resource=resource,
    )
    return nested_context, None


def _emit_verb_route(
    routes: list[dict[str, Any]],
    warnings: list[WarningRecord],
    ruby_file: RubyFile,
    node: Any,
    context: RouteContext,
    method_name: str,
) -> None:
    arguments = _argument_nodes(node)
    positionals = _positional_arguments(arguments)
    options = _option_pairs(arguments, ruby_file)

    # Hash-rocket form: get "/path" => "controller#action"
    if not positionals and arguments and arguments[0].type == "pair":
        path_node = _field(arguments[0], "key")
        target_node = _field(arguments[0], "value")
    else:
        path_node = positionals[0] if positionals else None
        target_node = options.get("to")

    route_part = _static_literal(ruby_file, path_node)
    target = _static_literal(ruby_file, target_node)
    on_scope = _static_literal(ruby_file, options.get("on"))
    controller_option = _static_literal(ruby_file, options.get("controller"))
    action_option = _static_literal(ruby_file, options.get("action"))
    reason_parts: list[str] = []

    if route_part is None:
        reason_parts.append("route path/action argument is dynamic")

    resource_scope = on_scope or context.route_scope
    base = context.path_prefix
    if context.resource is not None:
        if resource_scope == "member":
            base = context.resource.member_path
        elif resource_scope == "collection":
            base = context.resource.base_path
        elif context.resource.singular and on_scope is None and context.route_scope is None:
            base = context.resource.base_path
        elif on_scope is None and context.route_scope is None:
            reason_parts.append("custom route inside resources lacks a static member/collection scope")
            base = context.resource.base_path
        else:
            reason_parts.append("route on: scope is unsupported or dynamic")

    path = _join_path(base, route_part or "") if route_part is not None else None
    controller: str | None = None
    action: str | None = None
    if target is not None:
        if "#" not in target:
            reason_parts.append("to: target does not contain controller#action")
        else:
            controller_path, action = target.split("#", 1)
            controller = _controller_class(controller_path, context.controller_modules)
    else:
        if target_node is not None:
            reason_parts.append("route to: target is dynamic")
        action = action_option or route_part
        if controller_option:
            controller = _controller_class(controller_option, context.controller_modules)
        elif context.resource is not None:
            controller = context.resource.controller_class
        else:
            reason_parts.append("controller cannot be inferred outside a resource scope")

    if options.get("action") is not None and action_option is None:
        reason_parts.append("route action: option is dynamic")
    if options.get("controller") is not None and controller_option is None:
        reason_parts.append("route controller: option is dynamic")
    if action is None:
        reason_parts.append("action cannot be inferred")

    reason = "; ".join(dict.fromkeys(reason_parts)) or None
    confidence = "high" if reason is None else "low"
    routes.append(
        _route_record(
            ruby_file,
            node,
            method_name.upper(),
            path,
            controller,
            action,
            context,
            confidence,
            reason,
        )
    )
    if reason:
        _route_warning(warnings, ruby_file, node, reason)


def _visit_route_body(
    routes: list[dict[str, Any]],
    warnings: list[WarningRecord],
    ruby_file: RubyFile,
    body: Any,
    context: RouteContext,
) -> None:
    for node in body.named_children:
        if node.type == "comment":
            continue
        if node.type != "call":
            reason = f"dynamic or unsupported route control node: {node.type}"
            routes.append(
                _route_record(
                    ruby_file, node, None, None, None, None, context, "low", reason
                )
            )
            _route_warning(
                warnings,
                ruby_file,
                node,
                reason,
                "rails_route_dynamic_context",
            )
            continue

        name = _method_name(ruby_file, node)
        receiver_node = _field(node, "receiver")
        if receiver_node is not None:
            receiver, receiver_bounded = _bounded_expression(
                ruby_file, receiver_node
            )
            known_draw_receiver = (
                name == "draw"
                and receiver_bounded
                and receiver == "Rails.application.routes"
                and _block_body(node) is not None
            )
            if not known_draw_receiver:
                reason = (
                    "explicit call receiver cannot be proven to be the "
                    f"Rails route mapper: {receiver or '<unavailable>'}"
                )
                routes.append(
                    _route_record(
                        ruby_file, node, None, None, None, None, context, "low", reason
                    )
                )
                _route_warning(
                    warnings,
                    ruby_file,
                    node,
                    reason,
                    "rails_route_unsupported_dsl",
                )
                continue
        if name in _HTTP_METHODS:
            _emit_verb_route(routes, warnings, ruby_file, node, context, name)
            continue

        if name in {"namespace", "scope"}:
            if context.resource is not None:
                reason = (
                    f"{name} nested inside a resource scope is not composed "
                    "by the Phase 2 route index"
                )
                routes.append(
                    _route_record(
                        ruby_file, node, None, None, None, None, context, "low", reason
                    )
                )
                _route_warning(warnings, ruby_file, node, reason)
                continue
            arguments = _argument_nodes(node)
            positionals = _positional_arguments(arguments)
            options = _option_pairs(arguments, ruby_file)
            static_name = _static_literal(
                ruby_file, positionals[0] if positionals else None
            )
            if positionals and static_name is None:
                reason = f"{name} has a dynamic positional name/path"
                routes.append(
                    _route_record(
                        ruby_file, node, None, None, None, None, context, "low", reason
                    )
                )
                _route_warning(warnings, ruby_file, node, reason)
                continue
            if name == "namespace" and not positionals:
                reason = "namespace has no static positional name"
                routes.append(
                    _route_record(
                        ruby_file, node, None, None, None, None, context, "low", reason
                    )
                )
                _route_warning(warnings, ruby_file, node, reason)
                continue
            path_value = _static_literal(ruby_file, options.get("path"))
            module_value = _static_literal(ruby_file, options.get("module"))
            dynamic_options = [
                option
                for option, value in (
                    ("path", path_value),
                    ("module", module_value),
                )
                if option in options and value is None
            ]
            if dynamic_options:
                reason = (
                    f"{name} has dynamic "
                    + "/".join(f"{option}:" for option in dynamic_options)
                    + " option"
                )
                routes.append(
                    _route_record(
                        ruby_file, node, None, None, None, None, context, "low", reason
                    )
                )
                _route_warning(warnings, ruby_file, node, reason)
                continue
            if name == "namespace":
                if "path" not in options:
                    path_value = static_name
                if "module" not in options:
                    module_value = static_name
            elif static_name and "path" not in options:
                path_value = static_name
            block_body = _block_body(node)
            if block_body is None or (path_value is None and module_value is None):
                reason = f"{name} has a dynamic name/options or no static block"
                routes.append(
                    _route_record(
                        ruby_file, node, None, None, None, None, context, "low", reason
                    )
                )
                _route_warning(warnings, ruby_file, node, reason)
                continue
            module_parts = context.controller_modules
            namespaces = context.namespace_stack
            if module_value:
                module_parts = module_parts + tuple(
                    part for part in module_value.split("/") if part
                )
                namespaces = namespaces + tuple(
                    part for part in module_value.split("/") if part
                )
            child_context = RouteContext(
                path_prefix=_join_path(context.path_prefix, path_value or ""),
                controller_modules=module_parts,
                namespace_stack=namespaces,
                resource=context.resource,
                route_scope=context.route_scope,
            )
            _visit_route_body(
                routes, warnings, ruby_file, block_body, child_context
            )
            continue

        if name in {"resources", "resource"}:
            child_context, _reason = _emit_resource_routes(
                routes,
                warnings,
                ruby_file,
                node,
                context,
                singular=name == "resource",
            )
            block_body = _block_body(node)
            if block_body is not None and child_context is not None:
                _visit_route_body(
                    routes, warnings, ruby_file, block_body, child_context
                )
            continue

        if name in {"member", "collection"}:
            block_body = _block_body(node)
            if context.resource is None or block_body is None:
                reason = f"{name} route scope appears outside a static resource block"
                routes.append(
                    _route_record(
                        ruby_file, node, None, None, None, None, context, "low", reason
                    )
                )
                _route_warning(warnings, ruby_file, node, reason)
                continue
            child_context = RouteContext(
                path_prefix=context.path_prefix,
                controller_modules=context.controller_modules,
                namespace_stack=context.namespace_stack,
                resource=context.resource,
                route_scope=name,
            )
            _visit_route_body(
                routes, warnings, ruby_file, block_body, child_context
            )
            continue

        if (
            name in _TRANSPARENT_ROUTE_BLOCKS
            and receiver_node is not None
            and _block_body(node) is not None
        ):
            _visit_route_body(
                routes, warnings, ruby_file, _block_body(node), context
            )
            continue

        if name in _UNSUPPORTED_ROUTE_BLOCKS or _block_body(node) is not None:
            reason = f"unsupported Rails route DSL block: {name or '<dynamic>'}"
            routes.append(
                _route_record(
                    ruby_file, node, None, None, None, None, context, "low", reason
                )
            )
            _route_warning(
                warnings, ruby_file, node, reason, "rails_route_unsupported_dsl"
            )
            continue

        # A dynamic route such as `draw :legacy` is important negative
        # evidence even though it cannot be expanded here.
        if name in {"draw", "match", "via", "send", "public_send"}:
            reason = f"dynamic or unsupported Rails route declaration: {name}"
            routes.append(
                _route_record(
                    ruby_file, node, None, None, None, None, context, "low", reason
                )
            )
            _route_warning(warnings, ruby_file, node, reason)
            continue

        reason = f"unsupported Rails route DSL call: {name or '<dynamic>'}"
        routes.append(
            _route_record(
                ruby_file, node, None, None, None, None, context, "low", reason
            )
        )
        _route_warning(
            warnings,
            ruby_file,
            node,
            reason,
            "rails_route_unsupported_dsl",
        )


def _resolve_controller_actions(
    routes: list[dict[str, Any]],
    methods_by_key: dict[MethodKey, list[MethodDefinition]],
) -> tuple[dict[int, MethodDefinition], list[WarningRecord]]:
    targets: dict[int, MethodDefinition] = {}
    warnings: list[WarningRecord] = []
    for index, route in enumerate(routes):
        if route.get("unresolved_reason"):
            continue
        controller = route.get("controller")
        action = route.get("action")
        if not controller or not action:
            continue
        candidates = methods_by_key.get(MethodKey(controller, action, False), [])
        if len(candidates) == 1 and candidates[0].source_file.selected:
            target = candidates[0]
            route["controller_file"] = target.file
            route["action_line"] = target.line
            targets[index] = target
            continue
        if len(candidates) == 1:
            reason = (
                "controller action definition is outside selected audit files: "
                f"{controller}#{action}"
            )
        else:
            reason = (
                f"controller action has {len(candidates)} local definitions: "
                f"{controller}#{action}"
            )
        existing = route.get("unresolved_reason")
        route["unresolved_reason"] = f"{existing}; {reason}" if existing else reason
        route["confidence"] = "low"
        warnings.append(
            WarningRecord(
                route["source_file"],
                route["source_line"],
                "rails_controller_action_unresolved",
                reason,
                "info",
            )
        )
    return targets, warnings


def _rails_routes(
    inventory: Inventory,
    methods_by_key: dict[MethodKey, list[MethodDefinition]],
) -> tuple[
    list[dict[str, Any]],
    dict[int, MethodDefinition],
    list[WarningRecord],
]:
    routes: list[dict[str, Any]] = []
    warnings: list[WarningRecord] = []
    for ruby_file in inventory.files:
        if not ruby_file.selected or not _route_file(ruby_file.relative_path):
            continue
        first_record = len(routes)
        _visit_route_body(
            routes, warnings, ruby_file, ruby_file.root, RouteContext()
        )
        if ruby_file.relative_path != "config/routes.rb":
            reason = (
                "route file inclusion context is unknown; draw/scope context "
                "was not inferred"
            )
            for route in routes[first_record:]:
                existing = route.get("unresolved_reason")
                route["unresolved_reason"] = (
                    f"{existing}; {reason}" if existing else reason
                )
                route["confidence"] = "low"
            if len(routes) > first_record:
                warnings.append(
                    WarningRecord(
                        ruby_file.relative_path,
                        1,
                        "rails_route_inclusion_context_unresolved",
                        reason,
                        "info",
                    )
                )

    routes.sort(
        key=lambda item: (
            item["source_file"],
            item["source_line"],
            item["http_verb"] or "",
            item["path"] or "",
            item["controller"] or "",
            item["action"] or "",
        )
    )
    targets, resolution_warnings = _resolve_controller_actions(
        routes, methods_by_key
    )
    warnings.extend(resolution_warnings)
    return routes, targets, warnings


def _class_level_calls(
    inventory: Inventory,
) -> dict[str, list[CallSite]]:
    result: dict[str, list[CallSite]] = {}
    for call in inventory.calls:
        if call.class_level and call.enclosing_class:
            result.setdefault(call.enclosing_class, []).append(call)
    return result


def _worker_metadata(
    class_name: str,
    calls: list[CallSite],
) -> tuple[list[str], dict[str, Any]]:
    mixins: set[str] = set()
    metadata: dict[str, Any] = {
        "queue": None,
        "feature_category": None,
        "urgency": None,
        "idempotent": False,
        "sidekiq_options": {},
    }
    for call in calls:
        allowed_receivers = {None, "self"}
        if call.receiver not in allowed_receivers:
            continue
        ruby_file = call.source_file
        positionals = _positional_arguments(call.argument_nodes)
        options = _option_pairs(call.argument_nodes, ruby_file)
        if call.call_name == "include":
            for argument in positionals:
                constant = _static_constant(ruby_file, argument)
                if constant in _WORKER_MIXINS:
                    mixins.add(constant)
        elif call.call_name == "feature_category":
            metadata["feature_category"] = (
                _static_literal(ruby_file, positionals[0]) if positionals else None
            )
        elif call.call_name == "urgency":
            metadata["urgency"] = (
                _static_literal(ruby_file, positionals[0]) if positionals else None
            )
        elif call.call_name == "idempotent!":
            metadata["idempotent"] = True
        elif call.call_name == "sidekiq_options":
            rendered: dict[str, str | None] = {}
            for key, value_node in sorted(options.items()):
                value = _static_literal(ruby_file, value_node)
                if value is None:
                    value = _static_constant(ruby_file, value_node)
                rendered[key] = value
            metadata["sidekiq_options"] = rendered
            metadata["queue"] = rendered.get("queue")
    return sorted(mixins), metadata


def _workers(
    inventory: Inventory,
    methods_by_key: dict[MethodKey, list[MethodDefinition]],
) -> tuple[
    list[dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    list[WarningRecord],
]:
    classes = _class_map(inventory)
    class_calls = _class_level_calls(inventory)
    records: list[dict[str, Any]] = []
    by_name: dict[str, list[dict[str, Any]]] = {}
    warnings: list[WarningRecord] = []

    for class_name in sorted(classes):
        occurrences = [item for item in classes[class_name] if item.kind == "class"]
        if not occurrences:
            continue
        selected_occurrences = [
            item for item in occurrences if item.source_file.selected
        ]
        all_mixins, _all_metadata = _worker_metadata(
            class_name, class_calls.get(class_name, [])
        )
        selected_calls = [
            call
            for call in class_calls.get(class_name, [])
            if call.source_file.selected
        ]
        mixins, metadata = _worker_metadata(class_name, selected_calls)
        all_superclass_markers = sorted(
            {
                occurrence.superclass
                for occurrence in occurrences
                if occurrence.superclass
                and (
                    occurrence.superclass == "ApplicationWorker"
                    or occurrence.superclass.endswith("::ApplicationWorker")
                    or occurrence.superclass == "Sidekiq::Worker"
                )
            }
        )
        superclass_markers = sorted(
            {
                occurrence.superclass
                for occurrence in selected_occurrences
                if occurrence.superclass
                and (
                    occurrence.superclass == "ApplicationWorker"
                    or occurrence.superclass.endswith("::ApplicationWorker")
                    or occurrence.superclass == "Sidekiq::Worker"
                )
            }
        )
        perform_defs = methods_by_key.get(
            MethodKey(class_name, "perform", False), []
        )
        worker_marked_anywhere = bool(all_mixins or all_superclass_markers)
        if not worker_marked_anywhere and not (
            class_name.endswith("Worker") and perform_defs
        ):
            continue

        selected = bool(selected_occurrences)
        display_occurrence = (
            selected_occurrences[0] if selected else occurrences[0]
        )
        selected_worker_marked = bool(mixins or superclass_markers)
        reason: str | None = None
        confidence = "high" if selected_worker_marked else "medium"
        perform = perform_defs[0] if len(perform_defs) == 1 else None
        if not selected:
            reason = "worker definition is outside selected audit files"
            confidence = "low"
        elif not selected_worker_marked:
            reason = "worker-like class lacks a recognized Sidekiq/ApplicationWorker marker"
            confidence = "low"
        elif len(perform_defs) != 1:
            reason = f"worker perform method has {len(perform_defs)} local definitions"
            confidence = "low"
        elif perform is not None and not perform.source_file.selected:
            reason = "worker perform definition is outside selected audit files"
            perform = None
            confidence = "low"
        if reason and selected:
            warnings.append(
                WarningRecord(
                    display_occurrence.file,
                    display_occurrence.line,
                    "sidekiq_worker_unresolved",
                    f"{class_name}: {reason}",
                    "info",
                )
            )
        record = {
            "class_name": class_name,
            "class_file": display_occurrence.file,
            "class_line": display_occurrence.line,
            "worker_markers": sorted(mixins + superclass_markers),
            "perform_method_line": perform.line if perform else None,
            "perform_parameters": perform.parameters if perform else [],
            "worker_file": perform.file if perform else None,
            "queue_metadata": metadata,
            "confidence": confidence,
            "unresolved_reason": reason,
            "_selected": selected,
        }
        by_name.setdefault(class_name, []).append(record)
        if selected:
            records.append(
                {
                    key: value
                    for key, value in record.items()
                    if key != "_selected"
                }
            )
    records.sort(key=lambda item: (item["class_name"], item["class_file"]))
    return records, by_name, warnings


def _find_pair_recursive(
    ruby_file: RubyFile,
    nodes: Iterable[Any],
    wanted: str,
) -> Any | None:
    for node in nodes:
        if node.type == "pair":
            key, value = _pair_parts(node, ruby_file)
            if key == wanted:
                return value
        if node.type in {"hash", "bare_assoc_hash", "array", "argument_list"}:
            found = _find_pair_recursive(ruby_file, node.named_children, wanted)
            if found is not None:
                return found
    return None


def _resolve_worker_name(
    reference: ConstantReference | None,
    lexical_scopes: tuple[str, ...],
    worker_map: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any] | None, str | None]:
    if reference is None:
        return None, "worker receiver/class is dynamic or unavailable"
    lookup_names = _constant_lookup_names(reference, lexical_scopes)
    candidates = [
        worker
        for candidate_name in lookup_names
        for worker in worker_map.get(candidate_name, [])
    ]
    if len(candidates) != 1:
        matched_names = [
            candidate_name
            for candidate_name in lookup_names
            if worker_map.get(candidate_name)
        ]
        if len(matched_names) > 1:
            return (
                None,
                "worker class is ambiguous across lexical scopes: "
                + ", ".join(matched_names),
            )
        return (
            None,
            f"worker class has {len(candidates)} indexed definitions: {reference.name}",
        )
    worker = candidates[0]
    if not worker.get("_selected"):
        return None, "worker definition is outside selected audit files"
    if worker.get("unresolved_reason"):
        return None, f"worker definition is unresolved: {worker['unresolved_reason']}"
    return worker, None


def _enqueue_sites(
    inventory: Inventory,
    worker_map: dict[str, list[dict[str, Any]]],
    defined_classes: set[str],
) -> tuple[
    list[dict[str, Any]],
    dict[MethodKey, list[dict[str, Any]]],
    list[WarningRecord],
]:
    records: list[dict[str, Any]] = []
    by_method: dict[MethodKey, list[dict[str, Any]]] = {}
    warnings: list[WarningRecord] = []

    for call in inventory.calls:
        if not call.source_file.selected or call.call_name not in _ENQUEUE_METHODS:
            continue
        raw_arguments: list[str | None] = []
        arguments_ok = True
        for argument in call.argument_nodes:
            raw, ok = _bounded_expression(call.source_file, argument)
            raw_arguments.append(raw)
            arguments_ok = arguments_ok and ok

        receiver_reference = _static_constant_reference(
            call.source_file, call.receiver_node
        )
        receiver_class = receiver_reference.name if receiver_reference else None
        reason: str | None = (
            None
            if call.receiver_bounded
            else "enqueue receiver exceeds the audit expression limit"
        )
        receiver_api_unresolved = False
        if call.call_name in {"push", "push_bulk"}:
            if receiver_class != "Sidekiq::Client":
                reason = "push/push_bulk receiver is not statically Sidekiq::Client"
                receiver_class = None
                receiver_reference = None
            else:
                receiver_lookup = _constant_lookup_names(
                    receiver_reference,
                    call.lexical_scopes,
                )
                shadowed = [
                    name
                    for name in receiver_lookup
                    if name != "Sidekiq::Client" and name in defined_classes
                ]
                if shadowed:
                    receiver_api_unresolved = True
                    reason = (
                        "relative Sidekiq::Client receiver is shadowed in a "
                        "lexical scope: "
                        + ", ".join(shadowed)
                    )
                class_node = _find_pair_recursive(
                    call.source_file, call.argument_nodes, "class"
                )
                receiver_reference = _static_constant_reference(
                    call.source_file, class_node
                )
                receiver_class = (
                    receiver_reference.name if receiver_reference else None
                )
                if receiver_reference is None:
                    reason = "Sidekiq::Client push class is dynamic or unavailable"
        elif call.call_name == "delay":
            reason = "Sidekiq delay proxy cannot be connected to a worker perform method"

        worker, worker_reason = _resolve_worker_name(
            receiver_reference,
            call.lexical_scopes,
            worker_map,
        )
        if call.call_name == "delay":
            worker = None
        if receiver_api_unresolved:
            worker = None
        if worker_reason and reason is None:
            reason = worker_reason
        if not arguments_ok and reason is None:
            reason = "one or more enqueue arguments exceeded the audit expression limit"

        confidence = "high" if worker is not None and reason is None else "low"
        record = {
            "enqueue_source_file": call.file,
            "enqueue_line": call.line,
            "enqueue_column": call.column,
            "enclosing_class_or_module": call.enclosing_class,
            "enclosing_method": call.enclosing_method,
            "receiver_class_name": receiver_class or call.receiver,
            "enqueue_method": call.call_name,
            "raw_argument_expressions": raw_arguments,
            "resolved_worker_class": worker["class_name"] if worker else None,
            "worker_file": worker["worker_file"] if worker else None,
            "perform_method_line": worker["perform_method_line"] if worker else None,
            "perform_parameters": worker["perform_parameters"] if worker else [],
            "queue_metadata": worker["queue_metadata"] if worker else None,
            "confidence": confidence,
            "unresolved_reason": reason,
        }
        records.append(record)
        if call.method_key is not None:
            by_method.setdefault(call.method_key, []).append(record)
        if reason:
            warnings.append(
                WarningRecord(
                    call.file,
                    call.line,
                    "sidekiq_enqueue_unresolved",
                    reason,
                    "info",
                )
            )

    records.sort(
        key=lambda item: (
            item["enqueue_source_file"],
            item["enqueue_line"],
            item["enqueue_column"],
            item["enqueue_method"],
            item["receiver_class_name"] or "",
        )
    )
    for values in by_method.values():
        values.sort(
            key=lambda item: (
                item["enqueue_line"],
                item["enqueue_column"],
                item["enqueue_method"],
            )
        )
    return records, by_method, warnings


def _service_calls(inventory: Inventory) -> dict[MethodKey, list[ServiceCall]]:
    result: dict[MethodKey, list[ServiceCall]] = {}
    for call in inventory.calls:
        if (
            not call.source_file.selected
            or call.method_key is None
            or call.call_name in _AUTH_CALLS | _ENQUEUE_METHODS
        ):
            continue
        target_class: str | None = None
        singleton = True
        evidence: str | None = None

        target_reference: ConstantReference | None = None
        direct_reference = _static_constant_reference(
            call.source_file, call.receiver_node
        )
        if direct_reference and call.call_name != "new" and (
            direct_reference.name.endswith("Service")
            or call.call_name in _SERVICE_TERMINALS
        ):
            target_class = direct_reference.name
            target_reference = direct_reference
            singleton = True
            prefix = "::" if direct_reference.absolute else ""
            evidence = (
                f"static constant receiver "
                f"{prefix}{direct_reference.name}.{call.call_name}"
            )
        elif call.receiver_node is not None and call.receiver_node.type == "call":
            inner_name = _method_name(call.source_file, call.receiver_node)
            inner_receiver = _field(call.receiver_node, "receiver")
            inner_reference = _static_constant_reference(
                call.source_file, inner_receiver
            )
            if (
                inner_name == "new"
                and inner_reference
                and (
                    inner_reference.name.endswith("Service")
                    or call.call_name in _SERVICE_TERMINALS
                )
            ):
                target_class = inner_reference.name
                target_reference = inner_reference
                singleton = False
                prefix = "::" if inner_reference.absolute else ""
                evidence = (
                    f"static constructor receiver "
                    f"{prefix}{inner_reference.name}.new(...).{call.call_name}"
                )
        elif call.receiver in {None, "self"} and call.enclosing_class:
            target_class = call.enclosing_class
            target_reference = ConstantReference(target_class, True)
            singleton = call.method_key.singleton
            evidence = f"same-class {'self.' if call.receiver else 'bare '}call"

        if target_class and target_reference and evidence:
            result.setdefault(call.method_key, []).append(
                ServiceCall(
                    source_method=call.method_key,
                    file=call.file,
                    line=call.line,
                    column=call.column,
                    receiver_class=target_class,
                    receiver_absolute=target_reference.absolute,
                    lexical_scopes=call.lexical_scopes,
                    method_name=call.call_name,
                    singleton=singleton,
                    evidence=evidence,
                )
            )
    for values in result.values():
        values.sort(
            key=lambda item: (
                item.file,
                item.line,
                item.column,
                item.receiver_class,
                item.method_name,
                item.singleton,
            )
        )
    return result


def _resolve_service_call(
    service_call: ServiceCall,
    methods_by_key: dict[MethodKey, list[MethodDefinition]],
) -> tuple[MethodDefinition | None, str | None]:
    reference = ConstantReference(
        service_call.receiver_class,
        service_call.receiver_absolute,
    )
    lookup_names = _constant_lookup_names(reference, service_call.lexical_scopes)
    lookup_keys = [
        MethodKey(
            class_name,
            service_call.method_name,
            service_call.singleton,
        )
        for class_name in lookup_names
    ]
    candidates = [
        definition
        for key in lookup_keys
        for definition in methods_by_key.get(key, [])
    ]
    if len(candidates) == 1:
        if candidates[0].source_file.selected:
            return candidates[0], None
        return (
            None,
            "service target definition is outside selected audit files: "
            + candidates[0].key.label(),
        )
    matched_keys = [
        key.label()
        for key in lookup_keys
        if methods_by_key.get(key)
    ]
    if len(matched_keys) > 1:
        return (
            None,
            "service class is ambiguous across lexical scopes: "
            + ", ".join(matched_keys),
        )
    if not candidates:
        basename_matches = sorted(
            candidate_key.label()
            for candidate_key, definitions in methods_by_key.items()
            if candidate_key.method_name == service_call.method_name
            and candidate_key.singleton == service_call.singleton
            and candidate_key.class_name.split("::")[-1]
            == service_call.receiver_class.split("::")[-1]
            and definitions
        )
        if len(basename_matches) > 1:
            return (
                None,
                "service class is ambiguous across local namespaces: "
                + ", ".join(basename_matches),
            )
        if len(basename_matches) == 1:
            return (
                None,
                "service class would require unproven namespace inference: "
                + basename_matches[0],
            )
    return (
        None,
        "service target has "
        f"{len(candidates)} exact local definitions: "
        + ", ".join(key.label() for key in lookup_keys),
    )


def _reachable_enqueues(
    start: MethodDefinition,
    enqueues_by_method: dict[MethodKey, list[dict[str, Any]]],
    service_calls: dict[MethodKey, list[ServiceCall]],
    methods_by_key: dict[MethodKey, list[MethodDefinition]],
) -> tuple[
    list[tuple[dict[str, Any], list[tuple[ServiceCall, MethodDefinition]]]],
    list[str],
]:
    found: list[
        tuple[dict[str, Any], list[tuple[ServiceCall, MethodDefinition]]]
    ] = []
    gaps: list[str] = []
    queue: list[
        tuple[MethodDefinition, list[tuple[ServiceCall, MethodDefinition]], int]
    ] = [(start, [], 0)]
    visited: set[MethodKey] = set()
    while queue:
        method, transitions, depth = queue.pop(0)
        if method.key in visited:
            continue
        visited.add(method.key)
        for enqueue in enqueues_by_method.get(method.key, []):
            found.append((enqueue, transitions))
        if depth >= 3:
            if service_calls.get(method.key):
                gaps.append(
                    f"service traversal limit reached at {method.key.label()}"
                )
            continue
        for call in service_calls.get(method.key, []):
            target, reason = _resolve_service_call(call, methods_by_key)
            if target is None:
                gaps.append(
                    f"{call.file}:{call.line}: {reason or 'service call unresolved'}"
                )
                continue
            if target.key in visited:
                continue
            queue.append((target, transitions + [(call, target)], depth + 1))
    found.sort(
        key=lambda item: (
            item[0]["enqueue_source_file"],
            item[0]["enqueue_line"],
            tuple(
                (transition[0].file, transition[0].line)
                for transition in item[1]
            ),
        )
    )
    return found, sorted(set(gaps))


def _hop(
    source_file: str,
    source_line: int,
    destination_file: str,
    destination_line: int,
    relation: str,
    confidence: str,
    evidence: str,
) -> dict[str, Any]:
    return {
        "source_file": source_file,
        "source_line": source_line,
        "destination_file": destination_file,
        "destination_line": destination_line,
        "relation": relation,
        "confidence": confidence,
        "evidence": evidence,
    }


def _path_id(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"hunt-path-{hashlib.sha256(canonical).hexdigest()[:20]}"


def _execution_paths(
    routes: list[dict[str, Any]],
    route_targets: dict[int, MethodDefinition],
    auth_by_method: dict[MethodKey, list[dict[str, Any]]],
    enqueue_by_method: dict[MethodKey, list[dict[str, Any]]],
    service_calls: dict[MethodKey, list[ServiceCall]],
    methods_by_key: dict[MethodKey, list[MethodDefinition]],
) -> tuple[list[dict[str, Any]], list[WarningRecord]]:
    paths: list[dict[str, Any]] = []
    warnings: list[WarningRecord] = []
    truncated = False

    for route_index, route in enumerate(routes):
        if len(paths) >= _MAX_EXECUTION_PATHS:
            truncated = True
            break

        action_method = route_targets.get(route_index)
        if action_method is None:
            payload = {
                "route": {
                    "file": route["source_file"],
                    "line": route["source_line"],
                    "column": route.get("source_column"),
                    "verb": route["http_verb"],
                    "path": route["path"],
                    "controller": route.get("controller"),
                    "action": route.get("action"),
                },
                "status": "partial",
            }
            paths.append(
                {
                    "path_id": _path_id(payload),
                    "status": "partial",
                    "route": payload["route"],
                    "hops": [],
                    "confidence": "low",
                    "unresolved_gaps": [
                        route.get("unresolved_reason")
                        or "controller action is not uniquely resolved"
                    ],
                }
            )
            continue

        authorizations = auth_by_method.get(action_method.key, [])
        reachable, service_gaps = _reachable_enqueues(
            action_method,
            enqueue_by_method,
            service_calls,
            methods_by_key,
        )
        for gap in service_gaps:
            match = re.match(r"^(.+):(\d+):\s*(.+)$", gap)
            warnings.append(
                WarningRecord(
                    match.group(1) if match else route["source_file"],
                    int(match.group(2)) if match else route["source_line"],
                    "service_call_unresolved",
                    match.group(3) if match else gap,
                    "info",
                )
            )
        route_view = {
            "file": route["source_file"],
            "line": route["source_line"],
            "verb": route["http_verb"],
            "path": route["path"],
            "controller": route.get("controller"),
            "action": route.get("action"),
        }
        authorization_options: list[dict[str, Any] | None] = (
            list(authorizations) if authorizations else [None]
        )
        reachable_options: list[
            tuple[dict[str, Any], list[tuple[ServiceCall, MethodDefinition]]] | None
        ] = list(reachable) if reachable else [None]

        for authorization in authorization_options:
            for reachable_item in reachable_options:
                if len(paths) >= _MAX_EXECUTION_PATHS:
                    truncated = True
                    break
                enqueue: dict[str, Any] | None = None
                transitions: list[tuple[ServiceCall, MethodDefinition]] = []
                if reachable_item is not None:
                    enqueue, transitions = reachable_item
                hops: list[dict[str, Any]] = [
                    _hop(
                        route["source_file"],
                        route["source_line"],
                        action_method.file,
                        action_method.line,
                        "route_to_controller",
                        "high",
                        f"static Rails route target {action_method.key.label()}",
                    ),
                ]
                if authorization is not None:
                    hops.append(
                        _hop(
                        action_method.file,
                        action_method.line,
                        authorization["source_file"],
                        authorization["source_line"],
                        "contains_authorization",
                        "high",
                        (
                            f"{authorization['call_name']} call is lexically contained "
                            "in the controller action; "
                            f"inferred action={authorization.get('inferred_action')!r}, "
                            f"resource={authorization.get('inferred_resource')!r}"
                        ),
                        )
                    )
                previous = action_method
                for service_call, service_method in transitions:
                    hops.append(
                        _hop(
                            previous.file,
                            service_call.line,
                            service_method.file,
                            service_method.line,
                            "calls_service",
                            "high",
                            service_call.evidence,
                        )
                    )
                    previous = service_method
                if enqueue is not None:
                    hops.append(
                        _hop(
                            previous.file,
                            previous.line,
                            enqueue["enqueue_source_file"],
                            enqueue["enqueue_line"],
                            "contains_enqueue",
                            "medium",
                            (
                                f"{enqueue['enqueue_method']} enqueue is lexically reachable "
                                "through only static, unique service calls; branch and order "
                                "are not proven"
                            ),
                        )
                    )
                # Unresolved sibling calls remain warnings, but they are not
                # evidence gaps in a separately proven static path to this
                # enqueue. Preserve them on the partial record only when no
                # reachable enqueue was found.
                gaps = list(service_gaps) if enqueue is None else []
                if authorization is None:
                    gaps.append(
                        "no indexed authorization call is lexically contained in the controller action"
                    )
                elif authorization.get("unresolved_reason"):
                    gaps.append(
                        f"authorization inference unresolved: {authorization['unresolved_reason']}"
                    )
                if enqueue is None:
                    gaps.append(
                        "no statically reachable Sidekiq enqueue was found within three service hops"
                    )
                elif enqueue.get("unresolved_reason"):
                    gaps.append(
                        f"worker resolution unresolved: {enqueue['unresolved_reason']}"
                    )
                if enqueue is not None and enqueue.get("worker_file") and enqueue.get(
                    "perform_method_line"
                ):
                    hops.append(
                        _hop(
                            enqueue["enqueue_source_file"],
                            enqueue["enqueue_line"],
                            enqueue["worker_file"],
                            enqueue["perform_method_line"],
                            "enqueue_to_worker_perform",
                            "high",
                            f"static worker receiver {enqueue['resolved_worker_class']} uniquely matches a local perform method",
                        )
                    )
                elif enqueue is not None:
                    gaps.append("enqueue could not be connected to a unique worker perform method")

                status = "resolved" if not gaps else "partial"
                confidence = "medium" if status == "resolved" else "low"
                identity = {
                    "route": [
                        route["source_file"],
                        route["source_line"],
                        route.get("source_column"),
                        route["http_verb"],
                        route["path"],
                    ],
                    "authorization": (
                        [
                            authorization["source_file"],
                            authorization["source_line"],
                            authorization.get("source_column"),
                            authorization["call_name"],
                            authorization.get("receiver"),
                            authorization.get("raw_arguments"),
                        ]
                        if authorization is not None
                        else None
                    ),
                    "services": [
                        [
                            call.file,
                            call.line,
                            call.column,
                            method.key.label(),
                        ]
                        for call, method in transitions
                    ],
                    "enqueue": (
                        [
                            enqueue["enqueue_source_file"],
                            enqueue["enqueue_line"],
                            enqueue.get("enqueue_column"),
                            enqueue["enqueue_method"],
                            enqueue.get("receiver_class_name"),
                            enqueue.get("raw_argument_expressions"),
                        ]
                        if enqueue is not None
                        else None
                    ),
                    "worker": (
                        enqueue.get("resolved_worker_class")
                        if enqueue is not None
                        else None
                    ),
                    "gaps": sorted(set(gaps)),
                }
                paths.append(
                    {
                        "path_id": _path_id(identity),
                        "status": status,
                        "route": route_view,
                        "hops": hops,
                        "confidence": confidence,
                        "unresolved_gaps": sorted(set(gaps)),
                    }
                )
            if truncated:
                break
        if truncated:
            break

    if truncated:
        warnings.append(
            WarningRecord(
                None,
                None,
                "execution_path_cap_reached",
                f"execution path output capped at {_MAX_EXECUTION_PATHS} records",
                "warning",
            )
        )

    paths.sort(key=lambda item: item["path_id"])
    return paths, warnings


def _apply_definition_universe_barrier(
    inventory: Inventory,
    routes: list[dict[str, Any]],
    workers: list[dict[str, Any]],
    worker_map: dict[str, list[dict[str, Any]]],
) -> str | None:
    """Prevent unique links when any guarded Ruby definition may be missing."""

    blockers = sorted(set(inventory.resolution_blockers))
    if not blockers:
        return None

    displayed = blockers[:5]
    suffix = (
        f", and {len(blockers) - len(displayed)} more"
        if len(blockers) > len(displayed)
        else ""
    )
    reason = (
        "definition universe is incomplete because guarded Ruby input could "
        f"not be indexed: {', '.join(displayed)}{suffix}"
    )

    for route in routes:
        route.pop("controller_file", None)
        route.pop("action_line", None)
        existing = route.get("unresolved_reason")
        route["unresolved_reason"] = f"{existing}; {reason}" if existing else reason
        route["confidence"] = "low"

    for worker in workers:
        existing = worker.get("unresolved_reason")
        worker["unresolved_reason"] = (
            f"{existing}; {reason}" if existing else reason
        )
        worker["confidence"] = "low"
    for candidates in worker_map.values():
        for worker in candidates:
            existing = worker.get("unresolved_reason")
            worker["unresolved_reason"] = (
                f"{existing}; {reason}" if existing else reason
            )
            worker["confidence"] = "low"

    return reason


def _warning_sort_key(item: WarningRecord | dict[str, Any]) -> tuple[Any, ...]:
    if isinstance(item, WarningRecord):
        data = item.as_dict()
    else:
        data = item
    return (
        data.get("file") or "",
        data.get("line") or 0,
        data.get("category") or "",
        data.get("reason") or "",
        data.get("severity") or "",
    )


def build_audit_indexes(
    target: Path,
    include_patterns: Iterable[str] = (),
    exclude_patterns: Iterable[str] = (),
) -> dict[str, Any]:
    """Build all Phase 2 local audit indexes for ``target``.

    ``include_patterns`` and ``exclude_patterns`` apply only to these audit
    indexes.  The caller's product ``build_graph`` invocation remains a full
    graph build, as required by the hunt manifest contract.
    """

    target = Path(target).resolve()
    includes = tuple(include_patterns)
    excludes = tuple(exclude_patterns)
    inventory = _collect_inventory(target, includes, excludes)
    methods_by_key = _method_map(inventory)

    authorization, auth_by_method, auth_warnings = _authorization_calls(
        inventory
    )
    routes, route_targets, route_warnings = _rails_routes(
        inventory, methods_by_key
    )
    workers, worker_map, worker_warnings = _workers(
        inventory, methods_by_key
    )
    barrier_reason = _apply_definition_universe_barrier(
        inventory,
        routes,
        workers,
        worker_map,
    )
    barrier_warnings: list[WarningRecord] = []
    if barrier_reason is not None:
        route_targets = {}
        barrier_warnings.append(
            WarningRecord(
                None,
                None,
                "ruby_definition_universe_incomplete",
                barrier_reason,
                "warning",
            )
        )
    enqueues, enqueue_by_method, enqueue_warnings = _enqueue_sites(
        inventory,
        worker_map,
        set(_class_map(inventory)),
    )
    service_calls = _service_calls(inventory)
    execution_paths, path_warnings = _execution_paths(
        routes,
        route_targets,
        auth_by_method,
        enqueue_by_method,
        service_calls,
        methods_by_key,
    )

    all_warnings = (
        inventory.warnings
        + auth_warnings
        + route_warnings
        + worker_warnings
        + barrier_warnings
        + enqueue_warnings
        + path_warnings
    )
    deduplicated_warnings: dict[
        tuple[str | None, int | None, str, str, str], WarningRecord
    ] = {}
    for warning in all_warnings:
        key = (
            warning.file,
            warning.line,
            warning.category,
            warning.reason,
            warning.severity,
        )
        deduplicated_warnings[key] = warning
    all_warnings = sorted(
        deduplicated_warnings.values(), key=_warning_sort_key
    )
    return {
        "extractor_version": HUNT_INDEX_EXTRACTOR_VERSION,
        "rails_routes": routes,
        "authorization_calls": authorization,
        "sidekiq_jobs": {
            "workers": workers,
            "enqueue_sites": enqueues,
        },
        "execution_paths": execution_paths,
        "warnings": [warning.as_dict() for warning in all_warnings],
    }
