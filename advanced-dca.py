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

import yaml
from loguru import logger
from rich.console import Console
from rich.table import Table

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------
ROOT_NETUID = 0
DEFAULT_TOLERANCE = 0.05
ALLOW_PARTIAL = True
MIN_ALPHA_ROOT_TO_KEEP_TAO = 0

console = Console()


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
    console.rule("[bold cyan]DCA SUMMARY[/bold cyan]")
    console.print()

    info_table = Table(show_header=False, box=None, padding=(0, 2))
    info_table.add_column("Key", style="bold")
    info_table.add_column("Value")
    info_table.add_row("Start", stats.start_time.strftime("%Y-%m-%d %H:%M:%S UTC"))
    info_table.add_row("End", end_time.strftime("%Y-%m-%d %H:%M:%S UTC"))
    info_table.add_row("Duration", duration_str)
    info_table.add_row("Network", network)
    info_table.add_row("Mode", "[yellow]DRY RUN[/yellow]" if dry_run else "LIVE")
    info_table.add_row("Total purchases", str(stats.total_iterations))
    info_table.add_row("Stop reason", stop_reason)
    console.print(info_table)
    console.print()

    table = Table(title="Per-Subnet Breakdown")
    table.add_column("Subnet", justify="right", style="cyan")
    table.add_column("Purchases", justify="right")
    table.add_column("Failed", justify="right", style="red")
    table.add_column("TAO Spent", justify="right", style="green")
    table.add_column("Est. Alpha", justify="right")
    table.add_column("Avg Price", justify="right")

    total_purchases = 0
    total_failed = 0
    total_tao = 0.0

    for netuid in sorted(stats.by_netuid.keys()):
        s = stats.by_netuid[netuid]
        n_ok = len(s.successful)
        n_fail = len(s.failed)
        total_purchases += n_ok
        total_failed += n_fail
        total_tao += s.total_tao_spent
        table.add_row(
            str(netuid),
            str(n_ok),
            str(n_fail),
            f"{s.total_tao_spent:.4f}",
            f"{s.total_alpha_estimated:.4f}",
            f"{s.avg_price:.6f}" if s.avg_price > 0 else "-",
        )

    table.add_section()
    table.add_row(
        "TOTAL",
        str(total_purchases),
        str(total_failed),
        f"{total_tao:.4f}",
        "",
        "",
        style="bold",
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

    subtensor = Subtensor(network=network, log_verbose=False)
    wallet = Wallet(name=wallet_name)
    coldkey = wallet.coldkeypub.ss58_address

    stats = DCAStats()
    prefix = "[DRY RUN] " if dry_run else ""

    # --- Initial info ---
    dynamic_infos = subtensor.all_subnets()
    dynamic_info_by_netuid = {d.netuid: d for d in dynamic_infos}

    logger.info(f"{prefix}Coldkey         : {coldkey}")
    logger.info(f"{prefix}Hotkey          : {hotkey}")
    logger.info(f"{prefix}Network         : {network}")
    logger.info(f"{prefix}Subnets         : {[sc.netuid for sc in subnet_configs]}")
    logger.info(f"{prefix}Amounts (TAO)   : {[sc.amount for sc in subnet_configs]}")
    logger.info(f"{prefix}Tolerance       : {tolerance * 100:.2f}%")

    if max_tao is not None:
        logger.info(f"{prefix}Max TAO budget  : {max_tao}")
    if max_iterations is not None:
        logger.info(f"{prefix}Max iterations  : {max_iterations}")

    for sc in subnet_configs:
        pool = dynamic_info_by_netuid.get(sc.netuid)
        if pool:
            logger.info(f"{prefix}  SN{sc.netuid} price   : {pool.price.tao:.9f} TAO/alpha")
        if sc.tempo is not None:
            logger.info(f"{prefix}  SN{sc.netuid} tempo   : every {sc.tempo}s (fixed)")
        else:
            logger.info(f"{prefix}  SN{sc.netuid} tempo   : every [{sc.tempo_min}s - {sc.tempo_max}s] (random)")

    if dry_run:
        logger.info("[DRY RUN] No transactions will be submitted")

    logger.info(f"{prefix}Press CTRL+C to stop")

    if not dry_run and wallet.coldkey_file.is_encrypted():
        wallet.unlock_coldkey()

    # --- Init scheduler: all subnets run immediately ---
    now = time.monotonic()
    for sc in subnet_configs:
        sc.next_run_at = now

    # Track consecutive no-funds per subnet to detect global exhaustion
    no_funds_streak: dict[int, bool] = {sc.netuid: False for sc in subnet_configs}

    stop_reason = "unknown"

    try:
        while True:
            # --- Find the subnet with the earliest next_run_at ---
            sc = min(subnet_configs, key=lambda s: s.next_run_at)

            # --- Sleep until it's time ---
            wait = sc.next_run_at - time.monotonic()
            if wait > 0:
                logger.info(f"{prefix}Next: SN{sc.netuid} in {wait:.1f}s")
                time.sleep(wait)

            stats.total_iterations += 1
            iteration = stats.total_iterations

            # --- Check stopping conditions ---
            if max_iterations is not None and iteration > max_iterations:
                stop_reason = f"max iterations reached ({max_iterations})"
                logger.info(f"{prefix}[{iteration:04}] {stop_reason}")
                break

            if max_tao is not None and stats.total_tao_spent >= max_tao:
                stop_reason = f"max TAO budget reached ({max_tao})"
                logger.info(f"{prefix}[{iteration:04}] {stop_reason}")
                break

            # --- Refresh chain state ---
            dynamic_infos = subtensor.all_subnets()
            dynamic_info_by_netuid = {d.netuid: d for d in dynamic_infos}
            balance = subtensor.get_balance(coldkey)
            alpha_root = subtensor.get_stake(coldkey, hotkey, ROOT_NETUID)

            netuid = sc.netuid
            pool = dynamic_info_by_netuid.get(netuid)

            logger.info(f"{prefix}[{iteration:04}] SN{netuid} balance={balance} | root_stake={alpha_root}")

            if pool is None:
                logger.warning(f"{prefix}[{iteration:04}] SN{netuid} not found, skipping")
                sc.schedule_next()
                continue

            price = pool.price.tao
            stake_amount = sc.amount

            # --- Check budget ---
            if max_tao is not None:
                remaining_budget = max_tao - stats.total_tao_spent
                if remaining_budget <= 0:
                    stop_reason = f"max TAO budget reached ({max_tao})"
                    logger.info(f"{prefix}[{iteration:04}] {stop_reason}")
                    break
                if stake_amount > remaining_budget:
                    stake_amount = remaining_budget
                    logger.info(f"{prefix}[{iteration:04}] SN{netuid} adjusted amount to {stake_amount:.4f} (budget limit)")

            stake_balance = Balance.from_tao(stake_amount)
            estimated_alpha = estimate_alpha_from_tao(stake_amount, price)

            action_taken = False

            # --- Unstake from root if balance is insufficient ---
            if balance.tao < stake_amount and alpha_root > Balance.from_tao(MIN_ALPHA_ROOT_TO_KEEP_TAO):
                total_needed = sum(s.amount for s in subnet_configs)
                shortfall = max(total_needed - balance.tao, 0)
                movable = alpha_root - Balance.from_tao(MIN_ALPHA_ROOT_TO_KEEP_TAO)
                unstake_amount = Balance.from_tao(min(shortfall, movable.tao))

                logger.info(f"{prefix}[{iteration:04}] Unstaking {unstake_amount} from root to cover all subnets (shortfall={shortfall:.4f})")

                if not dry_run:
                    try:
                        subtensor.unstake(
                            wallet=wallet,
                            netuid=ROOT_NETUID,
                            hotkey_ss58=hotkey,
                            amount=unstake_amount,
                        )
                        balance = subtensor.get_balance(coldkey)
                        alpha_root = subtensor.get_stake(coldkey, hotkey, ROOT_NETUID)
                        logger.info(f"[{iteration:04}] After unstake: balance={balance}, root={alpha_root}")
                    except Exception as e:
                        logger.exception(f"[{iteration:04}] UNSTAKE_EXCEPTION={e}")

            # --- add_stake ---
            has_funds = balance.tao > 0.01 and balance.tao >= stake_amount
            # In dry-run, trust that unstake would have provided funds
            if not has_funds and dry_run and alpha_root > Balance.from_tao(MIN_ALPHA_ROOT_TO_KEEP_TAO):
                has_funds = True

            if has_funds:
                logger.info(f"{prefix}[{iteration:04}] SN{netuid} action=add_stake amount={stake_balance} est_alpha={estimated_alpha:.4f} price={price:.9f}")

                if dry_run:
                    stats.record(SubnetPurchase(
                        iteration=iteration, timestamp=datetime.now(timezone.utc),
                        netuid=netuid, action="add_stake", tao_amount=stake_amount,
                        estimated_alpha=estimated_alpha, price=price, success=True, dry_run=True,
                    ))
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
                        logger.info(f"[{iteration:04}] SN{netuid} add_stake {success=}")
                        stats.record(SubnetPurchase(
                            iteration=iteration, timestamp=datetime.now(timezone.utc),
                            netuid=netuid, action="add_stake", tao_amount=stake_amount,
                            estimated_alpha=estimated_alpha, price=price, success=success, dry_run=False,
                        ))
                        action_taken = success
                    except Exception as e:
                        logger.exception(f"[{iteration:04}] SN{netuid} ADD_STAKE_EXCEPTION={e}")
                        stats.record(SubnetPurchase(
                            iteration=iteration, timestamp=datetime.now(timezone.utc),
                            netuid=netuid, action="add_stake", tao_amount=stake_amount,
                            estimated_alpha=estimated_alpha, price=price, success=False, dry_run=False,
                        ))
            else:
                logger.info(f"{prefix}[{iteration:04}] SN{netuid} no funds available (balance={balance}, root={alpha_root})")

            # --- Track no-funds streak ---
            no_funds_streak[netuid] = not action_taken
            if all(no_funds_streak.values()):
                stop_reason = "no funds available on any subnet"
                logger.info(f"{prefix}[{iteration:04}] {stop_reason}")
                break

            # --- Schedule next run for this subnet ---
            sc.schedule_next()

    except KeyboardInterrupt:
        stop_reason = "user interrupt (CTRL+C)"
        logger.info(f"{prefix}DCA stopped by user")

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
