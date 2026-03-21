#!/usr/bin/env python3
"""Benchmark subtensor node block query performance.

Measures blocks/second throughput across historical 1M block ranges,
replicating the same RPC calls as sync_chain (getBlockHash + getBlock + getStorage).

Requirements: pip install rich

Usage:
    python tools/bench_node.py ws://localhost:9944
    python tools/bench_node.py wss://archive.chain.opentensor.ai:443 ws://localhost:9944
    python tools/bench_node.py --no-archive ws://localhost:9944
    python tools/bench_node.py --samples 50 --concurrency 4 ws://localhost:9944
"""

import argparse
import json
import random
import ssl
import statistics
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.console import Console
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

_ssl_ctx = None


def _get_ssl_ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


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


def rpc_call(url: str, method: str, params=None):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    ctx = _get_ssl_ctx() if url.startswith("https") else None
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        result = json.loads(resp.read())
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

    wall_start = time.perf_counter()

    if concurrency <= 1:
        for bn in block_numbers:
            try:
                timings.append(fetch_full_block(url, bn))
            except Exception:
                errors += 1
            if progress and task_id is not None:
                progress.advance(task_id)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(fetch_full_block, url, bn): bn for bn in block_numbers}
            for fut in as_completed(futures):
                try:
                    timings.append(fut.result())
                except Exception:
                    errors += 1
                if progress and task_id is not None:
                    progress.advance(task_id)

    wall_elapsed = time.perf_counter() - wall_start

    if not timings:
        return {"label": format_range_label(start, end), "error": "all queries failed", "errors": errors}

    timings.sort()
    return {
        "label": format_range_label(start, end),
        "samples": len(timings),
        "errors": errors,
        "avg_ms": statistics.mean(timings) * 1000,
        "median_ms": statistics.median(timings) * 1000,
        "p95_ms": timings[int(len(timings) * 0.95)] * 1000 if len(timings) >= 5 else timings[-1] * 1000,
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
    for url in urls:
        head = heads.get(url, "?")
        head_str = f"{head:,}" if isinstance(head, int) else head
        lines.append(f"[cyan]{node_label(url)}[/]  head: {head_str}")
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
            table.add_row(name, "[red]FAIL[/]", "-", "-", "-", "-", str(result.get("errors", "")))
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
    parser = argparse.ArgumentParser(description="Benchmark subtensor node block query performance")
    parser.add_argument("urls", nargs="+", help="Node URLs (ws://, wss://, http://, https://)")
    parser.add_argument("--no-archive", dest="archive", action="store_false", default=True, help="Only test recent blocks")
    parser.add_argument("--samples", type=int, default=20, help="Random blocks per range (default: 20)")
    parser.add_argument("--concurrency", type=int, default=1, help="Parallel requests (default: 1)")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    args = parser.parse_args()

    urls = [ws_to_http(u) for u in args.urls]

    if args.seed is not None:
        random.seed(args.seed)

    # Get chain head from each node
    heads: dict[str, int] = {}
    with console.status("[bold blue]Connecting to nodes..."):
        for url in urls:
            try:
                heads[url] = get_chain_head(url)
            except Exception as e:
                console.print(f"[red]Failed to connect to {node_label(url)}: {e}[/]")
                sys.exit(1)

    head = min(heads.values())

    print_header(urls, heads, args.archive, args.samples, args.concurrency)

    if args.archive:
        ranges = build_ranges(head)
    else:
        start = max(0, head - 256)
        ranges = [(start, head)]

    range_blocks = {}
    for s, e in ranges:
        if (s, e) == ranges[-1]:
            # Last range: use the N most recent blocks (sequential, not random)
            recent_start = max(s, head - args.samples + 1)
            range_blocks[(s, e)] = list(range(recent_start, head + 1))
        else:
            range_blocks[(s, e)] = sample_blocks_for_range(s, e, args.samples)
    total_queries = sum(len(b) for b in range_blocks.values()) * len(urls)

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
            blocks = range_blocks[(start, end)]
            is_last = (start, end) == ranges[-1]
            label = f"latest {len(blocks)}" if is_last else format_range_label(start, end)
            progress.update(task, description=f"[bold blue]{label}[/]")

            # Benchmark all nodes in parallel for this range
            with ThreadPoolExecutor(max_workers=len(urls)) as pool:
                futures = {pool.submit(benchmark_range, url, start, end, blocks, args.concurrency, progress, task): url for url in urls}
                results_for_range: dict[str, dict] = {}
                for fut in as_completed(futures):
                    url = futures[fut]
                    result = fut.result()
                    all_results[url].append(result)
                    results_for_range[url] = result

            # Preserve URL ordering for display
            results_for_range = {url: results_for_range[url] for url in urls}

            progress.stop()
            console.print(build_range_table(label, results_for_range))
            console.print()
            progress.start()

    console.print()
    table = build_summary_table(all_results)
    console.print(table)
    console.print()


if __name__ == "__main__":
    main()
