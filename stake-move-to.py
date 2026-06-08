# Move all alpha stake from one hotkey to another (same coldkey)
#
# python stake-move-to.py --network test --wallet testnet-holding-00 --dest-hotkey <DEST_HOTKEY_SS58> [--dest-netuid <NETUID>] [--standalone]

import argparse
import sys
import time

from loguru import logger

from base import fetch_delegate_identities_sync, rao_to_tao
from bittensor import Subtensor
from bittensor_wallet import Wallet

logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")

MIN_TAO = rao_to_tao(2_000_000)
MIN_ALPHA = rao_to_tao(2_000_000)

SUBNETS_TO_SKIP = []
SUBNETS_TO_PROCEED = [11, 28, 62]


def collect_eligible_stakes(stakes, dynamic_info_by_netuid, dest_hotkey, dest_netuid):
    eligible = []
    for stake in stakes:
        netuid = stake.netuid
        hotkey = stake.hotkey_ss58
        dst_netuid = dest_netuid if dest_netuid is not None else netuid

        if hotkey == dest_hotkey and netuid == dst_netuid:
            continue
        if netuid in SUBNETS_TO_SKIP or netuid not in SUBNETS_TO_PROCEED:
            continue

        pool = dynamic_info_by_netuid.get(netuid)
        tao_value = pool.alpha_to_tao(stake.stake)

        if stake.stake.tao < MIN_ALPHA or tao_value.tao < MIN_TAO:
            continue

        eligible.append({"stake": stake, "netuid": netuid, "hotkey": hotkey, "dst_netuid": dst_netuid, "tao": tao_value})
    return eligible


def compose_move_call(substrate, e, dest_hotkey):
    return substrate.compose_call(
        call_module="SubtensorModule",
        call_function="move_stake",
        call_params={
            "origin_netuid": e["netuid"],
            "origin_hotkey": e["hotkey"],
            "destination_netuid": e["dst_netuid"],
            "destination_hotkey": dest_hotkey,
            "alpha_amount": e["stake"].stake,
        },
    )


def main(wallet_name: str, network: str, dest_hotkey: str, dest_netuid: int | None, standalone: bool):
    subtensor = Subtensor(network=network, log_verbose=False)
    wallet = Wallet(name=wallet_name)

    if wallet.coldkey_file.is_encrypted():
        wallet.unlock_coldkey()

    origin_coldkey = wallet.coldkeypub.ss58_address
    logger.info(f"Coldkey            : {origin_coldkey}")
    logger.info(f"Destination hotkey : {dest_hotkey}")
    logger.info(f"Destination netuid : {dest_netuid if dest_netuid is not None else 'same as origin'}")
    logger.info(f"Mode               : {'standalone' if standalone else 'batch'}")

    stakes = subtensor.get_stake_info_for_coldkey(origin_coldkey)
    if not stakes:
        logger.error("No stakes found for this coldkey")
        return

    dynamic_info_by_netuid = {d.netuid: d for d in subtensor.all_subnets()}
    eligible = collect_eligible_stakes(stakes, dynamic_info_by_netuid, dest_hotkey, dest_netuid)

    if not eligible:
        logger.error("No stake moves to submit")
        return

    for idx, e in enumerate(eligible):
        logger.info(
            f"[{idx:03}] queued | netuid {e['netuid']} -> {e['dst_netuid']} | "
            f"{e['hotkey']} -> {dest_hotkey} | alpha={e['stake'].stake} | tao={e['tao']}"
        )

    if standalone:
        for e in eligible:
            call = compose_move_call(subtensor.substrate, e, dest_hotkey)
            extrinsic = subtensor.substrate.create_signed_extrinsic(call=call, keypair=wallet.coldkey)
            response = subtensor.substrate.submit_extrinsic(extrinsic, wait_for_inclusion=True, wait_for_finalization=True)
            response.process_events()

            if response.is_success:
                logger.info(f"SUCCESS | netuid={e['netuid']}")
            else:
                logger.error(f"FAILED | netuid={e['netuid']} | {response.error_message}")

            time.sleep(0.2)
    else:
        calls = [compose_move_call(subtensor.substrate, e, dest_hotkey) for e in eligible]
        batch_call = subtensor.substrate.compose_call("Utility", "batch_all", {"calls": calls})

        fee = subtensor.substrate.get_payment_info(call=batch_call, keypair=wallet.coldkeypub)["partial_fee"]
        logger.info(f"Estimated fee: {rao_to_tao(fee)}t")

        input("Press Enter to submit batch...")

        extrinsic = subtensor.substrate.create_signed_extrinsic(call=batch_call, keypair=wallet.coldkey)
        response = subtensor.substrate.submit_extrinsic(extrinsic, wait_for_inclusion=True, wait_for_finalization=True)

        if response.is_success:
            logger.info(f"Batch SUCCESS | {len(calls)} moves | {response.get_extrinsic_identifier()}")
        else:
            logger.error(f"Batch FAILED | {response.error_message}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Move stake between hotkeys (same coldkey)")
    parser.add_argument("--wallet")
    parser.add_argument("--network", choices=["finney", "test"])
    parser.add_argument("--dest-hotkey")
    parser.add_argument("--dest-netuid", type=int, default=None)
    parser.add_argument("--standalone", action="store_true")
    args = parser.parse_args()

    wallet = args.wallet or input("Wallet name: ").strip()
    network = args.network or input("Network (finney/test) [finney]: ").strip() or "finney"

    dest_hotkey = args.dest_hotkey
    dest_netuid = args.dest_netuid

    if not dest_hotkey:
        raw = input("Destination hotkey (SS58, or press Enter to browse metagraph): ").strip()
        if raw:
            dest_hotkey = raw
        else:
            netuid = dest_netuid or int(input("Netuid to browse: ").strip())
            dest_netuid = netuid

            sub = Subtensor(network=network, log_verbose=False)
            metagraph = sub.metagraph(netuid=netuid)

            delegate_identities = fetch_delegate_identities_sync(sub)

            print(f"\nValidators on subnet {netuid} (vtrust > 0):")
            for uid, hotkey in enumerate(metagraph.hotkeys):
                vtrust = metagraph.Tv[uid]
                if vtrust <= 0:
                    continue
                stake = metagraph.S[uid]
                coldkey = metagraph.coldkeys[uid]
                identity = delegate_identities.get(coldkey)
                name = identity.name if identity else ""
                print(f"  [{uid:>4}] {hotkey}  {name:<20}  (stake: {stake:.4f}, vtrust: {vtrust:.4f})")

            choice = input("\nEnter UID or paste hotkey: ").strip()
            if choice.isdigit():
                dest_hotkey = metagraph.hotkeys[int(choice)]
            else:
                dest_hotkey = choice

            print(f"Selected: {dest_hotkey}")

    main(wallet, network, dest_hotkey, dest_netuid, args.standalone)
