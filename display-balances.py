"""Show TAO balances (free + staked) across all local wallets.

Usage:
    python display-balances.py
    python display-balances.py --details
    python display-balances.py --wallets wallet1 wallet2
    python display-balances.py --exclude-wallets wallet3
"""

import argparse
import asyncio
import sys
from pathlib import Path

from bittensor import AsyncSubtensor, Balance
from bittensor_wallet import Wallet
from dotenv import load_dotenv
from loguru import logger
from rich.console import Console
from rich.table import Table

from base import get_live_price

load_dotenv()

logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")

DISPLAYED_MIN_USD_VALUE = 100
DEFAULT_WALLET_PATH = Path("~/.bittensor/wallets").expanduser()


# ---------------------------------------------------------------------
# Wallet discovery
# ---------------------------------------------------------------------


def discover_wallets(wallet_path: Path = DEFAULT_WALLET_PATH) -> list[Wallet]:
    wallets = []
    if not wallet_path.is_dir():
        logger.warning(f"Wallet path not found: {wallet_path}")
        return wallets
    for entry in sorted(wallet_path.iterdir()):
        if not entry.is_dir():
            continue
        try:
            w = Wallet(name=entry.name, path=str(wallet_path))
            _ = w.coldkeypub.ss58_address
            wallets.append(w)
        except Exception:
            continue
    return wallets


# ---------------------------------------------------------------------
# Stake computation
# ---------------------------------------------------------------------


async def compute_wallet_stakes(
    subtensor: AsyncSubtensor,
    coldkey_ss58: str,
    dynamic_info_by_netuid: dict,
) -> tuple[float, float, list]:
    stakes = await subtensor.get_stake_info_for_coldkey(coldkey_ss58)
    if not stakes:
        return 0.0, 0.0, []

    total_tao = 0.0
    total_with_slippage = 0.0

    for stake in stakes:
        if stake.stake.rao == 0:
            continue

        pool = dynamic_info_by_netuid.get(stake.netuid)
        if pool is None:
            continue

        alpha_value = Balance.from_rao(int(stake.stake.rao)).set_unit(stake.netuid)
        tao_value = pool.alpha_to_tao(alpha_value)
        total_tao += float(tao_value.tao)

        if stake.netuid == 0:
            total_with_slippage += float(tao_value.tao)
        else:
            slippage_value, _ = pool.alpha_to_tao_with_slippage(stake.stake)
            total_with_slippage += float(slippage_value.tao)

    return total_tao, total_with_slippage, stakes


# ---------------------------------------------------------------------
# Identity cache
# ---------------------------------------------------------------------


class IdentityResolver:
    def __init__(self, subtensor: AsyncSubtensor, delegate_identities: dict):
        self._subtensor = subtensor
        self._identities = delegate_identities
        self._cache: dict[str, str] = {}

    async def resolve(self, hotkey_ss58: str) -> str:
        if hotkey_ss58 in self._cache:
            return self._cache[hotkey_ss58]

        try:
            coldkey = await self._subtensor.get_hotkey_owner(hotkey_ss58)
            identity = self._identities.get(coldkey)
            name = identity.name if identity else ""
        except Exception:
            name = ""

        self._cache[hotkey_ss58] = name
        return name


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------


async def main():
    parser = argparse.ArgumentParser(description="Show TAO balances across all local wallets")
    parser.add_argument("--details", action="store_true", help="Show per-subnet stake breakdown")
    parser.add_argument("--wallets", nargs="*", default=[], help="Only show these wallets")
    parser.add_argument("--exclude-wallets", nargs="*", default=[], dest="exclude_wallets", help="Exclude these wallets")
    parser.add_argument("--min-usd", type=float, default=DISPLAYED_MIN_USD_VALUE, help="Min USD to show in details")
    parser.add_argument("--all", action="store_true", help="Show all wallets including empty ones")
    parser.add_argument("--network", default="finney", choices=["finney", "test"], help="Bittensor network")
    args = parser.parse_args()

    all_wallets = discover_wallets()
    if not all_wallets:
        logger.error(f"No wallets found in {DEFAULT_WALLET_PATH}")
        return

    if args.wallets:
        wallets = [w for w in all_wallets if w.name in args.wallets]
    else:
        wallets = [w for w in all_wallets if w.name not in args.exclude_wallets]

    if not wallets:
        logger.error("No wallets match the filter")
        return

    logger.info(f"Found {len(wallets)} wallet(s), connecting to {args.network}...")

    async with AsyncSubtensor(network=args.network) as subtensor:
        coldkeys = [w.coldkeypub.ss58_address for w in wallets]
        wallets_by_name = {w.name: w for w in wallets}

        dynamic_infos = await subtensor.all_subnets()
        dynamic_info_by_netuid = {d.netuid: d for d in dynamic_infos}

        free_balances, delegate_identities = await asyncio.gather(
            subtensor.get_balances(*coldkeys),
            subtensor.get_delegate_identities(),
        )

        stake_tasks = {
            ss58: compute_wallet_stakes(subtensor, ss58, dynamic_info_by_netuid)
            for ss58 in coldkeys
        }
        stake_results = dict(zip(stake_tasks.keys(), await asyncio.gather(*stake_tasks.values())))

        resolver = IdentityResolver(subtensor, delegate_identities)

        tao_price = get_live_price() or 0.0
        if tao_price == 0.0:
            logger.warning("Could not fetch TAO price — USD values will show as $0")

        # Build table
        console = Console()
        table = Table(
            title="[bold bright_white]TAO Balances[/]",
            title_style="bold bright_white",
            border_style="bright_blue",
            header_style="bold bright_cyan",

            padding=(0, 1),
        )
        table.add_column("Name", style="bold bright_green")
        table.add_column("Coldkey", style="cyan")
        if args.details:
            table.add_column("Hotkey", style="cyan", no_wrap=True)
        table.add_column("Free", justify="right", style="bright_red")
        table.add_column("Staked", justify="right", style="bright_blue")
        table.add_column("Staked (slippage)", justify="right", style="bright_yellow")
        table.add_column("Total", justify="right", style="bold bright_green")
        table.add_column("USD", justify="right", style="bold bright_magenta")

        grand_free = 0.0
        grand_staked = 0.0
        grand_slippage = 0.0

        for name in sorted(wallets_by_name.keys()):
            w = wallets_by_name[name]
            coldkey = w.coldkeypub.ss58_address

            free = float(free_balances.get(coldkey, Balance(0)).tao)
            staked, slippage, stakes = stake_results.get(coldkey, (0.0, 0.0, []))

            total = free + slippage
            if total <= 0.001 and not args.all:
                continue

            usd = total * tao_price

            grand_free += free
            grand_staked += staked
            grand_slippage += slippage

            row = [
                name,
                coldkey,
            ]
            if args.details:
                row.append("")
            row += [
                f"{free:,.3f}",
                f"{staked:,.3f}",
                f"{slippage:,.3f} τ",
                f"{total:,.3f}",
                f"${usd:,.2f}",
            ]
            table.add_row(*row)

            if args.details:
                for stake in sorted(stakes, key=lambda s: s.netuid):
                    if stake.stake.rao == 0:
                        continue

                    pool = dynamic_info_by_netuid.get(stake.netuid)
                    if pool is None:
                        continue

                    alpha_value = Balance.from_rao(int(stake.stake.rao)).set_unit(stake.netuid)
                    tao_val = pool.alpha_to_tao(alpha_value)
                    stake_usd = float(tao_val.tao) * tao_price

                    if not args.all and stake_usd < args.min_usd:
                        continue

                    identity = await resolver.resolve(stake.hotkey_ss58)
                    label = f"  └─ sn{stake.netuid}"
                    if identity:
                        label += f" ({identity})"
                    hotkey_display = stake.hotkey_ss58

                    table.add_row(
                        label,
                        "",
                        hotkey_display,
                        "",
                        f"{stake.stake.tao:,.3f} α",
                        f"{float(tao_val.tao):,.3f} τ",
                        "",
                        f"${stake_usd:,.2f}",
                        style="dim italic",
                    )

        # Total row
        table.add_section()
        grand_total = grand_free + grand_slippage
        total_row = ["TOTAL", ""]
        if args.details:
            total_row.append("")
        total_row += [
            f"{grand_free:,.3f}",
            f"{grand_staked:,.3f}",
            f"{grand_slippage:,.3f} τ",
            f"{grand_total:,.3f}",
            f"${grand_total * tao_price:,.2f}",
        ]
        table.add_row(*total_row, style="bold bright_white")

        console.print(table)


if __name__ == "__main__":
    asyncio.run(main())
