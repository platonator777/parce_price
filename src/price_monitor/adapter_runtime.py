from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .adapter_api import RawOffer


ALLOWED_IMPORTS = {"json", "re", "math", "html", "typing", "collections", "price_monitor.adapter_api", "price_monitor.adapter_helpers", "price_monitor.source"}
FORBIDDEN_CALLS = {"open", "exec", "eval", "compile", "input", "breakpoint", "__import__", "globals", "locals", "vars"}
FORBIDDEN_ATTRS = {"system", "popen", "spawn", "fork", "connect", "urlopen", "request", "remove", "unlink", "rmdir", "write_text", "write_bytes"}


@dataclass(slots=True)
class StaticCheck:
    ok: bool
    errors: list[str]


@dataclass(slots=True)
class ExecutionResult:
    ok: bool
    offers: list[dict[str, Any]]
    error: str | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


def extract_python_code(response: str) -> str:
    blocks = list(__import__("re").finditer(r"```(?:python)?\s*(.*?)```", response, __import__("re").I | __import__("re").S))
    code = blocks[-1].group(1).strip() if blocks else response.strip()
    start = min((position for position in (code.find("from "), code.find("import "), code.find("def parse")) if position >= 0), default=0)
    return code[start:].strip()


def check_adapter_source(code: str) -> StaticCheck:
    errors: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return StaticCheck(False, [f"syntax error: {exc}"])
    parse_functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "parse"]
    if len(parse_functions) != 1 or isinstance(parse_functions[0], ast.AsyncFunctionDef):
        errors.append("adapter must define exactly one synchronous parse(record) function")
    elif len(parse_functions[0].args.args) != 1:
        errors.append("parse must accept exactly one positional argument")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_IMPORTS and not any(alias.name.startswith(item + ".") for item in ALLOWED_IMPORTS):
                    errors.append(f"import not allowed: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module not in ALLOWED_IMPORTS:
                errors.append(f"import not allowed: {module}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                errors.append(f"call not allowed: {node.func.id}")
            if isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_ATTRS:
                errors.append(f"attribute call not allowed: {node.func.attr}")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            errors.append(f"dunder access not allowed: {node.attr}")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            errors.append("global/nonlocal state is not allowed")
    return StaticCheck(not errors, list(dict.fromkeys(errors)))


def run_adapter(adapter_path: Path, record, *, timeout_seconds: float = 8.0, max_output_chars: int = 12_000) -> ExecutionResult:
    check = check_adapter_source(adapter_path.read_text(encoding="utf-8"))
    if not check.ok:
        return ExecutionResult(False, [], "; ".join(check.errors))
    request = json.dumps({"adapter": str(adapter_path.resolve()), "record": asdict(record)}, ensure_ascii=False)
    worker = Path(__file__).with_name("adapter_worker.py")
    env = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"}}
    try:
        with tempfile.TemporaryDirectory(prefix="price-adapter-") as directory:
            completed = subprocess.run(
                [sys.executable, "-I", str(worker)], input=request, text=True, encoding="utf-8",
                capture_output=True, timeout=timeout_seconds, cwd=directory, env=env,
            )
    except subprocess.TimeoutExpired as exc:
        return ExecutionResult(False, [], "adapter timed out", (exc.stdout or "")[-max_output_chars:], (exc.stderr or "")[-max_output_chars:], True)
    full_stdout = completed.stdout
    stdout, stderr = full_stdout[-max_output_chars:], completed.stderr[-max_output_chars:]
    if completed.returncode != 0:
        return ExecutionResult(False, [], f"adapter process exited {completed.returncode}", stdout, stderr)
    try:
        result = json.loads(full_stdout)
        if not isinstance(result, list):
            raise ValueError("parse result is not a list")
        offers = [RawOffer(**item).to_dict() for item in result if isinstance(item, dict)]
        if len(offers) != len(result):
            raise ValueError("every list item must be an offer object")
        return ExecutionResult(True, offers, stderr=stderr)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return ExecutionResult(False, [], f"invalid serialized result: {exc}", stdout, stderr)
