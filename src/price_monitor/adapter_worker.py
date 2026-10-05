from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

# -I deliberately removes the project from sys.path. Add only the package's src root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from price_monitor.source import SourceRecord


def main() -> None:
    request = json.loads(sys.stdin.read())
    path = Path(request["adapter"])
    spec = importlib.util.spec_from_file_location("generated_adapter", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load adapter")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    record = SourceRecord(**request["record"])
    result = module.parse(record)
    if not isinstance(result, list):
        raise TypeError("parse() must return a list")
    serializable = [asdict(item) if is_dataclass(item) else item for item in result]
    sys.stdout.write(json.dumps(serializable, ensure_ascii=False))


if __name__ == "__main__":
    main()
