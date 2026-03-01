# python seed-gen-basic.py --prefix 5Gurl --count 5
# python seed-gen-basic.py --suffix abc --count 3 --case-sensitive
# python seed-gen-basic.py --prefix Test --suffix XYZ --count 1 --output vanity.csv

import argparse
import csv
import sys
import time
from pathlib import Path

import bittensor
from loguru import logger

logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")

DEFAULT_OUTPUT = "keys.csv"
SS58_PREFIX_LEN = 2  # skip network prefix (e.g. "5F", "5C")
LOG_EVERY = 10_000
BASE58_ALPHABET = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def validate_vanity(value: str, label: str):
    invalid = set(value) - BASE58_ALPHABET
    if invalid:
        raise ValueError(
            f"Invalid {label} '{value}' — characters {invalid} are not in Base58 "
            f"(SS58 addresses cannot contain: 0, O, I, l)"
        )


def matches(address: str, prefix: str | None, suffix: str | None, case_sensitive: bool) -> bool:
    addr = address if case_sensitive else address.lower()
    if prefix:
        p = prefix if case_sensitive else prefix.lower()
        if not addr[SS58_PREFIX_LEN:].startswith(p):
            return False
    if suffix:
        s = suffix if case_sensitive else suffix.lower()
        if not addr.endswith(s):
            return False
    return True


def main(prefix: str | None, suffix: str | None, count: int, case_sensitive: bool, output: Path):
    logger.info(f"Prefix         : {prefix or '-'}")
    logger.info(f"Suffix         : {suffix or '-'}")
    logger.info(f"Case sensitive : {case_sensitive}")
    logger.info(f"Count          : {count}")
    logger.info(f"Output         : {output}")

    write_header = not output.exists() or output.stat().st_size == 0

    with open(output, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["address", "mnemonic"])

        generated = 0
        attempts = 0
        t0 = time.monotonic()

        while generated < count:
            mnemonic = bittensor.Keypair.generate_mnemonic(n_words=24)
            keypair = bittensor.Keypair.create_from_mnemonic(mnemonic)
            attempts += 1

            if attempts % LOG_EVERY == 0:
                rate = attempts / (time.monotonic() - t0)
                logger.info(f"{attempts:,} tested | {generated}/{count} found | {rate:.0f} addr/s")

            if matches(keypair.ss58_address, prefix, suffix, case_sensitive):
                generated += 1
                writer.writerow([keypair.ss58_address, mnemonic])
                f.flush()
                elapsed = time.monotonic() - t0
                logger.success(
                    f"[{generated}/{count}] {keypair.ss58_address} "
                    f"({attempts:,} attempts, {elapsed:.1f}s)"
                )

    elapsed = time.monotonic() - t0
    rate = attempts / elapsed if elapsed > 0 else 0
    logger.info(f"Done — {count} addresses in {elapsed:.1f}s ({attempts:,} attempts, {rate:.0f} addr/s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate vanity SS58 addresses with prefix/suffix matching")
    parser.add_argument("--prefix", type=str, default=None, help="Desired prefix (after SS58 network prefix)")
    parser.add_argument("--suffix", type=str, default=None, help="Desired suffix")
    parser.add_argument("--count", type=int, default=5, help="Number of addresses to generate (default: 5)")
    parser.add_argument("--case-sensitive", action="store_true", help="Case-sensitive matching (default: case-insensitive)")
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT, help=f"Output CSV file (default: {DEFAULT_OUTPUT})")
    args = parser.parse_args()

    if not args.prefix and not args.suffix:
        parser.error("At least one of --prefix or --suffix is required")

    try:
        if args.prefix:
            validate_vanity(args.prefix, "prefix")
        if args.suffix:
            validate_vanity(args.suffix, "suffix")
    except ValueError as e:
        parser.error(str(e))

    main(
        prefix=args.prefix,
        suffix=args.suffix,
        count=args.count,
        case_sensitive=args.case_sensitive,
        output=Path(args.output),
    )
