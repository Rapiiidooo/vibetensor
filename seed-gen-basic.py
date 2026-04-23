# python seed-gen-basic.py --prefix 5Gurl --count 5
# python seed-gen-basic.py --suffix abc --count 3 --case-sensitive
# python seed-gen-basic.py --prefix Test --suffix XYZ --count 1 --output vanity.csv
# python seed-gen-basic.py --contains taoswap --count 1 --workers 16

import argparse
import csv
import multiprocessing as mp
import sys
import time
from pathlib import Path

import bittensor
from loguru import logger

logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")

DEFAULT_OUTPUT = "keys.csv"
SS58_PREFIX_LEN = 2  # skip network prefix (e.g. "5F", "5C")
BASE58_ALPHABET = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def validate_vanity(value: str, label: str):
    invalid = set(value) - BASE58_ALPHABET
    if invalid:
        raise ValueError(
            f"Invalid {label} '{value}' — characters {invalid} are not in Base58 "
            f"(SS58 addresses cannot contain: 0, O, I, l)"
        )


def matches(address: str, prefix: str | None, suffix: str | None, contains: str | None, case_sensitive: bool) -> bool:
    addr = address if case_sensitive else address.lower()
    if prefix:
        p = prefix if case_sensitive else prefix.lower()
        if not addr[SS58_PREFIX_LEN:].startswith(p):
            return False
    if suffix:
        s = suffix if case_sensitive else suffix.lower()
        if not addr.endswith(s):
            return False
    if contains:
        c = contains if case_sensitive else contains.lower()
        if c not in addr[SS58_PREFIX_LEN:]:
            return False
    return True


def worker(
    worker_id: int,
    prefix: str | None,
    suffix: str | None,
    contains: str | None,
    case_sensitive: bool,
    found_queue: mp.Queue,
    stop_event,
    attempts_counter,
):
    local_attempts = 0
    last_flush = time.monotonic()

    while not stop_event.is_set():
        mnemonic = bittensor.Keypair.generate_mnemonic(n_words=24)
        keypair = bittensor.Keypair.create_from_mnemonic(mnemonic)
        local_attempts += 1

        if local_attempts % 50 == 0:
            now = time.monotonic()
            if now - last_flush >= 2.0:
                with attempts_counter.get_lock():
                    attempts_counter.value += local_attempts
                local_attempts = 0
                last_flush = now

        if matches(keypair.ss58_address, prefix, suffix, contains, case_sensitive):
            found_queue.put((worker_id, keypair.ss58_address, mnemonic))

    with attempts_counter.get_lock():
        attempts_counter.value += local_attempts


def main(prefix: str | None, suffix: str | None, contains: str | None, count: int, case_sensitive: bool, workers: int, output: Path):
    logger.info(f"Prefix         : {prefix or '-'}")
    logger.info(f"Suffix         : {suffix or '-'}")
    logger.info(f"Contains       : {contains or '-'}")
    logger.info(f"Case sensitive : {case_sensitive}")
    logger.info(f"Count          : {count}")
    logger.info(f"Workers        : {workers}")
    logger.info(f"Output         : {output}")

    write_header = not output.exists() or output.stat().st_size == 0
    if write_header:
        with open(output, mode="w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["address", "mnemonic"])

    stop_event = mp.Event()
    found_queue = mp.Queue()
    attempts_counter = mp.Value("q", 0)

    processes = []
    for i in range(workers):
        p = mp.Process(
            target=worker,
            args=(i, prefix, suffix, contains, case_sensitive, found_queue, stop_event, attempts_counter),
            daemon=True,
        )
        p.start()
        processes.append(p)

    generated = 0
    t0 = time.monotonic()

    try:
        while generated < count:
            while not found_queue.empty() and generated < count:
                wid, address, mnemonic = found_queue.get_nowait()
                generated += 1

                with open(output, mode="a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow([address, mnemonic])

                elapsed = time.monotonic() - t0
                total = attempts_counter.value
                logger.success(
                    f"[{generated}/{count}] [Worker {wid}] {address} "
                    f"({total:,} attempts, {elapsed:.1f}s)"
                )

            if generated >= count:
                break

            elapsed = time.monotonic() - t0
            total = attempts_counter.value
            rate = total / elapsed if elapsed > 0 else 0
            logger.info(f"{total:>12,} tested | {generated}/{count} found | {rate:,.0f} addr/s")
            time.sleep(5)
    except KeyboardInterrupt:
        logger.warning("Interrupted — stopping workers…")

    stop_event.set()
    for p in processes:
        p.join(timeout=5)

    elapsed = time.monotonic() - t0
    total = attempts_counter.value
    rate = total / elapsed if elapsed > 0 else 0
    logger.info(f"Done — {generated} addresses in {elapsed:.1f}s ({total:,} attempts, {rate:,.0f} addr/s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate vanity SS58 addresses with prefix/suffix matching")
    parser.add_argument("--prefix", type=str, default=None, help="Desired prefix (after SS58 network prefix)")
    parser.add_argument("--suffix", type=str, default=None, help="Desired suffix")
    parser.add_argument("--contains", type=str, default=None, help="Substring to find anywhere in the address (after SS58 network prefix)")
    parser.add_argument("--count", type=int, default=5, help="Number of addresses to generate (default: 5)")
    parser.add_argument("--case-sensitive", action="store_true", help="Case-sensitive matching (default: case-insensitive)")
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of worker processes (default: 8)",
    )
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT, help=f"Output CSV file (default: {DEFAULT_OUTPUT})")
    args = parser.parse_args()

    if not args.prefix and not args.suffix and not args.contains:
        parser.error("At least one of --prefix, --suffix or --contains is required")

    try:
        if args.prefix:
            validate_vanity(args.prefix, "prefix")
        if args.suffix:
            validate_vanity(args.suffix, "suffix")
        if args.contains:
            validate_vanity(args.contains, "contains")
    except ValueError as e:
        parser.error(str(e))

    main(
        prefix=args.prefix,
        suffix=args.suffix,
        contains=args.contains,
        count=args.count,
        case_sensitive=args.case_sensitive,
        workers=args.workers,
        output=Path(args.output),
    )
