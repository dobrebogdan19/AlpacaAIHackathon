"""Manual check: generate strategies against the live LLM and print them.

Not part of the test suite (it hits the network). Run from the repo root:

    python scripts/generate_demo.py [N]

Requires OPENAI_API_KEY in .env.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import generator  # noqa: E402


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    result = generator.generate(n=n)

    print(f"attempts={result.attempts}  "
          f"valid={len(result.strategies)}  "
          f"duplicates_collapsed={result.duplicates_collapsed}  "
          f"failures={len(result.failures)}\n")

    for i, s in enumerate(result.strategies, 1):
        print(f"--- {i}. {s.name}  [{s.symbol}] ---")
        print(f"    entry: {json.dumps(json.loads(s.entry.model_dump_json()))}")
        print(f"    exit : {json.dumps(json.loads(s.exit.model_dump_json()))}")
        print(f"    why  : {s.rationale}\n")

    for f in result.failures:
        print(f"[FAILED] {f['error']}\n         {json.dumps(f['raw'])[:400]}")


if __name__ == "__main__":
    main()
