#!/usr/bin/env python3
"""Pretty print a JWT body. No signature check. Teaching aid only.

Usage:
  python scripts/decode.py <token>
  echo <token> | python scripts/decode.py
"""
import base64
import json
import sys


def _seg(s: str) -> dict:
    s += "=" * (-len(s) % 4)
    return json.loads(base64.urlsafe_b64decode(s))


def main() -> None:
    token = (sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()).strip()
    parts = token.split(".")
    if len(parts) < 2:
        print("not a JWT")
        sys.exit(1)
    body = _seg(parts[1])
    for key in ("sub", "act", "aud", "azp", "scope", "iss", "exp"):
        print(f"{key:6} = {body.get(key)}")
    print("---- full body ----")
    print(json.dumps(body, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
