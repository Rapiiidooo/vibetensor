import asyncio

import bittensor
from bittensor import Balance, Wallet, AsyncSubtensor
from loguru import logger

from base import ArgParseManager


async def main():
    parser = ArgParseManager()
    parser.add_argument("--wallet-from", type=str, dest="wallet_from", help="Sender wallet name")
    parser.add_argument("--wallet-to", type=str, dest="wallet_to", help="Receiver wallet name")
    parser.add_argument("--ss58-to", type=str, dest="ss58_to", help="Receiver wallet ss58 address")
    args = parser.parse_args()

    subtensor = await bittensor.get_async_subtensor(network=args.network)

    wallet_from: Wallet = Wallet(name=args.wallet_from)

    if args.wallet_to is not None:
        wallet_to: Wallet = Wallet(name=args.wallet_to)
    elif args.ss58_to is not None:
        wallet_to: Wallet = Wallet(name=args.ss58_to)
        wallet_to.regenerate_coldkeypub(args.ss58_to)
    else:
        raise Exception("You must specify either --wallet-to or --ss58-to")

    current_balance_from = await subtensor.get_balance(address=wallet_from.coldkeypub.ss58_address)
    current_balance_to = await subtensor.get_balance(address=wallet_to.coldkeypub.ss58_address)

    logger.info(f"Current balance {wallet_from.name}: {current_balance_from}")
    logger.info(f"Current balance {wallet_to.name}: {current_balance_to}")

    result = await subtensor.transfer(wallet=wallet_from, destination_ss58=wallet_to.coldkeypub.ss58_address, amount=None, transfer_all=True, keep_alive=False)

    if result:
        logger.success("Successfully sent.")

    new_balance_from: Balance = await subtensor.get_balance(address=wallet_from.coldkeypub.ss58_address)
    new_balance_to: Balance = await subtensor.get_balance(address=wallet_to.coldkeypub.ss58_address)

    logger.info(f"Final balance {wallet_from.name}: {new_balance_from}")
    logger.info(f"Final balance {wallet_to.name}: {new_balance_to}")


asyncio.run(main())
