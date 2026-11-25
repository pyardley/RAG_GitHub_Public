"""ActiveRecord tables and the associations that stay in the database.

A flow chart is one request. This is the model that outlives that request:
``db/schema.rb`` for the tables, and ``belongs_to``, ``has_many``, ``has_one``,
and ``has_and_belongs_to_many`` for the lines between them. Form objects and
framework tables such as ``active_storage`` are left out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_CREATE = re.compile(r'^create_table\s+"(?P<name>[^"]+)"(?P<opts>.*?)\sdo\s*\|t\|\s*$')
_COLUMN = re.compile(
    r"^t\.(?P<type>bigint|integer|int|string|text|boolean|datetime|date|decimal|"
    r"float|jsonb|json|uuid|binary|inet|time|timestamp|numeric|citext)\s+"
    r'"(?P<name>[^"]+)"'
)
_FOREIGN = re.compile(r'^add_foreign_key\s+"(?P<from>[^"]+)"\s*,\s*"(?P<to>[^"]+)"(?P<rest>.*)$')
_FOREIGN_COLUMN = re.compile(r'column:\s*"([^"]+)"')
_CLASS = re.compile(r"^class\s+(?P<name>\w+)\s*<\s*(?P<parent>[\w:]+)")
_TABLE_NAME = re.compile(r"""self\.table_name\s*=\s*['"]([^'"]+)['"]""")
_ASSOC = re.compile(r"^(?P<kind>belongs_to|has_one|has_many|has_and_belongs_to_many)\s+:(?P<name>\w+)")
_OPEN = re.compile(r"\b(?:class|module|def|do|begin|case)\b")
_CLOSE = re.compile(r"\bend\b")
_CLASS_NAME = re.compile(r"""class_name:\s*(?:['"]([^'"]+)['"]|([A-Z]\w*(?:::[A-Z]\w*)*))""")
_FOREIGN_KEY = re.compile(r"""foreign_key:\s*(?:['"]:?(?P<quoted>\w+)['"]|:(?P<symbol>\w+))""")
_TIMESTAMPS = {"created_at", "updated_at"}
_ROOTS = {"ApplicationRecord", "ActiveRecord::Base"}
_IRREGULAR_PLURAL = {"person": "people", "child": "children", "man": "men"}
_IRREGULAR_SINGULAR = {"people": "person", "children": "child", "men": "man"}
_FRAMEWORK_PREFIXES = ("active_storage_", "action_text_", "action_mailbox_", "action_cable_")
_FRAMEWORK_NAMES = {"schema_migrations", "ar_internal_metadata"}
_COLUMN_CAP = 6


@dataclass
class _Model:
    name: str
    parent: str
    path: str
    line: int
    table_name: str = ""
    abstract: bool = False


@dataclass
class _Column:
    name: str
    type_name: str
    primary: bool = False
    null: bool = True
    references: str = ""

    def as_dict(self) -> dict:
        payload = {
            "name": self.name,
            "type": self.type_name,
            "primary": self.primary,
            "null": self.null,
        }
        if self.references:
            payload["references"] = self.references
        return payload


@dataclass
class _Table:
    name: str
    path: str = ""
    line: int = 1
    columns: list[_Column] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    polymorphic: bool = False

    def as_dict(self, shown: list[_Column], more: int, focused: bool) -> dict:
        payload = {
            "name": self.name,
            "models": list(self.models),
            "subtitle": _subtitle(self),
            "polymorphic": self.polymorphic,
            "path": self.path,
            "line": self.line,
            "columns": [column.as_dict() for column in shown],
            "more": more,
            "focused": focused,
        }
        return payload


@dataclass
class _Edge:
    source: str
    target: str
    kind: str
    label: str
    inverse: str = ""
    column: str = ""
    through: str = ""
    optional: bool = False
    path: str = ""
    line: int = 1
    has_many: bool = False
    has_one: bool = False

    def as_dict(self) -> dict:
        payload = {
            "from": self.source,
            "to": self.target,
            "kind": self.kind,
            "label": self.label,
            "column": self.column,
            "optional": self.optional,
            "path": self.path,
            "line": self.line,
        }
        if self.inverse:
            payload["inverse"] = self.inverse
        if self.through:
            payload["through"] = self.through
        return payload


def entity_diagram(root: Path, focus: str | None = None) -> dict:
    """Tables and associations in one checkout. ``focus`` keeps one table and its neighbours."""
    schema_path = root / "db" / "schema.rb"
    schema_text = ""
    if schema_path.is_file() and schema_path.stat().st_size <= 1_000_000:
        schema_text = schema_path.read_text(encoding="utf-8", errors="replace")
    tables = _parse_schema(schema_text, "db/schema.rb") if schema_text else {}
    models = _parse_models(root)
    _attach_models(tables, models)
    edges, unresolved = _associations(tables, models, "db/schema.rb")
    _mark_references(tables, edges)
    note = _note(bool(schema_text), bool(tables), unresolved)
    asked = " ".join((focus or "").split())
    chosen = _match_focus(asked, tables) if asked else ""
    total = len(tables)
    message = ""
    if asked and not chosen:
        message = f"No table or model named {asked}."
        tables = {}
        edges = []
    elif chosen:
        tables, edges = _focus(tables, edges, chosen)
        note = f"{_label(tables[chosen])} and the tables it joins. {len(tables)} of {total} tables. {note}"
    shown = [
        _present(table, bool(chosen), chosen)
        for table in sorted(tables.values(), key=lambda item: (item.polymorphic, item.name))
    ]
    associations = [edge.as_dict() for edge in edges if edge.source in tables and edge.target in tables]
    associations.sort(key=lambda item: (item["from"], item["to"], item["label"]))
    return {
        "tables": shown,
        "associations": associations,
        "note": note,
        "message": message,
        "focus": chosen,
        "table_count": total,
    }


def _present(table: _Table, focused_view: bool, chosen: str) -> dict:
    # A focused table shows a few more columns than its neighbours. The rest
    # stay counted in ``more`` so a hub such as Application does not become a poster.
    cap = 12 if focused_view and table.name == chosen else _COLUMN_CAP
    if table.polymorphic:
        shown, more = table.columns, 0
    else:
        shown, more = _overview(table.columns, cap)
    return table.as_dict(shown, more, bool(chosen) and table.name == chosen)


def _overview(columns: list[_Column], cap: int = _COLUMN_CAP) -> tuple[list[_Column], int]:
    keys = [
        column for column in columns
        if column.primary or column.references or column.name.endswith("_id") or column.name.endswith("_type")
    ]
    others = [column for column in columns if column not in keys and column.name not in _TIMESTAMPS]
    hidden = [column for column in columns if column not in keys and column not in others]
    extra = others[:cap]
    return keys + extra, len(others[cap:]) + len(hidden)


def _subtitle(table: _Table) -> str:
    if table.polymorphic:
        names = ", ".join(table.models)
        return f"polymorphic: {names}" if names else "polymorphic"
    return ", ".join(table.models)


def _note(has_schema: bool, has_tables: bool, unresolved: int) -> str:
    if not has_tables:
        if has_schema:
            return "The schema has no application tables."
        return "This checkout has no ActiveRecord models and no db/schema.rb."
    if has_schema:
        text = "Tables come from db/schema.rb and the ActiveRecord models. Framework tables are left out."
    else:
        text = "No db/schema.rb was found, so the tables come from the models and have no columns."
    if unresolved == 1:
        text += " 1 association was left out because its table is not in this checkout."
    elif unresolved:
        text += f" {unresolved} associations were left out because their tables are not in this checkout."
    return text


def _label(table: _Table) -> str:
    if table.models:
        return table.models[0]
    return table.name


def _match_focus(asked: str, tables: dict[str, _Table]) -> str:
    key = asked.lower().replace(" ", "").replace("_", "")
    exact_table = ""
    exact_model = ""
    for table in tables.values():
        if table.polymorphic:
            continue
        if _compact(table.name) == key:
            exact_table = table.name
        for model in table.models:
            if _compact(model) == key:
                exact_model = table.name
    return exact_table or exact_model


def _compact(value: str) -> str:
    return value.lower().replace("_", "")


def _focus(tables: dict[str, _Table], edges: list[_Edge], chosen: str) -> tuple[dict[str, _Table], list[_Edge]]:
    keep = {chosen}
    models = set(tables[chosen].models)
    for edge in edges:
        if edge.source == chosen or edge.target == chosen:
            keep.add(edge.source)
            keep.add(edge.target)
        elif edge.kind == "polymorphic" and edge.target in tables:
            if models.intersection(tables[edge.target].models):
                keep.add(edge.source)
                keep.add(edge.target)
    kept = {name: table for name, table in tables.items() if name in keep}
    return kept, [edge for edge in edges if edge.source in keep and edge.target in keep]


def _parse_schema(text: str, path: str) -> dict[str, _Table]:
    tables: dict[str, _Table] = {}
    current: _Table | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw).strip()
        if current is None:
            match = _CREATE.match(line)
            if match is None:
                foreign = _FOREIGN.match(line)
                if foreign is not None:
                    _add_foreign_key(tables, foreign, path, number)
                continue
            name = match.group("name")
            if _framework(name):
                current = _Table(name="__skip__")
                continue
            current = _Table(name=name, path=path, line=number)
            primary = _implicit_primary_key(match.group("opts"))
            if primary is not None:
                current.columns.append(_Column(primary[0], primary[1], primary=True, null=False))
            continue
        if line == "end":
            if current.name != "__skip__":
                tables[current.name] = current
            current = None
            continue
        if current.name == "__skip__":
            continue
        column = _COLUMN.match(line)
        if column is None:
            continue
        name = column.group("name")
        if any(item.name == name for item in current.columns):
            continue
        current.columns.append(_Column(
            name,
            column.group("type"),
            null="null: false" not in line,
        ))
    return tables


def _implicit_primary_key(options: str) -> tuple[str, str] | None:
    if re.search(r"\bid:\s*false\b", options):
        return None
    name = "id"
    named = re.search(r'primary_key:\s*"([^"]+)"', options)
    if named:
        name = named.group(1)
    kind = re.search(r"\bid:\s*:(\w+)", options)
    return name, kind.group(1) if kind else "bigint"


def _add_foreign_key(tables: dict[str, _Table], match: re.Match[str], path: str, line: int) -> None:
    source = match.group("from")
    target = match.group("to")
    if source not in tables or _framework(target):
        return
    column_name = _FOREIGN_COLUMN.search(match.group("rest"))
    column = column_name.group(1) if column_name else f"{_singularize(target)}_id"
    for item in tables[source].columns:
        if item.name == column:
            item.references = target
            return
    tables[source].columns.append(_Column(column, "bigint", null=True, references=target))
    # The foreign key is declared, even when the column line was not recognised.
    _ = (path, line)


def _parse_models(root: Path) -> dict[str, _Model]:
    folder = root / "app" / "models"
    models: dict[str, _Model] = {}
    if not folder.is_dir():
        return models
    for path in sorted(folder.rglob("*.rb")):
        if not path.is_file() or path.stat().st_size > 500_000:
            continue
        relative = path.relative_to(root).as_posix()
        _parse_model_file(path.read_text(encoding="utf-8", errors="replace"), relative, models)
    return models


def _parse_model_file(text: str, path: str, models: dict[str, _Model]) -> None:
    current: _Model | None = None
    class_depth = 0
    depth = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        code = _strip_comment(raw).strip()
        if code and current is not None and depth == class_depth + 1:
            table_name = _TABLE_NAME.search(code)
            if table_name:
                current.table_name = table_name.group(1)
            elif "self.abstract_class" in code and "true" in code:
                current.abstract = True
            else:
                association = _ASSOC.match(code)
                if association:
                    current_associations(current).append({
                        "kind": association.group("kind"),
                        "name": association.group("name"),
                        "code": code,
                        "path": path,
                        "line": number,
                    })
        if code and current is None:
            match = _CLASS.match(code)
            if match and match.group("name") not in models:
                current = _Model(match.group("name"), match.group("parent"), path, number)
                models[current.name] = current
                class_depth = depth
        if code:
            depth += len(_OPEN.findall(code)) + code.count("{")
            depth -= len(_CLOSE.findall(code)) + code.count("}")
            if depth < 0:
                depth = 0
        if current is not None and depth <= class_depth:
            current = None


def current_associations(model: _Model) -> list[dict]:
    associations = getattr(model, "associations", None)
    if associations is None:
        associations = []
        setattr(model, "associations", associations)
    return associations


def _attach_models(tables: dict[str, _Table], models: dict[str, _Model]) -> None:
    grouped: dict[str, list[_Model]] = {}
    for model in models.values():
        if model.abstract or _is_root(model.name) or not _record_chain(model, models):
            continue
        table_name = _table_for(model.name, models)
        if not table_name or _framework(table_name):
            continue
        grouped.setdefault(table_name, []).append(model)
    for table_name, group in grouped.items():
        group.sort(key=lambda model: (_model_rank(model, table_name), model.name))
        table = tables.get(table_name)
        if table is None:
            primary = group[0]
            table = _Table(name=table_name, path=primary.path, line=primary.line)
            tables[table_name] = table
        elif not table.path or table.path.endswith("schema.rb"):
            primary = group[0]
            if primary.path:
                table.path = primary.path
                table.line = primary.line
        table.models = [model.name for model in group]


def _model_rank(model: _Model, table_name: str) -> int:
    if model.table_name == table_name:
        return 0
    if not model.table_name and _tableize(model.name) == table_name and _is_root(model.parent):
        return 0
    return 1


def _associations(
    tables: dict[str, _Table],
    models: dict[str, _Model],
    schema_path: str,
) -> tuple[list[_Edge], int]:
    merged: dict[tuple[str, str, str], _Edge] = {}
    through: list[_Edge] = []
    polymorphic: dict[str, _Table] = {}
    unresolved = 0
    declared = [
        (model, item)
        for model in models.values()
        if _record_chain(model, models) and not model.abstract and not _is_root(model.name)
        for item in current_associations(model)
    ]
    for model, item in declared:
        if item["kind"] == "belongs_to" and "polymorphic: true" in item["code"]:
            _polymorphic_belongs(model, item, tables, models, polymorphic, merged)
            continue
        if item.get("kind") in {"has_many", "has_one"} and _option_symbol(item["code"], "as"):
            _polymorphic_inverse(model, item, tables, models, polymorphic, merged)
            continue
        source = _table_for(model.name, models)
        if not source or _framework(source):
            continue
        target = _association_table(item, tables, models)
        if item["kind"] in {"has_many", "has_one"} and _option_symbol(item["code"], "through"):
            if target and target in tables and source in tables and not _framework(target):
                through.append(_Edge(
                    source,
                    target,
                    "through",
                    item["name"],
                    through=_option_symbol(item["code"], "through"),
                    path=item["path"],
                    line=item["line"],
                ))
            else:
                unresolved += 1
            continue
        if not target or target not in tables or _framework(target) or source not in tables:
            unresolved += 1
            continue
        if item["kind"] == "has_and_belongs_to_many":
            through.append(_Edge(
                source,
                target,
                "many_to_many",
                item["name"],
                path=item["path"],
                line=item["line"],
            ))
            continue
        _merge_direct(merged, model, item, source, target, models)
    for name, table in polymorphic.items():
        tables[name] = table
    _foreign_key_edges(tables, merged, schema_path)
    edges = list(merged.values()) + through
    return edges, unresolved


def _foreign_key_edges(tables: dict[str, _Table], merged: dict[tuple[str, str, str], _Edge], path: str) -> None:
    """A foreign key in the schema is an association even when no model declares it."""
    for table in tables.values():
        for column in table.columns:
            if not column.references or column.references not in tables or _framework(column.references):
                continue
            key = (table.name, column.references, column.name)
            if key in merged:
                continue
            label = column.name[: -len("_id")] if column.name.endswith("_id") else column.name
            merged[key] = _Edge(
                table.name,
                column.references,
                "many",
                _singularize(label),
                column=column.name,
                path=table.path or path,
                line=table.line,
            )


def _polymorphic_belongs(model, item, tables, models, polymorphic, merged) -> None:
    source = _table_for(model.name, models)
    if not source or source not in tables:
        return
    interface = item["name"]
    node = f"poly:{interface}"
    if node not in polymorphic:
        polymorphic[node] = _Table(name=node, polymorphic=True, path=item["path"], line=item["line"])
    key = (source, node, interface)
    merged[key] = _Edge(
        source,
        node,
        "polymorphic",
        interface,
        column=f"{interface}_id",
        path=item["path"],
        line=item["line"],
    )


def _polymorphic_inverse(model, item, tables, models, polymorphic, merged) -> None:
    interface = _option_symbol(item["code"], "as")
    node = f"poly:{interface}"
    if node not in polymorphic:
        polymorphic[node] = _Table(name=node, polymorphic=True, path=item["path"], line=item["line"])
    owner = _table_for(model.name, models)
    if owner and model.name not in polymorphic[node].models:
        polymorphic[node].models.append(model.name)
    other = _class_named(_classify(_singularize(item["name"])), models)
    if other is None:
        return
    source = _table_for(other.name, models)
    if not source or source not in tables:
        return
    key = (source, node, interface)
    if key not in merged:
        merged[key] = _Edge(
            source,
            node,
            "polymorphic",
            interface,
            column=f"{interface}_id",
            path=item["path"],
            line=item["line"],
        )


def _merge_direct(merged, model, item, source: str, target: str, models: dict[str, _Model]) -> None:
    if item["kind"] == "belongs_to":
        column = _foreign_key(item["code"]) or f"{item['name']}_id"
        owner_table = source
        other_table = target
        belongs_name = item["name"]
        inverse_name = ""
        has_one = False
        has_many = False
    else:
        column = _foreign_key(item["code"]) or f"{_singularize(source)}_id"
        owner_table = target
        other_table = source
        belongs_name = ""
        inverse_name = item["name"]
        has_one = item["kind"] == "has_one"
        has_many = item["kind"] == "has_many"
    key = (owner_table, other_table, column)
    edge = merged.get(key)
    optional = _optional(item["code"])
    if edge is None:
        edge = _Edge(
            owner_table,
            other_table,
            "many",
            belongs_name or _singularize(column[: -len("_id")]) if column.endswith("_id") else column,
            inverse=inverse_name,
            column=column,
            optional=bool(optional),
            path=item["path"],
            line=item["line"],
            has_many=has_many,
            has_one=has_one,
        )
        _set_kind(edge)
        merged[key] = edge
        return
    if belongs_name and belongs_name not in _split(edge.label):
        edge.label = ", ".join(part for part in (edge.label, belongs_name) if part)
        edge.path = item["path"]
        edge.line = item["line"]
    if inverse_name and inverse_name not in _split(edge.inverse):
        edge.inverse = ", ".join(part for part in (edge.inverse, inverse_name) if part)
    edge.has_many = edge.has_many or has_many
    edge.has_one = edge.has_one or has_one
    _set_kind(edge)
    if optional is True:
        edge.optional = True
    _ = model


def _set_kind(edge: _Edge) -> None:
    # A belongs_to on its own is many-to-one. has_one narrows that to one-to-one
    # unless a has_many uses the same foreign key.
    edge.kind = "many" if edge.has_many or not edge.has_one else "one"


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _association_table(item: dict, tables: dict[str, _Table], models: dict[str, _Model]) -> str:
    named = _CLASS_NAME.search(item["code"])
    if named:
        class_name = named.group(1) or named.group(2)
        return _table_for(class_name.split("::")[-1], models) or ""
    if item["name"] in tables:
        return item["name"]
    candidate = _classify(_singularize(item["name"]))
    if candidate in models or _class_named(candidate, models) is not None:
        return _table_for(candidate, models) or ""
    table_name = _tableize(candidate)
    if table_name in tables:
        return table_name
    return ""


def _mark_references(tables: dict[str, _Table], edges: list[_Edge]) -> None:
    for edge in edges:
        if not edge.column or edge.kind in {"through", "polymorphic", "many_to_many"}:
            continue
        table = tables.get(edge.source)
        if table is None:
            continue
        for column in table.columns:
            if column.name == edge.column:
                column.references = edge.target


def _table_for(class_name: str, models: dict[str, _Model]) -> str:
    seen: set[str] = set()
    current = class_name.split("::")[-1]
    while current and current not in seen:
        seen.add(current)
        if _is_root(current):
            return ""
        model = models.get(current)
        if model is None:
            return _tableize(current)
        if model.table_name:
            return model.table_name
        if model.abstract or _is_root(model.parent):
            return _tableize(current)
        current = model.parent.split("::")[-1]
    return ""


def _record_chain(model: _Model, models: dict[str, _Model]) -> bool:
    seen: set[str] = set()
    current = model
    while current and current.name not in seen:
        seen.add(current.name)
        if _is_root(current.parent):
            return True
        parent = models.get(current.parent.split("::")[-1])
        if parent is None:
            return False
        current = parent
    return False


def _class_named(name: str, models: dict[str, _Model]) -> _Model | None:
    found = models.get(name)
    if found is not None:
        return found
    return models.get(name.split("::")[-1])


def _is_root(name: str) -> bool:
    return name in _ROOTS or name.endswith("::ApplicationRecord")


def _framework(name: str) -> bool:
    return name in _FRAMEWORK_NAMES or name.startswith(_FRAMEWORK_PREFIXES)


def _foreign_key(code: str) -> str:
    match = _FOREIGN_KEY.search(code)
    if match is None:
        return ""
    return match.group("quoted") or match.group("symbol") or ""


def _option_symbol(code: str, key: str) -> str:
    match = re.search(rf"{key}:\s*:(\w+)", code)
    return match.group(1) if match else ""


def _optional(code: str) -> bool | None:
    match = re.search(r"optional:\s*(true|false)", code)
    if match is None:
        return None
    return match.group(1) == "true"


def _tableize(class_name: str) -> str:
    return _pluralize(_underscore(class_name.split("::")[-1]))


def _underscore(name: str) -> str:
    underscored = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    underscored = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", underscored)
    return underscored.lower()


def _pluralize(word: str) -> str:
    if "_" in word:
        head, _, tail = word.rpartition("_")
        return f"{head}_{_pluralize(tail)}"
    if word in _IRREGULAR_PLURAL:
        return _IRREGULAR_PLURAL[word]
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"


def _singularize(word: str) -> str:
    if "_" in word:
        head, _, tail = word.rpartition("_")
        return f"{head}_{_singularize(tail)}"
    if word in _IRREGULAR_SINGULAR:
        return _IRREGULAR_SINGULAR[word]
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _classify(word: str) -> str:
    return "".join(part.capitalize() for part in word.split("_") if part)


def _strip_comment(line: str) -> str:
    out: list[str] = []
    quote = ""
    for index, char in enumerate(line):
        if quote:
            out.append(char)
            if char == quote and line[index - 1] != "\\":
                quote = ""
            continue
        if char in "'\"":
            quote = char
            out.append(char)
            continue
        if char == "#":
            break
        out.append(char)
    return "".join(out)
