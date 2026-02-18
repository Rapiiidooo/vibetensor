# Transfer all alpha tokens (including root) from one coldkey to another
# python exec-all-stake-transfer.py --wallet <WALLET> --network test --dest-coldkey <DEST> [--standalone]
# python exec-all-stake-transfer.py --wallet <WALLET> --network finney --dest-coldkey <DEST>

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
OUTPUT_DIR = Path(__file__).resolve().parent / "exec-all-stake-transfer"
OUTPUT_DIR.mkdir(exist_ok=True)

# ------------------------------------------------------------------
# Chain minimums (in TAO, as float)
# ------------------------------------------------------------------
MIN_TAO = 0.002
MIN_ALPHA = 0.002

SUBNETS_TO_SKIP = []


def is_transfer_enabled(subtensor: Subtensor, netuid: int, cache: dict[int, bool]) -> bool:
    """
    Cached check for subnet hyperparameter `transfers_enabled`.
    Defaults to False if missing or error.
    """
    if netuid in cache:
        return cache[netuid]

    try:
        hp = subtensor.get_subnet_hyperparameters(netuid=netuid)
        enabled = bool(getattr(hp, "transfers_enabled", False))
    except Exception as e:
        logger.warning(f"failed to fetch transfers_enabled for subnet {netuid}: {e}")
        enabled = False

    cache[netuid] = enabled

    if not enabled:
        logger.warning(f"transfers_disabled | skipping subnet {netuid}")

    return enabled


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


def collect_eligible_stakes(stakes, dynamic_info_by_netuid, subtensor, transfer_enabled_cache):
    """Single-pass collection of eligible stakes with their computed values."""
    eligible = []
    for stake in stakes:
        netuid = stake.netuid
        if netuid in SUBNETS_TO_SKIP:
            continue

        if not is_transfer_enabled(subtensor, netuid, transfer_enabled_cache):
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


def print_report(results: list[dict], origin: str, dest: str, network: str, mode: str, tao_price: float):
    succeeded = [r for r in results if r["status"] == "success"]
    failed = [r for r in results if r["status"] != "success"]

    total_tao = sum(r["tao_value"] for r in succeeded)
    total_usd = total_tao * tao_price

    lines = [
        "",
        "=" * 72,
        "  STAKE TRANSFER REPORT",
        "=" * 72,
        f"  Date            : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"  Network         : {network}",
        f"  Mode            : {mode}",
        f"  Origin          : {origin}",
        f"  Destination     : {dest}",
        f"  TAO price       : ${tao_price:,.2f}",
        "-" * 72,
        f"  {'Subnet':>6}  {'Hotkey':>48}  {'Alpha':>14}  {'TAO':>14}  {'USD':>10}  {'Status'}",
        "-" * 72,
    ]

    for r in results:
        usd = r["tao_value"] * tao_price
        tag = "OK" if r["status"] == "success" else "FAIL"
        lines.append(
            f"  {r['netuid']:>6}  {r['hotkey']:>48}  {r['alpha_tao']:>14.6f}  {r['tao_value']:>14.6f}  ${usd:>9,.2f}  {tag}"
        )

    lines.extend([
        "-" * 72,
        f"  Transfers       : {len(results)} total, {len(succeeded)} success, {len(failed)} failed",
        f"  Total TAO moved : {total_tao:.6f}",
        f"  Total USD value : ${total_usd:,.2f}",
        "=" * 72,
    ])

    logger.info("\n".join(lines))


def save_accounting_csv(
        results: list[dict],
        origin: str,
        dest: str,
        network: str,
        mode: str,
        tao_price: float,
        extrinsic_id: str | None = None,
) -> str:
    ts = datetime.now(timezone.utc)
    filename = OUTPUT_DIR / f"stake-transfer-{ts.strftime('%Y%m%d-%H%M%S')}.csv"

    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "date", "timestamp_utc", "network", "mode",
            "origin_coldkey", "dest_coldkey",
            "netuid", "hotkey",
            "alpha_amount", "tao_value", "usd_value", "tao_price_usd",
            "status", "extrinsic_id",
        ])
        for r in results:
            writer.writerow([
                ts.strftime("%Y-%m-%d"),
                ts.strftime("%Y-%m-%d %H:%M:%S"),
                network,
                mode,
                origin,
                dest,
                r["netuid"],
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


def main(wallet_name: str, network: str, dest_coldkey: str, standalone: bool):
    subtensor = Subtensor(network=network, log_verbose=False)
    wallet = Wallet(name=wallet_name)

    if wallet.coldkey_file.is_encrypted():
        wallet.unlock_coldkey()

    origin_coldkey = wallet.coldkeypub.ss58_address

    if origin_coldkey == dest_coldkey:
        logger.error("Origin and destination coldkeys are the same — aborting")
        return

    logger.info(f"Origin coldkey      : {origin_coldkey}")
    logger.info(f"Destination coldkey : {dest_coldkey}")
    logger.info(f"Mode                : {'standalone (one extrinsic per subnet)' if standalone else 'batch'}")

    stakes = subtensor.get_stake_info_for_coldkey(origin_coldkey)
    if not stakes:
        logger.error("No stakes found on origin coldkey")
        return

    dynamic_infos = subtensor.all_subnets()
    dynamic_info_by_netuid = {d.netuid: d for d in dynamic_infos}

    tao_price = get_live_price() or 0.0
    if tao_price == 0.0:
        logger.warning("Could not fetch TAO price — USD values will show as $0")

    transfer_enabled_cache: dict[int, bool] = {}

    eligible = collect_eligible_stakes(stakes, dynamic_info_by_netuid, subtensor, transfer_enabled_cache)

    if not eligible:
        logger.error("No eligible stake transfers found")
        return

    included_rows = [
        {
            "netuid": e["netuid"],
            "hotkey": e["stake"].hotkey_ss58,
            "alpha_tao": float(e["stake"].stake.tao),
            "tao": float(e["tao_value"].tao),
        }
        for e in eligible
    ]

    portfolio_snapshot = build_snapshot(included_rows, tao_price)
    log_portfolio_summary(portfolio_snapshot, tao_price)

    # ==================================================================
    # STANDALONE MODE
    # ==================================================================
    if standalone:
        input("Press Enter to start standalone transfers...")

        results = []
        for e in eligible:
            stake = e["stake"]
            netuid = e["netuid"]
            tao_value = e["tao_value"]

            logger.info(
                f"Submitting transfer | netuid={netuid} | hotkey={stake.hotkey_ss58} | "
                f"alpha={stake.stake} | tao={tao_value}"
            )

            call = subtensor.substrate.compose_call(
                call_module="SubtensorModule",
                call_function="transfer_stake",
                call_params={
                    "destination_coldkey": dest_coldkey,
                    "hotkey": stake.hotkey_ss58,
                    "origin_netuid": netuid,
                    "destination_netuid": netuid,
                    "alpha_amount": int(stake.stake.rao),
                },
            )

            extrinsic = subtensor.substrate.create_signed_extrinsic(call=call, keypair=wallet.coldkey)
            response = subtensor.substrate.submit_extrinsic(extrinsic, wait_for_inclusion=True,
                                                            wait_for_finalization=True)
            response.process_events()

            status = "success" if response.is_success else "failed"
            if not response.is_success:
                logger.error(f"Transfer failed | netuid={netuid} | error={response.error_message}")

            results.append({
                "netuid": netuid,
                "hotkey": stake.hotkey_ss58,
                "alpha_tao": float(stake.stake.tao),
                "tao_value": float(tao_value.tao),
                "status": status,
            })

            time.sleep(0.2)

        print_report(results, origin_coldkey, dest_coldkey, network, "standalone", tao_price)
        save_accounting_csv(results, origin_coldkey, dest_coldkey, network, "standalone", tao_price)
        return

    # ==================================================================
    # BATCH MODE (force_batch: continues even if individual calls fail)
    # ==================================================================
    calls = []
    for idx, e in enumerate(eligible):
        stake = e["stake"]
        netuid = e["netuid"]
        tao_value = e["tao_value"]

        logger.info(
            f"[{idx:03}] queued | netuid={netuid} | hotkey={stake.hotkey_ss58} | "
            f"alpha={stake.stake} | tao={tao_value}"
        )

        calls.append(
            subtensor.substrate.compose_call(
                call_module="SubtensorModule",
                call_function="transfer_stake",
                call_params={
                    "destination_coldkey": dest_coldkey,
                    "hotkey": stake.hotkey_ss58,
                    "origin_netuid": netuid,
                    "destination_netuid": netuid,
                    "alpha_amount": int(stake.stake.rao),
                },
            )
        )

    batch_call = subtensor.substrate.compose_call("Utility", "force_batch", {"calls": calls})
    extrinsic = subtensor.substrate.create_signed_extrinsic(batch_call, wallet.coldkey)

    input("Press Enter to submit batch transfer...")

    response = subtensor.substrate.submit_extrinsic(extrinsic, wait_for_inclusion=True, wait_for_finalization=True)
    response.process_events()

    batch_status = "success" if response.is_success else "failed"
    if response.is_success:
        logger.info(f"Batch submitted successfully | {len(calls)} transfers")
    else:
        logger.error(f"Batch failed: {response.error_message}")

    extrinsic_id = response.get_extrinsic_identifier()

    results = [
        {
            "netuid": e["netuid"],
            "hotkey": e["stake"].hotkey_ss58,
            "alpha_tao": float(e["stake"].stake.tao),
            "tao_value": float(e["tao_value"].tao),
            "status": batch_status,
        }
        for e in eligible
    ]

    print_report(results, origin_coldkey, dest_coldkey, network, "batch", tao_price)
    save_accounting_csv(results, origin_coldkey, dest_coldkey, network, "batch", tao_price, extrinsic_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Transfer stake (batch or standalone)")
    parser.add_argument("--wallet", required=True)
    parser.add_argument("--network", required=True, choices=["finney", "test"], default="finney")
    parser.add_argument("--dest-coldkey", required=True)
    parser.add_argument("--standalone", action="store_true")

    args = parser.parse_args()
    main(args.wallet, args.network, args.dest_coldkey, args.standalone)
