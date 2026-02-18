# Transfer entire TAO balance from one wallet to another
# python exec-balance-transfer-all.py --wallet-from <SENDER> --wallet-to <RECEIVER> --network finney
# python exec-balance-transfer-all.py --wallet-from <SENDER> --ss58-to <SS58_ADDRESS> --network finney

import argparse
import asyncio
import sys

import bittensor
from bittensor import Balance, Wallet
from loguru import logger

logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")


async def main():
    parser = argparse.ArgumentParser(description="Transfer entire TAO balance between wallets")
    parser.add_argument("--wallet-from", required=True, dest="wallet_from", help="Sender wallet name")
    parser.add_argument("--wallet-to", dest="wallet_to", help="Receiver wallet name")
    parser.add_argument("--ss58-to", dest="ss58_to", help="Receiver SS58 address (alternative to --wallet-to)")
    parser.add_argument("--network", default="finney", choices=["finney", "test"], help="Bittensor network")
    args = parser.parse_args()

    if not args.wallet_to and not args.ss58_to:
        parser.error("You must specify either --wallet-to or --ss58-to")

    if args.wallet_to and args.ss58_to:
        parser.error("Specify only one of --wallet-to or --ss58-to, not both")

    wallet_from = Wallet(name=args.wallet_from)

    if args.wallet_to:
        wallet_to = Wallet(name=args.wallet_to)
        dest_ss58 = wallet_to.coldkeypub.ss58_address
    else:
        dest_ss58 = args.ss58_to

    origin_ss58 = wallet_from.coldkeypub.ss58_address

    if origin_ss58 == dest_ss58:
        logger.error("Origin and destination are the same wallet — aborting")
        return

    subtensor = await bittensor.get_async_subtensor(network=args.network)

    balance_from: Balance = await subtensor.get_balance(address=origin_ss58)
    balance_to: Balance = await subtensor.get_balance(address=dest_ss58)

    logger.info(f"Network     : {args.network}")
    logger.info(f"From        : {wallet_from.name} ({origin_ss58})")
    logger.info(f"To          : {args.wallet_to or dest_ss58}")
    logger.info(f"Balance from: {balance_from}")
    logger.info(f"Balance to  : {balance_to}")

    input("Press Enter to transfer ALL balance (this empties the sender wallet)...")

    result = await subtensor.transfer(
        wallet=wallet_from,
        destination_ss58=dest_ss58,
        amount=None,
        transfer_all=True,
        keep_alive=False,
    )

    if not result:
        logger.error("Transfer failed")
        return

    logger.success("Transfer submitted successfully")

    new_balance_from: Balance = await subtensor.get_balance(address=origin_ss58)
    new_balance_to: Balance = await subtensor.get_balance(address=dest_ss58)

    logger.info(f"Final balance from: {new_balance_from}")
    logger.info(f"Final balance to  : {new_balance_to}")
    logger.info(f"Transferred       : {balance_from - new_balance_from}")


if __name__ == "__main__":
    asyncio.run(main())
