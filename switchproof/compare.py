import hashlib
import json

from deepdiff import DeepDiff


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def compare_rows(expected, actual, ordered=True):
    # Only row order is relaxed. Ordered nested lists remain ordered and duplicates survive.
    left = expected if ordered else sorted(expected, key=canonical)
    right = actual if ordered else sorted(actual, key=canonical)
    diff = DeepDiff(left, right, ignore_order=False, verbose_level=2)
    return {
        "status": "passed" if not diff else "mismatch",
        "expected_rows": len(left),
        "actual_rows": len(right),
        "expected_hash": digest(left),
        "actual_hash": digest(right),
        "diff": json.loads(diff.to_json()),
        "expected_sample": left[:6],
        "actual_sample": right[:6],
    }
