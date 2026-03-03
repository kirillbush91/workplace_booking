"""
Simple encrypted vault for `.env`.

Goal:
- keep plaintext `.env` out of git
- keep encrypted `.env.enc` in git
- keep the symmetric key outside the repo

Default key path:
- Windows: %USERPROFILE%\\.workplace_booking_env.key
- Linux/macOS: ~/.workplace_booking_env.key

Usage:
  python scripts/env_vault.py init-key
  python scripts/env_vault.py encrypt
  python scripts/env_vault.py decrypt
  python scripts/env_vault.py status
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


AAD = b"workplace_booking.env.v1"


def default_key_path() -> Path:
    return Path.home() / ".workplace_booking_env.key"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_private_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def create_key(path: Path, force: bool) -> None:
    if path.exists() and not force:
        raise SystemExit(f"Key already exists: {path}")
    raw = os.urandom(32)
    encoded = base64.urlsafe_b64encode(raw) + b"\n"
    write_private_file(path, encoded)
    print(f"Created key: {path}")


def load_key(path: Path) -> bytes:
    if not path.exists():
        raise SystemExit(f"Key file not found: {path}")
    raw = path.read_bytes().strip()
    try:
        decoded = base64.urlsafe_b64decode(raw)
    except Exception as exc:
        raise SystemExit(f"Invalid key file format: {path}: {exc}") from exc
    if len(decoded) != 32:
        raise SystemExit(f"Invalid key length in {path}; expected 32 bytes.")
    return decoded


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encrypt_env(env_path: Path, enc_path: Path, key_path: Path) -> None:
    if not env_path.exists():
        raise SystemExit(f"Env file not found: {env_path}")
    plaintext = env_path.read_bytes()
    key = load_key(key_path)
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, AAD)
    payload = {
        "version": 1,
        "algorithm": "AES-256-GCM",
        "created_at_utc": utc_now(),
        "aad": base64.urlsafe_b64encode(AAD).decode("ascii"),
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
        "sha256_plaintext": sha256_hex(plaintext),
        "source_name": env_path.name,
    }
    enc_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Encrypted {env_path} -> {enc_path}")


def decrypt_env(enc_path: Path, env_path: Path, key_path: Path, force: bool) -> None:
    if not enc_path.exists():
        raise SystemExit(f"Encrypted file not found: {enc_path}")
    if env_path.exists() and not force:
        raise SystemExit(f"Output file already exists: {env_path}. Use --force to overwrite.")
    payload = json.loads(enc_path.read_text(encoding="utf-8"))
    nonce = base64.urlsafe_b64decode(payload["nonce"])
    ciphertext = base64.urlsafe_b64decode(payload["ciphertext"])
    key = load_key(key_path)
    plaintext = AESGCM(key).decrypt(nonce, ciphertext, AAD)
    expected_sha = payload.get("sha256_plaintext")
    actual_sha = sha256_hex(plaintext)
    if expected_sha and expected_sha != actual_sha:
        raise SystemExit("Plaintext SHA256 mismatch after decryption.")
    env_path.write_bytes(plaintext)
    print(f"Decrypted {enc_path} -> {env_path}")


def print_status(env_path: Path, enc_path: Path, key_path: Path) -> None:
    print(f"env_path={env_path} exists={env_path.exists()}")
    print(f"enc_path={enc_path} exists={enc_path.exists()}")
    print(f"key_path={key_path} exists={key_path.exists()}")
    if enc_path.exists():
        try:
            payload = json.loads(enc_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"enc_parse_error={exc}")
            return
        print(f"enc_version={payload.get('version')}")
        print(f"enc_algorithm={payload.get('algorithm')}")
        print(f"enc_created_at_utc={payload.get('created_at_utc')}")
        print(f"enc_sha256_plaintext={payload.get('sha256_plaintext')}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Encrypt/decrypt .env for git storage.")
    parser.add_argument("--env", default=".env", help="Plaintext env path.")
    parser.add_argument("--enc", default=".env.enc", help="Encrypted env path.")
    parser.add_argument(
        "--key-file",
        default=str(default_key_path()),
        help="Symmetric key file path (must stay outside git).",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    init_key = sub.add_parser("init-key", help="Create a new symmetric key.")
    init_key.add_argument("--force", action="store_true", help="Overwrite existing key.")

    sub.add_parser("encrypt", help="Encrypt .env -> .env.enc")

    decrypt = sub.add_parser("decrypt", help="Decrypt .env.enc -> .env")
    decrypt.add_argument("--force", action="store_true", help="Overwrite existing output file.")

    sub.add_parser("status", help="Show env/key/encrypted file status.")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    env_path = Path(args.env)
    enc_path = Path(args.enc)
    key_path = Path(args.key_file)

    if args.command == "init-key":
        create_key(path=key_path, force=args.force)
        return 0
    if args.command == "encrypt":
        encrypt_env(env_path=env_path, enc_path=enc_path, key_path=key_path)
        return 0
    if args.command == "decrypt":
        decrypt_env(
            enc_path=enc_path,
            env_path=env_path,
            key_path=key_path,
            force=args.force,
        )
        return 0
    if args.command == "status":
        print_status(env_path=env_path, enc_path=enc_path, key_path=key_path)
        return 0
    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
