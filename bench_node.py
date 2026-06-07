#!/usr/bin/env python3
"""Benchmark subtensor node block query performance.

Measures blocks/second throughput across historical 1M block ranges,
replicating the same RPC calls as sync_chain (getBlockHash + getBlock + getStorage).

Requirements: pip install rich

Usage:
    python bench_node.py ws://localhost:9944
    python bench_node.py wss://archive.chain.opentensor.ai:443 ws://localhost:9944
    python bench_node.py --no-archive ws://localhost:9944
    python bench_node.py --samples 50 --concurrency 4 --timeout 10 ws://localhost:9944
    python bench_node.py --json results.json ws://localhost:9944
"""

import argparse
import http.client
import json
import math
import random
import ssl
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlsplit

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

console = Console()

RANGE_SIZE = 1_000_000

# Storage key for System.Events — same query sync_chain does via substrate.get_events()
SYSTEM_EVENTS_KEY = "0x26aa394eea5630e07c48ae0c9558cef780d41e5e16056765bc8461851072c9d7"

# Timestamp.Now storage key — sync_chain calls get_timestamp_via_storage()
TIMESTAMP_NOW_KEY = "0xf0c365c3cf59d671eb72da0e7a4113c49f1f0515f462cdcf84e0f1d6045dfcbb"

TIMEOUT = 30.0  # seconds per RPC call; overridden by --timeout

_ssl_ctx = None
_tls = threading.local()


def _get_ssl_ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def _get_conn(url: str) -> tuple[str, http.client.HTTPConnection, str]:
    """Persistent per-thread connection (keep-alive), mirroring sync_chain's persistent websocket."""
    parts = urlsplit(url)
    key = f"{parts.scheme}://{parts.netloc}"
    conns = getattr(_tls, "conns", None)
    if conns is None:
        conns = _tls.conns = {}
    conn = conns.get(key)
    if conn is None:
        if parts.scheme == "https":
            conn = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=TIMEOUT, context=_get_ssl_ctx())
        else:
            conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=TIMEOUT)
        conns[key] = conn
    return key, conn, parts.path or "/"


def _drop_conn(key: str):
    conn = _tls.conns.pop(key, None)
    if conn is not None:
        conn.close()


def ws_to_http(url: str) -> str:
    if url.startswith("ws://"):
        return url.replace("ws://", "http://", 1)
    if url.startswith("wss://"):
        return url.replace("wss://", "https://", 1)
    return url


def node_label(url: str) -> str:
    for prefix in ("https://", "http://", "wss://", "ws://"):
        if url.startswith(prefix):
            return url[len(prefix) :].rstrip("/")
    return url


def _post(url: str, payload: bytes) -> bytes:
    for attempt in (1, 2):
        key, conn, path = _get_conn(url)
        try:
            conn.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = resp.read()
        except TimeoutError:
            _drop_conn(key)
            raise
        except (http.client.HTTPException, OSError):
            # Stale keep-alive connection: drop it and retry once on a fresh one
            _drop_conn(key)
            if attempt == 2:
                raise
            continue
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {body[:120].decode(errors='replace')}")
        return body
    raise AssertionError("unreachable")


def rpc_call(url: str, method: str, params=None):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    result = json.loads(_post(url, payload))
    if "error" in result:
        raise RuntimeError(f"RPC error: {result['error']}")
    return result.get("result")


def get_chain_head(url: str) -> int:
    hash_ = rpc_call(url, "chain_getFinalizedHead")
    header = rpc_call(url, "chain_getHeader", [hash_])
    return int(header["number"], 16)


def fetch_full_block(url: str, block_number: int) -> float:
    t0 = time.perf_counter()
    block_hash = rpc_call(url, "chain_getBlockHash", [block_number])
    rpc_call(url, "chain_getBlock", [block_hash])
    rpc_call(url, "state_getStorage", [SYSTEM_EVENTS_KEY, block_hash])
    rpc_call(url, "state_getStorage", [TIMESTAMP_NOW_KEY, block_hash])
    return time.perf_counter() - t0


def benchmark_range(url: str, start: int, end: int, block_numbers: list[int], concurrency: int, progress=None, task_id=None) -> dict:
    timings = []
    errors = 0
    error_samples: list[str] = []

    def record_error(e: Exception):
        nonlocal errors
        errors += 1
        msg = (str(e) or type(e).__name__)[:160]
        if len(error_samples) < 3 and msg not in error_samples:
            error_samples.append(msg)

    wall_start = time.perf_counter()

    if concurrency <= 1:
        for bn in block_numbers:
            try:
                timings.append(fetch_full_block(url, bn))
            except Exception as e:
                record_error(e)
            if progress and task_id is not None:
                progress.advance(task_id)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(fetch_full_block, url, bn): bn for bn in block_numbers}
            for fut in as_completed(futures):
                try:
                    timings.append(fut.result())
                except Exception as e:
                    record_error(e)
                if progress and task_id is not None:
                    progress.advance(task_id)

    wall_elapsed = time.perf_counter() - wall_start

    if not timings:
        return {"label": format_range_label(start, end), "error": "all queries failed", "errors": errors, "error_samples": error_samples}

    timings.sort()
    return {
        "label": format_range_label(start, end),
        "samples": len(timings),
        "errors": errors,
        "error_samples": error_samples,
        "avg_ms": statistics.mean(timings) * 1000,
        "median_ms": statistics.median(timings) * 1000,
        # Nearest-rank p95 (the old int(n*0.95) returned the max for the default 20 samples)
        "p95_ms": timings[min(len(timings) - 1, math.ceil(len(timings) * 0.95) - 1)] * 1000,
        "min_ms": timings[0] * 1000,
        "max_ms": timings[-1] * 1000,
        "blocks_per_sec": len(timings) / wall_elapsed,
        "wall_sec": wall_elapsed,
    }


def format_range_label(start: int, end: int) -> str:
    s = f"{start / 1_000_000:.0f}M" if start >= 1_000_000 else f"{start // 1000}K" if start >= 1000 else str(start)
    e = f"{(end + 1) / 1_000_000:.0f}M" if (end + 1) % 1_000_000 == 0 else f"{end:,}"
    return f"{s} - {e}"


def build_ranges(head: int) -> list[tuple[int, int]]:
    ranges = []
    start = 0
    while start < head:
        end = min(start + RANGE_SIZE - 1, head)
        ranges.append((start, end))
        start += RANGE_SIZE
    return ranges


def sample_blocks_for_range(start: int, end: int, samples: int) -> list[int]:
    population = range(start, end + 1)
    n = min(samples, len(population))
    return sorted(random.sample(population, n))


def bps_style(bps: float) -> str:
    if bps > 5:
        return "green"
    if bps > 2:
        return "yellow"
    return "red"


def print_header(urls: list[str], heads: dict[str, int], archive: bool, samples: int, concurrency: int):
    lines = []
    head_max = max(heads.values())
    for url in urls:
        head = heads.get(url, "?")
        head_str = f"{head:,}" if isinstance(head, int) else head
        behind = f"  [yellow]({head_max - head:,} behind)[/]" if isinstance(head, int) and head_max - head > 256 else ""
        lines.append(f"[cyan]{node_label(url)}[/]  head: {head_str}{behind}")
    lines.append("")
    lines.append(f"Mode:        [bold]{'archive (full history)' if archive else 'recent blocks only'}[/]")
    lines.append(f"Samples:     {samples} random blocks per range")
    lines.append(f"Concurrency: {concurrency}")
    lines.append(f"RPC calls:   getBlockHash + getBlock + getStorage(Events) + getStorage(Timestamp)")
    console.print(Panel("\n".join(lines), title="[bold]Subtensor Node Benchmark[/]", border_style="blue"))


def build_range_table(label: str, results_by_url: dict[str, dict]) -> Table:
    table = Table(title=f"[bold]{label}[/]", show_header=True, header_style="bold", padding=(0, 1), expand=False)
    table.add_column("Node", style="cyan", min_width=20)
    table.add_column("blk/s", justify="right", min_width=8)
    table.add_column("avg", justify="right", min_width=8)
    table.add_column("median", justify="right", min_width=8)
    table.add_column("p95", justify="right", min_width=8)
    table.add_column("range", justify="right", min_width=12)
    table.add_column("err", justify="right", min_width=4)

    for url, result in results_by_url.items():
        name = node_label(url)
        if "error" in result:
            status = "[yellow]SKIP[/]" if result["error"] == "not synced" else "[red]FAIL[/]"
            err_count = result.get("errors", 0)
            table.add_row(name, status, "-", "-", "-", "-", f"[red]{err_count}[/]" if err_count else "")
            continue

        bps = result["blocks_per_sec"]
        style = bps_style(bps)
        err_str = f"[red]{result['errors']}[/]" if result["errors"] else ""

        table.add_row(
            name,
            f"[{style}]{bps:.2f}[/]",
            f"{result['avg_ms']:.0f}ms",
            f"{result['median_ms']:.0f}ms",
            f"{result['p95_ms']:.0f}ms",
            f"{result['min_ms']:.0f}-{result['max_ms']:.0f}ms",
            err_str,
        )

    return table


def build_summary_table(all_results: dict[str, list[dict]]) -> Table:
    table = Table(title="[bold]Summary[/]", show_header=True, header_style="bold", padding=(0, 1), expand=False)
    table.add_column("Node", style="cyan", min_width=20)
    table.add_column("Throughput", justify="right", min_width=10)
    table.add_column("Avg latency", justify="right", min_width=10)
    table.add_column("Best range", min_width=18)
    table.add_column("Worst range", min_width=18)
    table.add_column("Errors", justify="right", min_width=6)

    summaries = {}
    for url, results in all_results.items():
        valid = [r for r in results if "error" not in r]
        if not valid:
            summaries[url] = None
            continue
        total_samples = sum(r["samples"] for r in valid)
        total_wall = sum(r["wall_sec"] for r in valid)
        summaries[url] = {
            "throughput": total_samples / total_wall,
            "avg_ms": statistics.mean([r["avg_ms"] for r in valid]),
            "best": max(valid, key=lambda r: r["blocks_per_sec"]),
            "worst": min(valid, key=lambda r: r["blocks_per_sec"]),
            "total_samples": total_samples,
            "total_wall": total_wall,
            "total_errors": sum(r.get("errors", 0) for r in results),
        }

    for url, s in summaries.items():
        name = node_label(url)
        if s is None:
            table.add_row(name, "[red]FAIL[/]", "-", "-", "-", "-")
            continue

        style = bps_style(s["throughput"])
        err_str = f"[red]{s['total_errors']}[/]" if s["total_errors"] else "-"

        table.add_row(
            name,
            f"[{style} bold]{s['throughput']:.2f} blk/s[/]",
            f"{s['avg_ms']:.0f}ms",
            f"{s['best']['label']} ({s['best']['blocks_per_sec']:.2f})",
            f"{s['worst']['label']} ({s['worst']['blocks_per_sec']:.2f})",
            err_str,
        )

    # Ranking table if multiple nodes
    valid_summaries = {u: s for u, s in summaries.items() if s is not None}
    if len(valid_summaries) >= 2:
        ranked = sorted(valid_summaries.items(), key=lambda x: x[1]["throughput"], reverse=True)
        leader_bps = ranked[0][1]["throughput"]

        rank_table = Table(title="[bold]Ranking[/]", show_header=True, header_style="bold", padding=(0, 1), expand=False)
        rank_table.add_column("#", justify="right", min_width=2)
        rank_table.add_column("Node", style="cyan", min_width=20)
        rank_table.add_column("blk/s", justify="right", min_width=10)
        rank_table.add_column("", min_width=32)

        for i, (url, s) in enumerate(ranked, 1):
            ratio = s["throughput"] / leader_bps if leader_bps else 0
            bar_len = int(ratio * 30)
            bar = Text("█" * bar_len, style=bps_style(s["throughput"]))
            label = " (fastest)" if i == 1 else ""
            style = bps_style(s["throughput"])
            rank_table.add_row(str(i), node_label(url), f"[{style} bold]{s['throughput']:.2f}[/]", bar + Text(label, style="bold green"))

        console.print()
        console.print(rank_table)

    return table


def main():
    global TIMEOUT

    parser = argparse.ArgumentParser(description="Benchmark subtensor node block query performance")
    parser.add_argument("urls", nargs="+", help="Node URLs (ws://, wss://, http://, https://)")
    parser.add_argument("--no-archive", dest="archive", action="store_false", default=True, help="Only test recent blocks")
    parser.add_argument("--samples", type=int, default=20, help="Random blocks per range (default: 20)")
    parser.add_argument("--concurrency", type=int, default=1, help="Parallel requests (default: 1)")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    parser.add_argument("--timeout", type=float, default=30, help="Per-RPC timeout in seconds (default: 30)")
    parser.add_argument("--json", dest="json_out", metavar="PATH", default=None, help="Write results to PATH as JSON")
    args = parser.parse_args()

    TIMEOUT = args.timeout
    urls = [ws_to_http(u) for u in args.urls]

    if args.seed is not None:
        random.seed(args.seed)

    # Get chain head from each node (in parallel); drop unreachable nodes
    heads: dict[str, int] = {}
    failures: dict[str, Exception] = {}
    with console.status("[bold blue]Connecting to nodes..."):
        with ThreadPoolExecutor(max_workers=len(urls)) as pool:
            futures = {pool.submit(get_chain_head, url): url for url in urls}
            for fut in as_completed(futures):
                url = futures[fut]
                try:
                    heads[url] = fut.result()
                except Exception as e:
                    failures[url] = e

    for url, exc in failures.items():
        console.print(f"[red]Failed to connect to {node_label(url)}: {escape(str(exc) or type(exc).__name__)}[/]")
    urls = [u for u in urls if u in heads]
    if not urls:
        sys.exit(1)

    head = max(heads.values())

    print_header(urls, heads, args.archive, args.samples, args.concurrency)

    if args.archive:
        ranges = build_ranges(head)
    else:
        start = max(0, head - 256)
        ranges = [(start, head)]

    # Shared random samples per range, so every node is measured on the same blocks.
    # The last range uses each node's own latest blocks (sequential, not random).
    range_blocks = {}
    for s, e in ranges[:-1]:
        range_blocks[(s, e)] = sample_blocks_for_range(s, e, args.samples)
    latest_blocks = {url: list(range(max(0, heads[url] - args.samples + 1), heads[url] + 1)) for url in urls}

    def blocks_for(url: str, s: int, e: int, is_last: bool) -> list[int]:
        if is_last:
            return latest_blocks[url]
        # A node that lags behind only gets the blocks it actually has
        return [b for b in range_blocks[(s, e)] if b <= heads[url]]

    total_queries = sum(len(blocks_for(url, s, e, (s, e) == ranges[-1])) for s, e in ranges for url in urls)

    all_results: dict[str, list[dict]] = {url: [] for url in urls}

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        TextColumn("blocks"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Benchmarking", total=total_queries)

        for start, end in ranges:
            is_last = (start, end) == ranges[-1]
            label = f"latest {args.samples}" if is_last else format_range_label(start, end)
            progress.update(task, description=f"[bold blue]{label}[/]")

            results_for_range: dict[str, dict] = {}

            # Benchmark all nodes in parallel for this range; skip nodes not synced this far
            with ThreadPoolExecutor(max_workers=len(urls)) as pool:
                futures = {}
                for url in urls:
                    blocks = blocks_for(url, start, end, is_last)
                    if not blocks:
                        results_for_range[url] = {
                            "label": format_range_label(start, end),
                            "error": "not synced",
                            "errors": 0,
                            "error_samples": [f"not synced — node head is {heads[url]:,}"],
                        }
                        continue
                    futures[pool.submit(benchmark_range, url, start, end, blocks, args.concurrency, progress, task)] = url
                for fut in as_completed(futures):
                    results_for_range[futures[fut]] = fut.result()

            # Preserve URL ordering for display
            results_for_range = {url: results_for_range[url] for url in urls}
            for url, result in results_for_range.items():
                all_results[url].append(result)

            progress.stop()
            console.print(build_range_table(label, results_for_range))
            for url, result in results_for_range.items():
                for msg in result.get("error_samples", []):
                    console.print(f"  [dim]{node_label(url)}: [red]{escape(msg)}[/red][/dim]")
            console.print()
            progress.start()

    console.print()
    table = build_summary_table(all_results)
    console.print(table)
    console.print()

    if args.json_out:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "config": {
                "archive": args.archive,
                "samples": args.samples,
                "concurrency": args.concurrency,
                "seed": args.seed,
                "timeout": args.timeout,
            },
            "heads": {node_label(u): heads[u] for u in urls},
            "results": {node_label(u): all_results[u] for u in urls},
        }
        with open(args.json_out, "w") as f:
            json.dump(payload, f, indent=2)
        console.print(f"Results written to [bold]{args.json_out}[/]")
        console.print()


if __name__ == "__main__":
    main()
