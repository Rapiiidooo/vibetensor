"""Display detailed validator and miner statistics for a specific subnet.

Usage:
    python stats-subnet.py --netuid 19
    python stats-subnet.py --netuid 19 --weights
    python stats-subnet.py --netuid 19 --sort trust
    python stats-subnet.py --netuid 19 --full-keys
    python stats-subnet.py --netuid 19 --network test
"""

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

from bittensor import AsyncSubtensor, BLOCKTIME
from bittensor_wallet import Wallet
from dotenv import load_dotenv
from loguru import logger
from rich.console import Console
from rich.table import Table

from base import get_live_price

load_dotenv()

logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")

DEFAULT_WALLET_PATH = Path("~/.bittensor/wallets").expanduser()
U16_MAX = 65535


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def prettify_time(seconds: float) -> str:
    delta = timedelta(seconds=seconds)
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{days:02}d:{hours:02}h:{minutes:02}m"


def discover_coldkeys(wallet_path: Path = DEFAULT_WALLET_PATH) -> set[str]:
    """Return set of local coldkey SS58 addresses for 'mine' detection."""
    coldkeys = set()
    if not wallet_path.is_dir():
        return coldkeys
    for entry in wallet_path.iterdir():
        if not entry.is_dir():
            continue
        try:
            w = Wallet(name=entry.name, path=str(wallet_path))
            coldkeys.add(w.coldkeypub.ss58_address)
        except Exception:
            continue
    return coldkeys


async def get_delegate_take(subtensor: AsyncSubtensor, hotkey_ss58: str) -> float:
    try:
        result = await subtensor.substrate.query(
            module="SubtensorModule",
            storage_function="Delegates",
            params=[hotkey_ss58],
        )
        if result is not None:
            return (int(result.value) / U16_MAX) * 100
    except Exception:
        pass
    return 0.0


async def get_childkey_take(subtensor: AsyncSubtensor, hotkey_ss58: str, netuid: int) -> float:
    try:
        result = await subtensor.substrate.query(
            module="SubtensorModule",
            storage_function="ChildkeyTake",
            params=[hotkey_ss58, netuid],
        )
        if result is not None:
            return (int(result.value) / U16_MAX) * 100
    except Exception:
        pass
    return 0.0


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------


async def main():
    parser = argparse.ArgumentParser(description="Show detailed subnet statistics")
    parser.add_argument("--netuid", type=int, required=True, help="Subnet ID")
    parser.add_argument("--weights", action="store_true", help="Show validator weight details (slower)")
    parser.add_argument("--full-keys", action="store_true", dest="full_keys", help="Show full hotkey/coldkey")
    parser.add_argument("--sort", default="emission", choices=["emission", "vtrust"], help="Sort order")
    parser.add_argument("--decimals", type=int, default=3, help="Decimal places for rounding")
    parser.add_argument("--block", type=int, default=None, help="Query at specific block")
    parser.add_argument("--network", default="finney", choices=["finney", "test"], help="Bittensor network")
    args = parser.parse_args()

    my_coldkeys = discover_coldkeys()
    tao_price = get_live_price() or 0.0
    if tao_price == 0.0:
        logger.warning("Could not fetch TAO price — USD values will show as $0")

    console = Console()

    async with AsyncSubtensor(network=args.network) as subtensor:
        logger.info(f"Network  : {subtensor.chain_endpoint}")
        logger.info(f"Subnet   : {args.netuid}")

        reg_cost = await subtensor.recycle(netuid=args.netuid, block=args.block)
        if reg_cost is not None:
            reg_tao = float(reg_cost.tao)
            logger.info(f"Reg cost : {reg_tao:.4f} τ (${reg_tao * tao_price:,.2f})")

        lite = not args.weights
        metagraph = await subtensor.metagraph(args.netuid, lite=lite, block=args.block)

        if args.block:
            current_block = args.block
        else:
            current_block = await subtensor.block

        curr_block = metagraph.block
        uids = metagraph.uids.tolist()

        # Pool info for alpha→TAO conversion
        pool = metagraph.pool
        alpha_token_price = pool.tao_in / pool.alpha_in if pool.alpha_in > 0 else 0.0

        # Tempo calculations
        tempo_blocks = metagraph.tempo
        tempo_seconds = tempo_blocks * BLOCKTIME
        tempos_per_day = (86400 / tempo_seconds) if tempo_seconds > 0 else 0

        # Identities from metagraph
        identities = metagraph.identities or []

        # Block at registration from metagraph
        block_at_reg = metagraph.block_at_registration or []

        # Immunity period (in blocks) for this subnet
        immunity_period = await subtensor.immunity_period(netuid=args.netuid, block=args.block) or 0

        validators = []
        miners = []

        for uid in uids:
            axon = metagraph.axons[uid]
            ip_address = axon.ip
            port = axon.port
            full_address = f"{ip_address}:{port}"

            hotkey = metagraph.hotkeys[uid]
            coldkey = metagraph.coldkeys[uid]
            stake = float(metagraph.stake[uid])
            last_update = int(metagraph.last_update[uid])
            calc_last_update = int(curr_block - last_update)

            emission = float(metagraph.E[uid])
            vtrust = float(metagraph.validator_trust[uid])
            is_validator = vtrust > 0.01

            is_mine = coldkey in my_coldkeys

            # Registration info
            reg_block = int(block_at_reg[uid]) if uid < len(block_at_reg) else 0
            since_reg = prettify_time((current_block - reg_block) * BLOCKTIME) if reg_block > 0 else "?"

            # Immunity: neuron is immune while (current_block - reg_block) < immunity_period
            if reg_block > 0 and immunity_period > 0:
                blocks_remaining = immunity_period - (current_block - reg_block)
                if blocks_remaining > 0:
                    immune_str = prettify_time(blocks_remaining * BLOCKTIME)
                else:
                    immune_str = ""
            else:
                immune_str = ""

            # Daily rewards
            daily_alpha = tempos_per_day * emission
            daily_tao = daily_alpha * alpha_token_price
            daily_usd = daily_tao * tao_price

            # Identity resolution
            identity = identities[uid] if uid < len(identities) and identities[uid] else None
            if identity is None:
                identity_name = ""
            elif hasattr(identity, "name"):
                identity_name = identity.name
            elif isinstance(identity, dict):
                identity_name = identity.get("name", "")
            else:
                identity_name = str(identity)

            # Key display — hotkey always full, coldkey shows identity or truncated
            display_hotkey = hotkey
            if args.full_keys:
                display_coldkey = coldkey
            else:
                display_coldkey = identity_name if identity_name else coldkey[:12]

            row = {
                "address": full_address,
                "uid": uid,
                "axon": axon.version,
                "last_upd": calc_last_update,
                "stake": stake,
                "emission": emission,
                "alpha_d": daily_alpha,
                "tao_d": daily_tao,
                "usd_d": daily_usd,
                "vtrust": vtrust,
                "coldkey": display_coldkey,
                "hotkey": display_hotkey,
                "reg_since": since_reg,
                "immune": immune_str,
                "is_mine": is_mine,
                "take": None,
                "childkey_take": None,
            }

            if is_validator:
                validators.append(row)
            else:
                miners.append(row)

        # Fetch takes for validators concurrently
        async def _fetch_takes(row):
            dt, ct = await asyncio.gather(
                get_delegate_take(subtensor, row["hotkey"]),
                get_childkey_take(subtensor, row["hotkey"], args.netuid),
            )
            row["take"] = dt
            row["childkey_take"] = ct

        await asyncio.gather(*[_fetch_takes(r) for r in validators])

        # Sort
        if args.sort == "emission":
            sort_key = lambda r: (-r["emission"], -r["vtrust"])
        else:
            sort_key = lambda r: (-r["vtrust"], -r["emission"])

        validators.sort(key=sort_key)
        miners.sort(key=sort_key)

        # Render
        def build_table(title: str, rows: list[dict], decimals: int, show_take: bool = True) -> Table:
            table = Table(
                title=f"[bold bright_white]{title}[/]",
                title_style="bold bright_white",
                border_style="bright_blue",
                header_style="bold bright_cyan",
                padding=(0, 1),
            )

            table.add_column("P.", justify="right", style="bold bright_white")
            table.add_column("UID", justify="right")
            table.add_column("Address", style="dim")
            table.add_column("Axon", justify="right")
            table.add_column("Last Upd.", justify="right")
            table.add_column("Stake", justify="right")
            table.add_column("Emission", justify="right")
            table.add_column("α/d", justify="right")
            table.add_column("τ/d", justify="right")
            table.add_column("$/d", justify="right", style="bright_magenta")
            table.add_column("VTrust", justify="right")
            if show_take:
                table.add_column("Take %", justify="right")
                table.add_column("CK Take %", justify="right")
            table.add_column("Coldkey", style="bright_white")
            table.add_column("Hotkey", style="cyan", no_wrap=True)
            table.add_column("Reg Since")
            table.add_column("Immune", justify="right", style="yellow")
            table.add_column("Mine", justify="center")

            for idx, r in enumerate(rows, start=1):
                style = "bold bright_green" if r["is_mine"] else ""

                cells = [
                    str(idx),
                    str(r["uid"]),
                    r["address"],
                    str(r["axon"]),
                    str(r["last_upd"]),
                    f"{r['stake']:,.{decimals}f}",
                    f"{r['emission']:.{decimals}f}",
                    f"{r['alpha_d']:.{decimals}f}",
                    f"{r['tao_d']:.{decimals}f}",
                    f"{r['usd_d']:.2f}",
                    f"{r['vtrust']:.{decimals}f}",
                ]
                if show_take:
                    cells.append(f"{r['take']:.1f}" if r.get("take") is not None else "")
                    cells.append(f"{r['childkey_take']:.1f}" if r.get("childkey_take") is not None else "")
                cells.extend([
                    r["coldkey"],
                    r["hotkey"],
                    r["reg_since"],
                    r["immune"],
                    "[bright_green]✓[/bright_green]" if r["is_mine"] else "",
                ])

                table.add_row(*cells, style=style)

            return table

        console.print(build_table(f"Validators — SN{args.netuid}", validators, args.decimals, show_take=True))
        console.print()
        console.print(build_table(f"Miners — SN{args.netuid}", miners, args.decimals, show_take=False))

        # Summary (only count nodes with emission > 0)
        active_validators = [r for r in validators if r["emission"] > 0]
        active_miners = [r for r in miners if r["emission"] > 0]
        val_emission = sum(r["emission"] for r in active_validators)
        miner_emission = sum(r["emission"] for r in active_miners)

        console.print()
        summary = Table(
            title="[bold bright_white]Summary[/]",
            border_style="bright_blue",
            header_style="bold bright_cyan",
        )
        summary.add_column("Metric", style="bright_white")
        summary.add_column("Validators", justify="right", style="bright_green")
        summary.add_column("Miners", justify="right", style="bright_yellow")

        summary.add_row(
            "Count (active / total)",
            f"{len(active_validators)} / {len(validators)}",
            f"{len(active_miners)} / {len(miners)}",
        )
        summary.add_row("Emission/epoch", f"{val_emission:.4f}", f"{miner_emission:.4f}")
        summary.add_row("Emission/day", f"{val_emission * tempos_per_day:.4f}", f"{miner_emission * tempos_per_day:.4f}")
        summary.add_row(
            "TAO/day",
            f"{val_emission * tempos_per_day * alpha_token_price:.4f}",
            f"{miner_emission * tempos_per_day * alpha_token_price:.4f}",
        )
        summary.add_row(
            "USD/day",
            f"${val_emission * tempos_per_day * alpha_token_price * tao_price:,.2f}",
            f"${miner_emission * tempos_per_day * alpha_token_price * tao_price:,.2f}",
        )
        console.print(summary)

        # Weights analysis
        if args.weights:
            console.print()
            console.rule("[bold bright_white]Weight Analysis[/bold bright_white]")

            for uid in uids:
                vtrust = float(metagraph.validator_trust[uid])
                stake = float(metagraph.stake[uid])
                if vtrust <= 0.01 or stake < 1000:
                    continue

                weights_row = metagraph.W[uid]
                nonzero = [(int(target_uid), float(w)) for target_uid, w in enumerate(weights_row) if float(w) > 0]
                if not nonzero:
                    continue

                nonzero.sort(key=lambda x: -x[1])

                identity = identities[uid] if uid < len(identities) and identities[uid] else None
                name = identity.name if identity else metagraph.hotkeys[uid][:12]

                console.print(
                    f"\n[bold bright_cyan]UID {uid}[/bold bright_cyan] ({name}) — "
                    f"stake {stake:,.2f} α — vtrust {vtrust:.4f} — "
                    f"{len(nonzero)} non-zero weights"
                )

                wt = Table(border_style="dim", header_style="bold", padding=(0, 1))
                wt.add_column("Target UID", justify="right")
                wt.add_column("Weight", justify="right")
                wt.add_column("Hotkey", style="cyan")
                wt.add_column("Mine", justify="center")

                for target_uid, w in nonzero[:20]:
                    target_coldkey = metagraph.coldkeys[target_uid]
                    target_hotkey = metagraph.hotkeys[target_uid]
                    is_mine = target_coldkey in my_coldkeys

                    wt.add_row(
                        str(target_uid),
                        f"{w:.6f}",
                        target_hotkey[:12] if not args.full_keys else target_hotkey,
                        "[bright_green]✓[/bright_green]" if is_mine else "",
                        style="bold bright_green" if is_mine else "",
                    )

                if len(nonzero) > 20:
                    wt.add_row("...", f"+{len(nonzero) - 20} more", "", "")

                console.print(wt)


if __name__ == "__main__":
    asyncio.run(main())
