"""Show all balances for a single wallet with full precision.

Usage:
    python display-wallet.py --wallet <name>
    python display-wallet.py --ss58 <address>
    python display-wallet.py --wallet <name> --network test
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

from base import fetch_delegate_identities, get_live_price

load_dotenv()

logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")

DEFAULT_WALLET_PATH = Path("~/.bittensor/wallets").expanduser()


def fmt_tao(value: float) -> str:
    return f"{value:.9f}"


async def main():
    parser = argparse.ArgumentParser(description="Show all balances for a single wallet")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--wallet", help="Wallet name")
    group.add_argument("--ss58", help="SS58 address")
    parser.add_argument("--network", default="finney", choices=["finney", "test"])
    args = parser.parse_args()

    if args.wallet:
        wallet = Wallet(name=args.wallet, path=str(DEFAULT_WALLET_PATH))
        coldkey_ss58 = wallet.coldkeypub.ss58_address
        label = args.wallet
    else:
        coldkey_ss58 = args.ss58
        label = coldkey_ss58

    logger.info(f"Fetching balances for {label} ({coldkey_ss58}) on {args.network}...")

    async with AsyncSubtensor(network=args.network) as subtensor:
        free_balance, stakes, dynamic_infos, delegate_identities = await asyncio.gather(
            subtensor.get_balance(coldkey_ss58),
            subtensor.get_stake_info_for_coldkey(coldkey_ss58),
            subtensor.all_subnets(),
            fetch_delegate_identities(subtensor),
        )

        dynamic_info_by_netuid = {d.netuid: d for d in dynamic_infos}
        tao_price = get_live_price() or 0.0

        console = Console()

        # Header
        console.print()
        console.print(f"[bold bright_white]Wallet:[/] [bright_green]{label}[/]")
        console.print(f"[bold bright_white]Coldkey:[/] [cyan]{coldkey_ss58}[/]")
        console.print(f"[bold bright_white]Network:[/] {args.network}")
        console.print(f"[bold bright_white]TAO Price:[/] [bright_magenta]${tao_price:,.2f}[/]")
        console.print()

        # Free balance
        free_tao = float(free_balance.tao)
        console.print(f"[bold bright_white]Free Balance:[/] [bright_red]{fmt_tao(free_tao)} τ[/]  (${free_tao * tao_price:,.2f})")
        console.print()

        # Stakes table
        table = Table(
            title="[bold bright_white]Stakes[/]",
            border_style="bright_blue",
            header_style="bold bright_cyan",
            padding=(0, 1),
        )
        table.add_column("Subnet", style="bold bright_green", justify="right")
        table.add_column("Hotkey", style="cyan")
        table.add_column("Validator", style="bright_yellow")
        table.add_column("Alpha (α)", justify="right", style="bright_blue")
        table.add_column("TAO value (τ)", justify="right", style="bright_white")
        table.add_column("TAO w/ slippage (τ)", justify="right", style="bright_yellow")
        table.add_column("USD", justify="right", style="bright_magenta")
        table.add_column("Locked (α)", justify="right", style="red")

        total_tao = 0.0
        total_slippage = 0.0

        sorted_stakes = sorted(stakes, key=lambda s: s.netuid) if stakes else []

        for stake in sorted_stakes:
            if stake.stake.rao == 0:
                continue

            pool = dynamic_info_by_netuid.get(stake.netuid)
            if pool is None:
                continue

            alpha_value = Balance.from_rao(int(stake.stake.rao)).set_unit(stake.netuid)
            tao_value = pool.alpha_to_tao(alpha_value)
            tao_float = float(tao_value.tao)

            if stake.netuid == 0:
                slippage_float = tao_float
            else:
                slippage_value, _ = pool.alpha_to_tao_with_slippage(stake.stake)
                slippage_float = float(slippage_value.tao)

            total_tao += tao_float
            total_slippage += slippage_float

            # Resolve validator name
            try:
                owner = await subtensor.get_hotkey_owner(stake.hotkey_ss58)
                identity = delegate_identities.get(owner)
                validator_name = identity.name if identity else ""
            except Exception:
                validator_name = ""

            locked_alpha = float(stake.locked.tao) if stake.locked and stake.locked.rao > 0 else 0.0

            table.add_row(
                str(stake.netuid),
                stake.hotkey_ss58,
                validator_name,
                fmt_tao(float(stake.stake.tao)),
                fmt_tao(tao_float),
                fmt_tao(slippage_float),
                f"${slippage_float * tao_price:,.2f}",
                fmt_tao(locked_alpha) if locked_alpha > 0 else "",
            )

        # Total row
        table.add_section()
        table.add_row(
            "",
            "[bold bright_white]TOTAL[/]",
            "",
            "",
            f"[bold]{fmt_tao(total_tao)}[/]",
            f"[bold]{fmt_tao(total_slippage)}[/]",
            f"[bold]${total_slippage * tao_price:,.2f}[/]",
            "",
            style="bold bright_white",
        )

        console.print(table)

        # Grand total
        grand_total = free_tao + total_slippage
        console.print()
        console.print(f"[bold bright_white]Grand Total (free + staked w/ slippage):[/] [bold bright_green]{fmt_tao(grand_total)} τ[/]  ([bold bright_magenta]${grand_total * tao_price:,.2f}[/])")
        console.print()


if __name__ == "__main__":
    asyncio.run(main())
