from __future__ import annotations

import hashlib
import secrets
import sys


def hash_password(password: str) -> str:
  salt = secrets.token_hex(16)
  iterations = 210_000
  digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations).hex()
  return "pbkdf2_sha256$" + str(iterations) + "$" + salt + "$" + digest


def main() -> None:
  if len(sys.argv) < 2:
    print('Usage: python backend/generate_password_hash.py "YourStrongPassword"')
    raise SystemExit(1)
  print(hash_password(sys.argv[1]))


if __name__ == "__main__":
  main()
