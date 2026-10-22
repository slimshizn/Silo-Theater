"""Small structural validator for the pinned, official OpenAPI projection.

Only the JSON Schema keywords present in the playback request schemas are
needed. This keeps protocol checks independent of the client implementation.
"""

import json
import math
from pathlib import Path


CONTRACT = json.loads((Path(__file__).parent / "fixtures/playback-contract.json").read_text())
SCHEMAS = CONTRACT["schemas"]


def assert_contract(case, value, name):
    def check(current, schema, path):
        if "$ref" in schema:
            schema = SCHEMAS[schema["$ref"].rsplit("/", 1)[-1]]
        kind = schema.get("type")
        types = {
            "object": lambda v: isinstance(v, dict),
            "array": lambda v: isinstance(v, list),
            "string": lambda v: isinstance(v, str),
            "boolean": lambda v: isinstance(v, bool),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v),
        }
        if kind in types:
            case.assertTrue(types[kind](current), "{} must be {}".format(path, kind))
        if "enum" in schema:
            case.assertIn(current, schema["enum"], path)
        if kind == "object":
            for key in schema.get("required", []):
                case.assertIn(key, current, "{} requires {}".format(path, key))
            properties = schema.get("properties", {})
            for key, item in current.items():
                child = properties.get(key)
                if child is None:
                    extra = schema.get("additionalProperties", True)
                    case.assertIsNot(extra, False, "{} forbids {}".format(path, key))
                    child = extra if isinstance(extra, dict) else {}
                check(item, child, "{}.{}".format(path, key))
        elif kind == "array":
            for index, item in enumerate(current):
                check(item, schema.get("items", {}), "{}[{}]".format(path, index))
        elif kind == "string":
            if "minLength" in schema:
                case.assertGreaterEqual(len(current), schema["minLength"], path)
            if "maxLength" in schema:
                case.assertLessEqual(len(current), schema["maxLength"], path)
        elif kind in ("number", "integer"):
            if "minimum" in schema:
                case.assertGreaterEqual(current, schema["minimum"], path)
            if "maximum" in schema:
                case.assertLessEqual(current, schema["maximum"], path)

    check(value, SCHEMAS[name], name)
