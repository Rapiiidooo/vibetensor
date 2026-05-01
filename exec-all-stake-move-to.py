# Move all alpha stake from a coldkey to a single destination hotkey (same coldkey)
# python exec-all-stake-move-to.py --wallet <WALLET> --network test --dest-hotkey <HK> [--dest-netuid <N>] [--standalone]
# python exec-all-stake-move-to.py --wallet <WALLET> --network finney --dest-hotkey <HK> [--dest-netuid <N>]

import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from base import get_live_price
from bittensor import Subtensor
from bittensor_wallet import Wallet

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")

# ------------------------------------------------------------------
# Output directory
# ------------------------------------------------------------------
OUTPUT_DIR = Path(__file__).resolve().parent / "logs" / "exec-all-stake-move-to"
OUTPUT_DIR.mkdir(exist_ok=True)

# ------------------------------------------------------------------
# Chain minimums (in TAO, as float)
# ------------------------------------------------------------------
MIN_TAO = 0.002
MIN_ALPHA = 0.002

SUBNETS_TO_SKIP = []


def build_snapshot(included_rows: list[dict], tao_price: float) -> dict:
    by_subnet = {}
    total_staked_tao = 0.0

    for row in included_rows:
        netuid = row["netuid"]
        alpha = row["alpha_tao"]
        tao = row["tao"]
        usd = tao * tao_price

        if netuid not in by_subnet:
            by_subnet[netuid] = {"alpha": 0.0, "tao": 0.0, "usd": 0.0}

        by_subnet[netuid]["alpha"] += alpha
        by_subnet[netuid]["tao"] += tao
        by_subnet[netuid]["usd"] += usd
        total_staked_tao += tao

    return {
        "tao_price_usd": tao_price,
        "staked_tao": total_staked_tao,
        "total_usd": total_staked_tao * tao_price,
        "by_subnet": by_subnet,
    }


def collect_eligible_stakes(stakes, dynamic_info_by_netuid, dest_hotkey, dest_netuid):
    """Filter stakes that are worth moving and not already at destination."""
    eligible = []

    for stake in stakes:
        netuid = stake.netuid
        hotkey = stake.hotkey_ss58
        dst_netuid = dest_netuid if dest_netuid is not None else netuid

        if netuid in SUBNETS_TO_SKIP:
            continue

        if hotkey == dest_hotkey and netuid == dst_netuid:
            continue

        pool = dynamic_info_by_netuid.get(netuid)
        if pool is None:
            continue

        tao_value = pool.alpha_to_tao(stake.stake)
        if stake.stake.tao < MIN_ALPHA or tao_value.tao < MIN_TAO:
            continue

        eligible.append({
            "stake": stake,
            "netuid": netuid,
            "hotkey": hotkey,
            "dst_netuid": dst_netuid,
            "tao_value": tao_value,
            "pool": pool,
        })

    return eligible


def log_portfolio_summary(portfolio_snapshot, tao_price):
    logger.info("--------------------------------------------------")
    logger.info(f"TAO price (USD)      : ${tao_price:,.2f}")
    logger.info(f"Included subnets     : {len(portfolio_snapshot['by_subnet'])}")
    logger.info(f"Included staked TAO  : {portfolio_snapshot['staked_tao']:.6f}")
    logger.info(f"Included value USD   : ${portfolio_snapshot['total_usd']:,.2f}")
    logger.info("--------------------------------------------------")


def compose_move_call(substrate, e, dest_hotkey):
    return substrate.compose_call(
        call_module="SubtensorModule",
        call_function="move_stake",
        call_params={
            "origin_netuid": e["netuid"],
            "origin_hotkey": e["hotkey"],
            "destination_netuid": e["dst_netuid"],
            "destination_hotkey": dest_hotkey,
            "alpha_amount": int(e["stake"].stake.rao),
        },
    )


def print_report(results: list[dict], coldkey: str, dest_hotkey: str, network: str, mode: str, tao_price: float):
    succeeded = [r for r in results if r["status"] == "success"]
    failed = [r for r in results if r["status"] != "success"]

    total_tao = sum(r["tao_value"] for r in succeeded)
    total_usd = total_tao * tao_price

    lines = [
        "",
        "=" * 88,
        "  STAKE MOVE REPORT",
        "=" * 88,
        f"  Date            : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"  Network         : {network}",
        f"  Mode            : {mode}",
        f"  Coldkey         : {coldkey}",
        f"  Dest hotkey     : {dest_hotkey}",
        f"  TAO price       : ${tao_price:,.2f}",
        "-" * 88,
        f"  {'Subnet':>6} → {'Dst':>4}  {'Origin hotkey':>48}  {'Alpha':>14}  {'TAO':>14}  {'USD':>10}  {'Status'}",
        "-" * 88,
    ]

    for r in results:
        usd = r["tao_value"] * tao_price
        tag = "OK" if r["status"] == "success" else "FAIL"
        lines.append(
            f"  {r['netuid']:>6} → {r['dst_netuid']:>4}  {r['hotkey']:>48}  "
            f"{r['alpha_tao']:>14.6f}  {r['tao_value']:>14.6f}  ${usd:>9,.2f}  {tag}"
        )

    lines.extend([
        "-" * 88,
        f"  Moves           : {len(results)} total, {len(succeeded)} success, {len(failed)} failed",
        f"  Total TAO moved : {total_tao:.6f}",
        f"  Total USD value : ${total_usd:,.2f}",
        "=" * 88,
    ])

    logger.info("\n".join(lines))


def save_accounting_csv(
        results: list[dict],
        coldkey: str,
        dest_hotkey: str,
        network: str,
        mode: str,
        tao_price: float,
        extrinsic_id: str | None = None,
) -> str:
    ts = datetime.now(timezone.utc)
    filename = OUTPUT_DIR / f"stake-move-{ts.strftime('%Y%m%d-%H%M%S')}.csv"

    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "date", "timestamp_utc", "network", "mode",
            "coldkey", "dest_hotkey",
            "origin_netuid", "dest_netuid", "origin_hotkey",
            "alpha_amount", "tao_value", "usd_value", "tao_price_usd",
            "status", "extrinsic_id",
        ])
        for r in results:
            writer.writerow([
                ts.strftime("%Y-%m-%d"),
                ts.strftime("%Y-%m-%d %H:%M:%S"),
                network,
                mode,
                coldkey,
                dest_hotkey,
                r["netuid"],
                r["dst_netuid"],
                r["hotkey"],
                f"{r['alpha_tao']:.9f}",
                f"{r['tao_value']:.9f}",
                f"{r['tao_value'] * tao_price:.2f}",
                f"{tao_price:.2f}",
                r["status"],
                extrinsic_id or "",
            ])

    logger.info(f"Accounting CSV saved: {filename}")
    return filename


def main(wallet_name: str, network: str, dest_hotkey: str, dest_netuid: int | None, standalone: bool):
    subtensor = Subtensor(network=network, log_verbose=False)
    wallet = Wallet(name=wallet_name)

    coldkey = wallet.coldkeypub.ss58_address

    logger.info(f"Coldkey            : {coldkey}")
    logger.info(f"Destination hotkey : {dest_hotkey}")
    logger.info(f"Destination netuid : {dest_netuid if dest_netuid is not None else 'same as origin'}")
    logger.info(f"Mode               : {'standalone (one extrinsic per move)' if standalone else 'batch'}")

    stakes = subtensor.get_stake_info_for_coldkey(coldkey)
    if not stakes:
        logger.error("No stakes found on coldkey")
        return

    dynamic_info_by_netuid = {d.netuid: d for d in subtensor.all_subnets()}

    tao_price = get_live_price() or 0.0
    if tao_price == 0.0:
        logger.warning("Could not fetch TAO price — USD values will show as $0")

    eligible = collect_eligible_stakes(stakes, dynamic_info_by_netuid, dest_hotkey, dest_netuid)
    if not eligible:
        logger.error("No eligible stake moves found")
        return

    included_rows = [
        {
            "netuid": e["netuid"],
            "hotkey": e["hotkey"],
            "alpha_tao": float(e["stake"].stake.tao),
            "tao": float(e["tao_value"].tao),
        }
        for e in eligible
    ]

    portfolio_snapshot = build_snapshot(included_rows, tao_price)
    log_portfolio_summary(portfolio_snapshot, tao_price)

    for idx, e in enumerate(eligible):
        logger.info(
            f"[{idx:03}] queued | netuid {e['netuid']} → {e['dst_netuid']} | "
            f"{e['hotkey']} → {dest_hotkey} | alpha={e['stake'].stake} | tao={e['tao_value']}"
        )

    # Unlock after preview so user sees the summary before entering password
    if wallet.coldkey_file.is_encrypted():
        wallet.unlock_coldkey()

    # ==================================================================
    # STANDALONE MODE
    # ==================================================================
    if standalone:
        input("Press Enter to start standalone moves...")

        results = []

        for e in eligible:
            logger.info(
                f"Submitting move | netuid={e['netuid']} → {e['dst_netuid']} | "
                f"{e['hotkey']} → {dest_hotkey} | alpha={e['stake'].stake} | tao={e['tao_value']}"
            )

            call = compose_move_call(subtensor.substrate, e, dest_hotkey)
            extrinsic = subtensor.substrate.create_signed_extrinsic(call=call, keypair=wallet.coldkey)
            response = subtensor.substrate.submit_extrinsic(extrinsic, wait_for_inclusion=True,
                                                            wait_for_finalization=True)
            response.process_events()

            status = "success" if response.is_success else "failed"
            if not response.is_success:
                logger.error(f"Move failed | netuid={e['netuid']} | error={response.error_message}")

            results.append({
                "netuid": e["netuid"],
                "dst_netuid": e["dst_netuid"],
                "hotkey": e["hotkey"],
                "alpha_tao": float(e["stake"].stake.tao),
                "tao_value": float(e["tao_value"].tao),
                "status": status,
            })

            time.sleep(0.2)

        print_report(results, coldkey, dest_hotkey, network, "standalone", tao_price)
        save_accounting_csv(results, coldkey, dest_hotkey, network, "standalone", tao_price)
        return

    # ==================================================================
    # BATCH MODE (force_batch: continues even if individual calls fail)
    # ==================================================================
    calls = [compose_move_call(subtensor.substrate, e, dest_hotkey) for e in eligible]
    batch_call = subtensor.substrate.compose_call("Utility", "force_batch", {"calls": calls})

    fee = subtensor.substrate.get_payment_info(call=batch_call, keypair=wallet.coldkeypub)["partial_fee"]
    logger.info(f"Estimated fee: {fee / 1e9:.9f} TAO")

    extrinsic = subtensor.substrate.create_signed_extrinsic(batch_call, wallet.coldkey)

    input("Press Enter to submit batch moves...")

    response = subtensor.substrate.submit_extrinsic(extrinsic, wait_for_inclusion=True, wait_for_finalization=True)
    response.process_events()

    batch_status = "success" if response.is_success else "failed"
    if response.is_success:
        logger.info(f"Batch submitted successfully | {len(calls)} moves")
    else:
        logger.error(f"Batch failed: {response.error_message}")

    extrinsic_id = response.get_extrinsic_identifier()

    results = [
        {
            "netuid": e["netuid"],
            "dst_netuid": e["dst_netuid"],
            "hotkey": e["hotkey"],
            "alpha_tao": float(e["stake"].stake.tao),
            "tao_value": float(e["tao_value"].tao),
            "status": batch_status,
        }
        for e in eligible
    ]

    print_report(results, coldkey, dest_hotkey, network, "batch", tao_price)
    save_accounting_csv(results, coldkey, dest_hotkey, network, "batch", tao_price, extrinsic_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Move all stake to a single destination hotkey (batch or standalone)")
    parser.add_argument("--wallet", required=True)
    parser.add_argument("--network", required=True, choices=["finney", "test"], default="finney")
    parser.add_argument("--dest-hotkey", required=True)
    parser.add_argument("--dest-netuid", type=int, default=None,
                        help="Destination netuid (defaults to same netuid as origin stake)")
    parser.add_argument("--standalone", action="store_true")

    args = parser.parse_args()
    main(args.wallet, args.network, args.dest_hotkey, args.dest_netuid, args.standalone)
