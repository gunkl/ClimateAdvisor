"""Shared package walker and literal-at-sink finder for the translation guards (Issue #1341).

One module defines *where occupant-facing text may appear* (the sink list below), so the ratchet guards in
``tools/i18n_checks.py`` and the tests that exercise them cannot drift apart. Pure standard library; it works in a
bare interpreter (``tools/i18n_checks.py`` loads it by file path).

Finders return :class:`Hit` rows ``(file, line, sink, text)`` where ``file`` is relative to the integration
directory (``automation.py``). Counting per file is what the ratchet baselines store.

Heuristic ("is this English prose?"): two or more alphabetic words, or, at a label sink, one capitalised word of
three or more letters. Identifiers, dotted keys, snake_case, entity ids and unit-only text never match.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
PACKAGE_DIR = REPO / "custom_components" / "climate_advisor"
FRONTEND_DIR = PACKAGE_DIR / "frontend"

# Names that are never translated (brand, product).
BRAND_TEXT = frozenset({"Climate Advisor"})

# Python files whose job is to READ English that the integration itself stored or that another system wrote (stored
# records, the investigator, captured log lines). They may match English prose by design (spec D4).
STORED_ENGLISH_READERS = frozenset({"ai_skills_investigator.py", "log_capture.py"})
# Outside the package (not scanned here, listed so the spec and the guard agree): tools/sim_harness/outcomes.py.

# Python files that ARE the formatters: they may hold unit symbols and strftime.
FORMATTER_FILES = frozenset({"i18n.py", "temperature.py", "time_utils.py"})

# ---- Python sink definitions (the one list) ----------------------------------------------------------------------
# call name -> (positional indexes, keyword names) whose literal text is shown to the occupant.
CALL_SINKS: dict[str, tuple[tuple[int, ...], tuple[str, ...]]] = {
    "_notify": ((0, 1), ("title", "message")),
    "_notify_in_background": ((0, 1), ("title", "message")),
    "_notify_whf_safety": ((0, 1), ("title", "message")),
    "_notify_indoor_blind": ((0, 1), ("title", "message")),
    "SelectOptionDict": ((), ("label",)),
    "ServiceValidationError": ((0,), ("message",)),
    "HomeAssistantError": ((0,), ("message",)),
}
# modules whose dict literals are API payloads (prose / error / message / detail fields)
API_MODULES = frozenset({"api.py", "dashboard_api.py"})
API_FIELDS = frozenset({"error", "message", "detail", "prose", "text", "summary", "description", "title"})
# display keys of the event payload handed to ``_emit_event``
EVENT_DISPLAY_KEYS = frozenset({"title", "message", "text", "summary", "label", "description", "detail"})
LOGGER_NAMES = frozenset({"_LOGGER", "logger", "LOGGER", "log", "_log"})
LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "log"})


@dataclass(frozen=True)
class Hit:
    file: str
    line: int
    sink: str
    text: str

    def __str__(self) -> str:
        shown = self.text if len(self.text) <= 60 else self.text[:57] + "..."
        return f"{self.file}:{self.line} [{self.sink}] {shown!r}"


# ---- walking -------------------------------------------------------------------------------------------------------
def package_files(package_dir: Path | None = None) -> list[Path]:
    """Every ``.py`` file of the integration, sorted."""
    return sorted((package_dir or PACKAGE_DIR).rglob("*.py"))


_TREE_CACHE: dict[tuple[str, int, int], ast.Module] = {}


def parse_file(path: Path) -> ast.Module:
    """Parse ``path`` (cached by path, mtime and size)."""
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    tree = _TREE_CACHE.get(key)
    if tree is None:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _TREE_CACHE[key] = tree
    return tree


def parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def iter_package(finder: Callable[[ast.Module, str], list[Hit]], package_dir: Path | None = None) -> list[Hit]:
    """Run ``finder(tree, relative_name)`` over every package module."""
    root = package_dir or PACKAGE_DIR
    hits: list[Hit] = []
    for path in package_files(root):
        hits.extend(finder(parse_file(path), path.relative_to(root).as_posix()))
    return hits


# ---- literal text --------------------------------------------------------------------------------------------------
def literal_text(node: ast.AST | None) -> str | None:
    """The constant text of a string expression (f-strings keep their literal parts, ``{}`` for holes)."""
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            parts.append(value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else "{}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        left, right = literal_text(node.left), literal_text(node.right)
        if left is None and right is None:
            return None
        return (left or "{}") + ("" if isinstance(node.op, ast.Mod) else (right or "{}"))
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and isinstance(node.func.value, (ast.Constant, ast.JoinedStr))
    ):
        return literal_text(node.func.value)
    return None


_WORD = re.compile(r"[A-Za-z][A-Za-z'’]*")
_PLACEHOLDER = re.compile(r"\{[^{}]*\}|%[-+ 0#]*\d*(?:\.\d+)?[sdifr]")
_SINGLE_WORD = re.compile(r"[A-Z][A-Za-z'’-]{2,}")


def is_english_prose(text: str | None, min_words: int = 2) -> bool:
    """True when ``text`` reads as English meant for a person (see the module docstring)."""
    if not text:
        return False
    cleaned = _PLACEHOLDER.sub(" ", text).strip()
    if not cleaned or cleaned in BRAND_TEXT:
        return False
    words = [w for w in _WORD.findall(cleaned) if len(w) >= 2]
    if len(words) >= max(min_words, 2):
        # reject identifier soup such as "a_b c_d" or dotted keys: require real spaces between the words
        return " " in cleaned
    if min_words <= 1 and len(words) == 1:
        return bool(_SINGLE_WORD.fullmatch(cleaned))
    return False


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _in_log_call(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute) and cur.func.attr in LOG_METHODS:
            base = cur.func.value
            name = base.id if isinstance(base, ast.Name) else base.attr if isinstance(base, ast.Attribute) else ""
            if name in LOGGER_NAMES or name.lower().endswith("logger"):
                return True
        cur = parents.get(cur)
    return False


def _dict_items(node: ast.Dict) -> Iterator[tuple[str, ast.AST]]:
    for key, value in zip(node.keys, node.values, strict=True):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            yield key.value, value


# ---- finder 1: English literals at occupant sinks -------------------------------------------------------------------
def find_literals_at_sinks(tree: ast.Module, rel: str) -> list[Hit]:
    """Hard-coded English at the Python occupant sinks (plan section 4 item 3)."""
    hits: list[Hit] = []
    parents = parent_map(tree)

    def add(node: ast.AST, sink: str, text: str | None, min_words: int = 1) -> None:
        if is_english_prose(text, min_words):
            hits.append(Hit(rel, getattr(node, "lineno", 0), sink, text or ""))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in CALL_SINKS:
                positions, keywords = CALL_SINKS[name]
                for idx in positions:
                    if idx < len(node.args):
                        add(node.args[idx], name, literal_text(node.args[idx]))
                for kw in node.keywords:
                    if kw.arg in keywords:
                        add(kw.value, f"{name}({kw.arg}=)", literal_text(kw.value))
            if name == "async_create_issue":
                for kw in node.keywords:
                    if kw.arg == "translation_placeholders" and isinstance(kw.value, ast.Dict):
                        for key, value in _dict_items(kw.value):
                            add(value, f"async_create_issue.placeholder[{key}]", literal_text(value))
            if name == "_emit_event":
                payloads = [a for a in node.args[1:2] if isinstance(a, ast.Dict)]
                payloads += [kw.value for kw in node.keywords if kw.arg == "data" and isinstance(kw.value, ast.Dict)]
                for payload in payloads:
                    for key, value in _dict_items(payload):
                        if key in EVENT_DISPLAY_KEYS:
                            add(value, f"_emit_event[{key}]", literal_text(value), 2)
            for kw in node.keywords:
                if kw.arg == "detail":
                    add(kw.value, "detail=", literal_text(kw.value), 2)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                tname = target.id if isinstance(target, ast.Name) else getattr(target, "attr", None)
                if tname == "_attr_name":
                    add(node, "_attr_name", literal_text(node.value))
        elif isinstance(node, ast.Dict) and rel in API_MODULES:
            for key, value in _dict_items(node):
                if key in API_FIELDS and not _in_log_call(node, parents):
                    add(value, f"api[{key}]", literal_text(value))

    hits.extend(_event_renderer_hits(tree, rel))
    hits.extend(_strftime_hits(tree, rel, parents))
    hits.extend(_unit_symbol_hits(tree, rel, parents))
    return sorted(set(hits), key=lambda h: (h.file, h.line, h.sink, h.text))


def _event_renderer_hits(tree: ast.Module, rel: str) -> list[Hit]:
    """Prose returned by the functions registered in ``EVENT_RENDERERS``."""
    registry: ast.Dict | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "EVENT_RENDERERS" for t in targets) and isinstance(
            node.value, ast.Dict
        ):
            registry = node.value
    if registry is None:
        return []
    names = {v.id for v in registry.values if isinstance(v, ast.Name)}
    funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    hits: list[Hit] = []
    for func in funcs:
        for node in ast.walk(func):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            values = node.value.elts if isinstance(node.value, ast.Tuple) else [node.value]
            for value in values:
                text = literal_text(value)
                if is_english_prose(text, 2):
                    hits.append(Hit(rel, node.lineno, f"EVENT_RENDERERS.{func.name}", text or ""))
    return hits


def _strftime_hits(tree: ast.Module, rel: str, parents: dict[ast.AST, ast.AST]) -> list[Hit]:
    if rel in FORMATTER_FILES:
        return []
    return [
        Hit(rel, node.lineno, "strftime", "strftime(...)")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "strftime"
        and not _in_log_call(node, parents)
    ]


def _unit_symbol_hits(tree: ast.Module, rel: str, parents: dict[ast.AST, ast.AST]) -> list[Hit]:
    """Bare degree-unit symbols in string literals outside the formatters (docstrings and log calls excluded)."""
    if rel in FORMATTER_FILES:
        return []
    hits: list[Hit] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if "°F" not in node.value and "°C" not in node.value:
            continue
        if isinstance(parents.get(node), ast.Expr):  # docstring / bare string statement
            continue
        if _in_log_call(node, parents):
            continue
        hits.append(Hit(rel, node.lineno, "unit-symbol", node.value))
    return hits


# ---- finder 2: code that compares English prose ---------------------------------------------------------------------
_REGEX_FUNCS = frozenset({"search", "match", "fullmatch", "compile", "findall", "sub", "split", "finditer"})
_STR_MATCH = frozenset({"startswith", "endswith"})


def _prose_args(node: ast.AST, min_words: int) -> list[str]:
    if isinstance(node, ast.Tuple):
        out: list[str] = []
        for elt in node.elts:
            out.extend(_prose_args(elt, min_words))
        return out
    text = literal_text(node) if isinstance(node, (ast.Constant, ast.JoinedStr)) else None
    return [text] if text and is_english_prose(text, min_words) else []


def find_prose_comparisons(tree: ast.Module, rel: str) -> list[Hit]:
    """``startswith``/``endswith``/``in``/``==``/regex against literal English prose (plan section 4 item 4)."""
    if rel in STORED_ENGLISH_READERS:
        return []
    hits: list[Hit] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in _STR_MATCH and node.args:
                hits.extend(Hit(rel, node.lineno, attr, text) for text in _prose_args(node.args[0], 1))
            elif (
                attr in _REGEX_FUNCS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "re"
                and node.args
            ):
                hits.extend(Hit(rel, node.lineno, f"re.{attr}", text) for text in _prose_args(node.args[0], 2))
        elif isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            for op, left, right in zip(node.ops, operands, operands[1:], strict=False):
                if not isinstance(op, (ast.In, ast.NotIn, ast.Eq, ast.NotEq)):
                    continue
                for const, other in ((left, right), (right, left)):
                    if isinstance(other, (ast.Constant, ast.List, ast.Tuple, ast.Set, ast.Dict, ast.JoinedStr)):
                        continue
                    text = literal_text(const) if isinstance(const, ast.Constant) else None
                    if text and is_english_prose(text, 2):
                        hits.append(Hit(rel, node.lineno, type(op).__name__.lower(), text))
    return sorted(set(hits), key=lambda h: (h.file, h.line, h.sink, h.text))


def counts_by_file(hits: list[Hit]) -> dict[str, int]:
    """``{file: number of hits}`` (what a ratchet baseline stores)."""
    out: dict[str, int] = {}
    for hit in hits:
        out[hit.file] = out.get(hit.file, 0) + 1
    return dict(sorted(out.items()))
