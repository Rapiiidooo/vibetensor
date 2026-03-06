# CLI-only:
#   python advanced-dca.py --network finney --wallet hot-trading --hotkey 5..... --netuids 64 19 --amount 0.25 --tempo 3600 --tolerance 0.02 --dry-run
# Strategy file:
#   python advanced-dca.py --strategy strategies/example.yml
#   python advanced-dca.py --strategy strategies/example.yml --dry-run --max-iterations 3

from __future__ import annotations

import argparse
import time
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from collections import deque

import yaml
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------
ROOT_NETUID = 0
DEFAULT_TOLERANCE = 0.05
ALLOW_PARTIAL = True
MIN_ALPHA_ROOT_TO_KEEP_TAO = 0

console = Console()

BANNER = r"""
     _       _                               _   ____   ____    _
    / \   __| |_   ____ _ _ __   ___ ___  __| | |  _ \ / ___|  / \
   / _ \ / _` \ \ / / _` | '_ \ / __/ _ \/ _` | | | | | |     / _ \
  / ___ \ (_| |\ V / (_| | | | | (_|  __/ (_| | | |_| | |___ / ___ \
 /_/   \_\__,_| \_/ \__,_|_| |_|\___\___|\__,_| |____/ \____/_/   \_\
"""


class DCADisplay:
    """Manages the live TUI: a fixed balance header + scrolling log panel."""

    def __init__(
        self,
        subnet_configs: list["SubnetConfig"],
        stats: "DCAStats",
        dry_run: bool,
        max_tao: float | None,
        max_iterations: int | None,
    ):
        self.subnet_configs = subnet_configs
        self.stats = stats
        self.dry_run = dry_run
        self.max_tao = max_tao
        self.max_iterations = max_iterations
        self.log_lines: deque[str] = deque(maxlen=200)

        # Chain state (updated each iteration)
        self.balance = None
        self.alpha_root = None
        self.subnet_alphas: dict[int, float] = {}
        self.prices: dict[int, float] = {}

    # -- Logging ----------------------------------------------------------

    def log(self, level: str, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        color = {"INFO": "cyan", "WARN": "yellow", "ERROR": "red", "OK": "green"}.get(level, "white")
        tag = "[yellow]DRY[/] " if self.dry_run else ""
        self.log_lines.append(f" [dim]{ts}[/]  {tag}[bold {color}]{level:5}[/] {msg}")

    # -- Renderables ------------------------------------------------------

    def _build_header(self) -> Table:
        mode_str = "[yellow]DRY RUN[/]" if self.dry_run else "[green]LIVE[/]"
        table = Table(
            title=f"[bold]{mode_str} Balance[/bold]",
            box=box.HEAVY,
            border_style="bright_blue",
            header_style="bold bright_white on dark_blue",
            padding=(0, 1),
            expand=True,
        )
        table.add_column("", justify="left", style="bold", min_width=8)
        table.add_column("Balance", justify="right", min_width=14)
        table.add_column("Value (τ)", justify="right", min_width=12)
        table.add_column("Price", justify="right", style="yellow", min_width=14)
        table.add_column("DCA Amt", justify="right", style="dim", min_width=10)
        table.add_column("Next In", justify="right", min_width=10)

        # Free + Root
        bal_str = str(self.balance) if self.balance is not None else "-"
        root_str = str(self.alpha_root) if self.alpha_root is not None else "-"
        table.add_row("[white]Free[/]", f"[bold green]{bal_str}[/]", "", "", "", "")
        table.add_row("[white]Root[/]", f"[bold yellow]{root_str}[/]", "", "", "", "")
        table.add_section()

        # Per-subnet rows
        now = time.monotonic()
        total_value_tao = 0.0
        for sc in self.subnet_configs:
            alpha = self.subnet_alphas.get(sc.netuid, 0)
            price = self.prices.get(sc.netuid, 0)
            value_tao = alpha * price
            total_value_tao += value_tao

            remaining = max(0, sc.next_run_at - now)
            if remaining > 0:
                m, s = divmod(int(remaining), 60)
                h, m = divmod(m, 60)
                next_str = f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")
            else:
                next_str = "[bold green]now[/]"

            value_str = f"[green]{value_tao:.4f}[/]" if value_tao > 0 else "[dim]-[/]"

            table.add_row(
                f"[bold cyan]SN{sc.netuid}[/]",
                f"[cyan]{alpha:.4f}[/] α",
                value_str,
                f"{price:.9f}" if price > 0 else "[dim]-[/]",
                f"{sc.amount:.4f} τ",
                next_str,
            )

        # Footer
        table.add_section()
        spent = self.stats.total_tao_spent
        iters = self.stats.total_iterations
        remaining_info = ""
        if self.max_tao is not None:
            remaining_info += f"budget [yellow]{self.max_tao - spent:.4f}[/] τ"
        if self.max_iterations is not None:
            if remaining_info:
                remaining_info += "  "
            remaining_info += f"iters [yellow]{self.max_iterations - iters}[/] left"
        table.add_row(
            "[bold]Total[/]",
            f"[bold green]{spent:.4f}[/] τ spent",
            f"[bold green]{total_value_tao:.4f}[/] τ" if total_value_tao > 0 else "",
            f"[dim]#{iters} buys[/]",
            "",
            remaining_info,
        )
        return table

    def _build_logs(self, max_lines: int) -> Panel:
        lines = list(self.log_lines)[-max_lines:] if self.log_lines else [
            "[dim]  Waiting for first iteration...[/]"
        ]
        return Panel(
            "\n".join(lines),
            title="[bold]Logs[/bold]",
            border_style="bright_blue",
            box=box.ROUNDED,
            padding=(0, 0),
        )

    def build(self) -> Layout:
        term_height = console.height
        header_height = 8 + len(self.subnet_configs)
        log_content_lines = max(3, term_height - header_height - 3)

        layout = Layout()
        layout.split_column(
            Layout(name="header", size=header_height),
            Layout(name="logs"),
        )
        layout["header"].update(self._build_header())
        layout["logs"].update(self._build_logs(log_content_lines))
        return layout


# ------------------------------------------------------------------
# SubnetConfig
# ------------------------------------------------------------------
@dataclass
class SubnetConfig:
    netuid: int
    amount: float
    tempo: float | None = None       # fixed
    tempo_min: float | None = None   # random range
    tempo_max: float | None = None
    next_run_at: float = 0.0         # timestamp (time.monotonic)

    def get_sleep_time(self) -> float:
        if self.tempo is not None:
            return self.tempo
        return random.uniform(self.tempo_min, self.tempo_max)

    def schedule_next(self):
        self.next_run_at = time.monotonic() + self.get_sleep_time()


# ------------------------------------------------------------------
# Stats dataclasses
# ------------------------------------------------------------------
@dataclass
class SubnetPurchase:
    iteration: int
    timestamp: datetime
    netuid: int
    action: str  # "add_stake"
    tao_amount: float
    estimated_alpha: float
    price: float
    success: bool
    dry_run: bool


@dataclass
class SubnetStats:
    netuid: int
    purchases: list[SubnetPurchase] = field(default_factory=list)

    @property
    def successful(self) -> list[SubnetPurchase]:
        return [p for p in self.purchases if p.success]

    @property
    def failed(self) -> list[SubnetPurchase]:
        return [p for p in self.purchases if not p.success]

    @property
    def total_tao_spent(self) -> float:
        return sum(p.tao_amount for p in self.successful)

    @property
    def total_alpha_estimated(self) -> float:
        return sum(p.estimated_alpha for p in self.successful)

    @property
    def avg_price(self) -> float:
        successes = self.successful
        if not successes:
            return 0.0
        return sum(p.price for p in successes) / len(successes)


@dataclass
class DCAStats:
    by_netuid: dict[int, SubnetStats] = field(default_factory=dict)
    total_iterations: int = 0
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def total_tao_spent(self) -> float:
        return sum(s.total_tao_spent for s in self.by_netuid.values())

    def record(self, purchase: SubnetPurchase):
        if purchase.netuid not in self.by_netuid:
            self.by_netuid[purchase.netuid] = SubnetStats(netuid=purchase.netuid)
        self.by_netuid[purchase.netuid].purchases.append(purchase)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def estimate_alpha_from_tao(tao_amount: float, price_tao_per_alpha: float) -> float:
    if price_tao_per_alpha <= 0:
        return 0.0
    return tao_amount / price_tao_per_alpha


def print_summary(stats: DCAStats, stop_reason: str, network: str, dry_run: bool):
    end_time = datetime.now(timezone.utc)
    duration = end_time - stats.start_time

    hours, remainder = divmod(int(duration.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    duration_str = f"{hours}h {minutes}m {seconds}s"

    console.print()

    # Session info panel
    mode_str = "[bold yellow]DRY RUN[/bold yellow]" if dry_run else "[bold green]LIVE[/bold green]"
    info_lines = [
        f"[dim]Start:[/]      {stats.start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"[dim]End:[/]        {end_time.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"[dim]Duration:[/]   [bold]{duration_str}[/bold]",
        f"[dim]Network:[/]    {network}",
        f"[dim]Mode:[/]       {mode_str}",
        f"[dim]Purchases:[/]  [bold]{stats.total_iterations}[/bold]",
        f"[dim]Stopped:[/]    {stop_reason}",
    ]
    if stats.total_tao_spent > 0:
        info_lines.append(f"[dim]Total TAO:[/]  [bold green]{stats.total_tao_spent:.4f}[/bold green]")

    console.print(Panel(
        "\n".join(info_lines),
        title="[bold cyan]DCA SESSION SUMMARY[/bold cyan]",
        border_style="cyan",
        box=box.DOUBLE,
        padding=(1, 2),
    ))

    # Per-subnet breakdown table
    table = Table(
        title="[bold]Per-Subnet Breakdown[/bold]",
        box=box.ROUNDED,
        border_style="bright_blue",
        header_style="bold bright_white on dark_blue",
        title_style="bold cyan",
        padding=(0, 1),
    )
    table.add_column("Subnet", justify="center", style="bold cyan", min_width=8)
    table.add_column("Buys", justify="center", style="green")
    table.add_column("Failed", justify="center", style="red")
    table.add_column("TAO Spent", justify="right", style="bold green")
    table.add_column("Est. Alpha", justify="right", style="yellow")
    table.add_column("Avg Price", justify="right", style="dim")

    total_purchases = 0
    total_failed = 0
    total_tao = 0.0
    total_alpha = 0.0

    for netuid in sorted(stats.by_netuid.keys()):
        s = stats.by_netuid[netuid]
        n_ok = len(s.successful)
        n_fail = len(s.failed)
        total_purchases += n_ok
        total_failed += n_fail
        total_tao += s.total_tao_spent
        total_alpha += s.total_alpha_estimated
        table.add_row(
            f"SN{netuid}",
            str(n_ok),
            str(n_fail) if n_fail > 0 else "[dim]-[/dim]",
            f"{s.total_tao_spent:.4f}",
            f"{s.total_alpha_estimated:.4f}",
            f"{s.avg_price:.6f}" if s.avg_price > 0 else "[dim]-[/dim]",
        )

    table.add_section()
    table.add_row(
        "[bold]TOTAL[/bold]",
        f"[bold]{total_purchases}[/bold]",
        f"[bold red]{total_failed}[/bold red]" if total_failed > 0 else "[dim]-[/dim]",
        f"[bold green]{total_tao:.4f}[/bold green]",
        f"[bold yellow]{total_alpha:.4f}[/bold yellow]",
        "",
    )

    console.print(table)
    console.print()


# ------------------------------------------------------------------
# YAML strategy loader
# ------------------------------------------------------------------
def load_strategy(path: str) -> dict:
    """Parse a YAML strategy file and return a config dict ready for main()."""
    data = yaml.safe_load(Path(path).read_text())

    # Global tempo defaults
    global_tempo = data.get("tempo")
    global_tempo_min = data.get("tempo_min")
    global_tempo_max = data.get("tempo_max")

    subnet_configs: list[SubnetConfig] = []
    for entry in data["subnets"]:
        netuid = entry["netuid"]
        amount = entry["amount"]

        # Per-subnet tempo overrides, fall back to global
        tempo = entry.get("tempo", global_tempo)
        tempo_min = entry.get("tempo_min", global_tempo_min)
        tempo_max = entry.get("tempo_max", global_tempo_max)

        # Validate: must have either fixed tempo or a random range
        has_fixed = tempo is not None
        has_range = tempo_min is not None and tempo_max is not None
        if not has_fixed and not has_range:
            raise ValueError(
                f"Subnet {netuid}: must have 'tempo' or both 'tempo_min'/'tempo_max' "
                f"(either per-subnet or globally)"
            )
        if has_range and tempo_min > tempo_max:
            raise ValueError(f"Subnet {netuid}: tempo_min ({tempo_min}) > tempo_max ({tempo_max})")

        subnet_configs.append(SubnetConfig(
            netuid=netuid,
            amount=amount,
            tempo=tempo if has_fixed else None,
            tempo_min=tempo_min if not has_fixed else None,
            tempo_max=tempo_max if not has_fixed else None,
        ))

    return {
        "network": data["network"],
        "wallet_name": data["wallet"],
        "hotkey": data["hotkey"],
        "subnet_configs": subnet_configs,
        "tolerance": data.get("tolerance", DEFAULT_TOLERANCE),
        "max_tao": data.get("max_tao"),
        "max_iterations": data.get("max_iterations"),
        "dry_run": data.get("dry_run", False),
    }


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main(
    network: str,
    wallet_name: str,
    hotkey: str,
    subnet_configs: list[SubnetConfig],
    tolerance: float,
    max_tao: float | None,
    max_iterations: int | None,
    dry_run: bool,
):
    from bittensor import Subtensor, Balance
    from bittensor_wallet import Wallet

    # --- Banner ---
    mode_tag = "[bold yellow] DRY RUN [/bold yellow]" if dry_run else "[bold green] LIVE [/bold green]"
    console.print(Panel(
        Text.from_ansi(BANNER),
        subtitle=mode_tag,
        border_style="bright_blue",
        box=box.DOUBLE,
        padding=(0, 2),
    ))

    subtensor = Subtensor(network=network, log_verbose=False)
    wallet = Wallet(name=wallet_name)
    coldkey = wallet.coldkeypub.ss58_address

    stats = DCAStats()

    # --- Initial info ---
    dynamic_infos = subtensor.all_subnets()
    dynamic_info_by_netuid = {d.netuid: d for d in dynamic_infos}

    # Config panel
    config_lines = [
        f"[dim]Coldkey:[/]     [white]{coldkey}[/white]",
        f"[dim]Hotkey:[/]     [white]{hotkey}[/white]",
        f"[dim]Network:[/]    [bold]{network}[/bold]",
        f"[dim]Tolerance:[/]  [bold]{tolerance * 100:.1f}%[/bold]",
    ]
    if max_tao is not None:
        config_lines.append(f"[dim]Max TAO:[/]    [bold yellow]{max_tao}[/bold yellow]")
    if max_iterations is not None:
        config_lines.append(f"[dim]Max Iters:[/]  [bold yellow]{max_iterations}[/bold yellow]")

    console.print(Panel(
        "\n".join(config_lines),
        title="[bold]Wallet Config[/bold]",
        border_style="bright_blue",
        box=box.ROUNDED,
        padding=(1, 2),
    ))

    # Subnets table
    sn_table = Table(
        title="[bold]Target Subnets[/bold]",
        box=box.ROUNDED,
        border_style="bright_blue",
        header_style="bold bright_white on dark_blue",
        padding=(0, 1),
    )
    sn_table.add_column("Subnet", justify="center", style="bold cyan", min_width=8)
    sn_table.add_column("Amount (TAO)", justify="right", style="green")
    sn_table.add_column("Price (TAO/α)", justify="right", style="yellow")
    sn_table.add_column("Tempo", justify="center", style="dim")

    for sc in subnet_configs:
        pool = dynamic_info_by_netuid.get(sc.netuid)
        price_str = f"{pool.price.tao:.9f}" if pool else "[red]N/A[/red]"
        if sc.tempo is not None:
            tempo_str = f"{sc.tempo:.0f}s"
        else:
            tempo_str = f"{sc.tempo_min:.0f}s ~ {sc.tempo_max:.0f}s"
        sn_table.add_row(f"SN{sc.netuid}", f"{sc.amount:.4f}", price_str, tempo_str)

    console.print(sn_table)

    if dry_run:
        console.print(Panel(
            "[bold yellow]No transactions will be submitted[/bold yellow]",
            border_style="yellow",
            box=box.ROUNDED,
        ))

    console.print()
    console.rule("[dim]Press CTRL+C to stop[/dim]")
    console.print()

    if not dry_run and wallet.coldkey_file.is_encrypted():
        wallet.unlock_coldkey()

    # --- Build live display ---
    display = DCADisplay(subnet_configs, stats, dry_run, max_tao, max_iterations)

    def refresh_chain_state():
        """Fetch balances, stakes, and prices from chain into display."""
        nonlocal dynamic_infos, dynamic_info_by_netuid
        dynamic_infos = subtensor.all_subnets()
        dynamic_info_by_netuid = {d.netuid: d for d in dynamic_infos}
        display.balance = subtensor.get_balance(coldkey)
        display.alpha_root = subtensor.get_stake(coldkey, hotkey, ROOT_NETUID)
        for _sc in subnet_configs:
            alpha = subtensor.get_stake(coldkey, hotkey, _sc.netuid)
            display.subnet_alphas[_sc.netuid] = float(alpha.tao)
            pool = dynamic_info_by_netuid.get(_sc.netuid)
            display.prices[_sc.netuid] = pool.price.tao if pool else 0.0

    # Initial chain state
    refresh_chain_state()

    # --- Init scheduler: all subnets run immediately ---
    now = time.monotonic()
    for sc in subnet_configs:
        sc.next_run_at = now

    # Track consecutive no-funds per subnet to detect global exhaustion
    no_funds_streak: dict[int, bool] = {sc.netuid: False for sc in subnet_configs}

    stop_reason = "unknown"

    try:
        with Live(display.build(), console=console, screen=True, refresh_per_second=1) as live:
            while True:
                # --- Find the subnet with the earliest next_run_at ---
                sc = min(subnet_configs, key=lambda s: s.next_run_at)

                # --- Sleep with live countdown ---
                while True:
                    wait = sc.next_run_at - time.monotonic()
                    if wait <= 0:
                        break
                    live.update(display.build())
                    time.sleep(min(1.0, wait))

                stats.total_iterations += 1
                iteration = stats.total_iterations

                # --- Check stopping conditions ---
                if max_iterations is not None and iteration > max_iterations:
                    stop_reason = f"max iterations reached ({max_iterations})"
                    display.log("INFO", f"[dim]#{iteration:04}[/dim] {stop_reason}")
                    live.update(display.build())
                    break

                if max_tao is not None and stats.total_tao_spent >= max_tao:
                    stop_reason = f"max TAO budget reached ({max_tao})"
                    display.log("INFO", f"[dim]#{iteration:04}[/dim] {stop_reason}")
                    live.update(display.build())
                    break

                # --- Refresh chain state ---
                display.log("INFO", f"[dim]#{iteration:04}[/dim] Refreshing chain state...")
                live.update(display.build())
                refresh_chain_state()
                live.update(display.build())

                netuid = sc.netuid
                pool = dynamic_info_by_netuid.get(netuid)

                if pool is None:
                    display.log("WARN", f"[dim]#{iteration:04}[/dim] [bold cyan]SN{netuid}[/bold cyan] not found, skipping")
                    sc.schedule_next()
                    live.update(display.build())
                    continue

                price = pool.price.tao
                stake_amount = sc.amount

                # --- Check budget ---
                if max_tao is not None:
                    remaining_budget = max_tao - stats.total_tao_spent
                    if remaining_budget <= 0:
                        stop_reason = f"max TAO budget reached ({max_tao})"
                        display.log("INFO", f"[dim]#{iteration:04}[/dim] {stop_reason}")
                        live.update(display.build())
                        break
                    if stake_amount > remaining_budget:
                        stake_amount = remaining_budget
                        display.log(
                            "WARN",
                            f"[dim]#{iteration:04}[/dim] [bold cyan]SN{netuid}[/bold cyan] "
                            f"adjusted to [yellow]{stake_amount:.4f}[/yellow] τ (budget limit)",
                        )

                stake_balance = Balance.from_tao(stake_amount)
                estimated_alpha = estimate_alpha_from_tao(stake_amount, price)

                action_taken = False

                # --- Unstake from root if balance is insufficient ---
                balance = display.balance
                alpha_root = display.alpha_root
                if balance.tao < stake_amount and alpha_root > Balance.from_tao(MIN_ALPHA_ROOT_TO_KEEP_TAO):
                    total_needed = sum(s.amount for s in subnet_configs)
                    shortfall = max(total_needed - balance.tao, 0)
                    movable = alpha_root - Balance.from_tao(MIN_ALPHA_ROOT_TO_KEEP_TAO)
                    unstake_amount = Balance.from_tao(min(shortfall, movable.tao))

                    display.log(
                        "WARN",
                        f"[dim]#{iteration:04}[/dim] Unstaking [yellow]{unstake_amount}[/yellow] from root "
                        f"(shortfall=[red]{shortfall:.4f}[/red])",
                    )
                    live.update(display.build())

                    if not dry_run:
                        try:
                            subtensor.unstake(
                                wallet=wallet,
                                netuid=ROOT_NETUID,
                                hotkey_ss58=hotkey,
                                amount=unstake_amount,
                            )
                            display.balance = subtensor.get_balance(coldkey)
                            display.alpha_root = subtensor.get_stake(coldkey, hotkey, ROOT_NETUID)
                            balance = display.balance
                            alpha_root = display.alpha_root
                            display.log("OK", f"[dim]#{iteration:04}[/dim] Unstake done")
                        except Exception as e:
                            display.log("ERROR", f"[dim]#{iteration:04}[/dim] Unstake failed: {e}")
                        live.update(display.build())

                # --- add_stake ---
                has_funds = balance.tao > 0.01 and balance.tao >= stake_amount
                if not has_funds and dry_run and alpha_root > Balance.from_tao(MIN_ALPHA_ROOT_TO_KEEP_TAO):
                    has_funds = True

                if has_funds:
                    display.log(
                        "INFO",
                        f"[dim]#{iteration:04}[/dim] [bold cyan]SN{netuid}[/bold cyan] "
                        f"[bold]add_stake[/bold] [green]{stake_balance}[/green] τ  "
                        f"est=[yellow]{estimated_alpha:.4f}[/yellow] α  "
                        f"price=[dim]{price:.9f}[/dim]",
                    )
                    live.update(display.build())

                    if dry_run:
                        stats.record(SubnetPurchase(
                            iteration=iteration, timestamp=datetime.now(timezone.utc),
                            netuid=netuid, action="add_stake", tao_amount=stake_amount,
                            estimated_alpha=estimated_alpha, price=price, success=True, dry_run=True,
                        ))
                        display.log("OK", f"[dim]#{iteration:04}[/dim] [bold cyan]SN{netuid}[/bold cyan] [dim](simulated)[/dim]")
                        action_taken = True
                    else:
                        try:
                            result, success = subtensor.add_stake(
                                wallet=wallet,
                                netuid=netuid,
                                hotkey_ss58=hotkey,
                                amount=stake_balance,
                                safe_staking=True,
                                allow_partial_stake=True,
                                rate_tolerance=tolerance,
                                mev_protection=True,
                            )
                            if success:
                                display.log("OK", f"[dim]#{iteration:04}[/dim] [bold cyan]SN{netuid}[/bold cyan] stake confirmed")
                            else:
                                display.log("ERROR", f"[dim]#{iteration:04}[/dim] [bold cyan]SN{netuid}[/bold cyan] stake rejected")
                            stats.record(SubnetPurchase(
                                iteration=iteration, timestamp=datetime.now(timezone.utc),
                                netuid=netuid, action="add_stake", tao_amount=stake_amount,
                                estimated_alpha=estimated_alpha, price=price, success=success, dry_run=False,
                            ))
                            action_taken = success
                        except Exception as e:
                            display.log("ERROR", f"[dim]#{iteration:04}[/dim] [bold cyan]SN{netuid}[/bold cyan] exception: {e}")
                            stats.record(SubnetPurchase(
                                iteration=iteration, timestamp=datetime.now(timezone.utc),
                                netuid=netuid, action="add_stake", tao_amount=stake_amount,
                                estimated_alpha=estimated_alpha, price=price, success=False, dry_run=False,
                            ))
                else:
                    display.log(
                        "WARN",
                        f"[dim]#{iteration:04}[/dim] [bold cyan]SN{netuid}[/bold cyan] "
                        f"no funds (balance=[red]{balance}[/red], root=[red]{alpha_root}[/red])",
                    )

                # --- Track no-funds streak ---
                no_funds_streak[netuid] = not action_taken
                if all(no_funds_streak.values()):
                    stop_reason = "no funds available on any subnet"
                    display.log("ERROR", f"[dim]#{iteration:04}[/dim] {stop_reason}")
                    live.update(display.build())
                    break

                # --- Schedule next run for this subnet ---
                sc.schedule_next()
                live.update(display.build())

    except KeyboardInterrupt:
        stop_reason = "user interrupt (CTRL+C)"

    console.print()
    print_summary(stats, stop_reason, network, dry_run)


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DCA add_stake on one or more subnets")

    # Strategy file mode
    parser.add_argument("--strategy", type=str, help="Path to YAML strategy file")

    # CLI-only mode args
    parser.add_argument("--network", choices=["finney", "test"], default="finney")
    parser.add_argument("--wallet")
    parser.add_argument("--hotkey")
    parser.add_argument("--netuids", type=int, nargs="+", help="One or more subnet netuids")
    parser.add_argument("--amount", type=float, help="Uniform TAO amount per subnet per iteration")
    parser.add_argument("--amounts", type=float, nargs="+", help="Per-subnet TAO amounts (must match --netuids length)")
    parser.add_argument("--tempo", type=float, help="Fixed seconds between iterations")
    parser.add_argument("--tempo-rand-x", type=float, help="Min seconds between iterations (random)")
    parser.add_argument("--tempo-rand-y", type=float, help="Max seconds between iterations (random)")
    parser.add_argument("--tolerance", type=float, default=None, help="Price tolerance (e.g. 0.05 = 5%%)")

    # Overridable args (work with both modes)
    parser.add_argument("--max-tao", type=float, help="Maximum total TAO to spend")
    parser.add_argument("--max-iterations", type=int, help="Maximum number of purchase attempts")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without submitting transactions")

    args = parser.parse_args()

    if args.strategy:
        # --- Strategy file mode ---
        config = load_strategy(args.strategy)

        # CLI overrides
        if args.dry_run:
            config["dry_run"] = True
        if args.max_tao is not None:
            config["max_tao"] = args.max_tao
        if args.max_iterations is not None:
            config["max_iterations"] = args.max_iterations

        main(**config)

    else:
        # --- CLI-only mode (backward compat) ---
        if not args.netuids:
            parser.error("Either --strategy or --netuids is required")
        if not args.wallet:
            parser.error("--wallet is required in CLI mode")
        if not args.hotkey:
            parser.error("--hotkey is required in CLI mode")

        # Validate tempo
        if args.tempo is None:
            if args.tempo_rand_x is None or args.tempo_rand_y is None:
                parser.error("Either --tempo OR both --tempo-rand-x and --tempo-rand-y must be set")
            if args.tempo_rand_x > args.tempo_rand_y:
                parser.error("--tempo-rand-x must be <= --tempo-rand-y")

        # Build subnet configs
        if args.amounts is not None:
            if len(args.amounts) != len(args.netuids):
                parser.error("--amounts must have the same number of values as --netuids")
            if args.amount is not None:
                parser.error("Cannot use both --amount and --amounts")
            amounts = dict(zip(args.netuids, args.amounts))
        elif args.amount is not None:
            amounts = {netuid: args.amount for netuid in args.netuids}
        else:
            parser.error("Either --amount or --amounts must be specified")

        subnet_configs = [
            SubnetConfig(
                netuid=netuid,
                amount=amounts[netuid],
                tempo=args.tempo,
                tempo_min=args.tempo_rand_x if args.tempo is None else None,
                tempo_max=args.tempo_rand_y if args.tempo is None else None,
            )
            for netuid in args.netuids
        ]

        main(
            network=args.network,
            wallet_name=args.wallet,
            hotkey=args.hotkey,
            subnet_configs=subnet_configs,
            tolerance=args.tolerance if args.tolerance is not None else DEFAULT_TOLERANCE,
            max_tao=args.max_tao,
            max_iterations=args.max_iterations,
            dry_run=args.dry_run,
        )
