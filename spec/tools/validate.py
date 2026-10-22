#!/usr/bin/env python3
"""Validate AHP v1 schemas and examples.

  python3 spec/tools/validate.py                 # schemas well-formed + spec/examples
  python3 spec/tools/validate.py --jsonl FILE    # lines from legacy_to_ahp.py

Without the `jsonschema` package only the JSON well-formedness check runs.
Example file naming: <kind>.<anything>.json where kind is envelope|reply|ack|capabilities|consumer;
*.jsonl files are envelope-per-line logs. Files under examples/invalid/ MUST fail.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SPEC = Path(__file__).resolve().parent.parent
SCHEMAS = SPEC / "schemas"
BASE = "https://ahp.invalid/schemas/1/"


def load_all():
    docs = {}
    for path in sorted(SCHEMAS.rglob("*.schema.json")):
        docs[path] = json.loads(path.read_text(encoding="utf-8"))  # raises on malformed JSON
    return docs


def make_validators(docs):
    try:
        import jsonschema
        from referencing import Registry, Resource
    except ImportError:
        return None
    registry = Registry().with_resources(
        (doc["$id"], Resource.from_contents(doc)) for doc in docs.values()
    )
    out = {}
    for path, doc in docs.items():
        jsonschema.Draft202012Validator.check_schema(doc)
        name = path.name.replace(".schema.json", "")
        out[name] = jsonschema.Draft202012Validator(doc, registry=registry,
                                                    format_checker=jsonschema.FormatChecker())
    return out


def errors_for(validator, instance):
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
            for e in validator.iter_errors(instance)]


def main(argv):
    docs = load_all()
    print(f"schemas well-formed: {len(docs)}")
    validators = make_validators(docs)
    if validators is None:
        print("jsonschema not installed: instance validation skipped")
        return 0
    failed = 0
    checked = 0
    if "--jsonl" in argv:
        path = Path(argv[argv.index("--jsonl") + 1])
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            item = json.loads(line)
            errs = errors_for(validators[item["kind"]], item["doc"])
            checked += 1
            if errs:
                failed += 1
                print(f"FAIL {item.get('source', n)}: {errs[:3]}")
            else:
                print(f"ok   {item.get('source', n)} -> {item['doc'].get('event', item['kind'])}")
    else:
        for path in sorted((SPEC / "examples").rglob("*.json*")):
            expect_fail = "invalid" in path.parts
            kind = path.name.split(".", 1)[0]
            if path.suffix == ".jsonl":
                instances = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
                kind = "envelope"
            else:
                instances = [json.loads(path.read_text(encoding="utf-8"))]
            for inst in instances:
                errs = errors_for(validators[kind], inst)
                checked += 1
                rel = path.relative_to(SPEC)
                if expect_fail and not errs:
                    failed += 1
                    print(f"FAIL {rel}: expected invalid, passed")
                elif not expect_fail and errs:
                    failed += 1
                    print(f"FAIL {rel}: {errs[:3]}")
                else:
                    print(f"ok   {rel}" + (f"  (rejected as expected: {errs[0][:80]})" if expect_fail else ""))
    print(f"checked {checked}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
