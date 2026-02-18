import argparse
import datetime
import os
import struct
import threading
from decimal import Decimal

import bittensor
import requests
import xxhash
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

SUBTENSOR_ARCHIVE = os.getenv("SUBTENSOR_ARCHIVE", "wss://entrypoint-finney.opentensor.ai:443")
SUBTENSOR_TEST = "wss://test.finney.opentensor.ai:443"

RAO_PER_TAO = Decimal("1000000000")  # 1 TAO = 1e9 rao


class ArgParseManager(argparse.ArgumentParser):
    def __init__(self, description=None):
        super().__init__(description=description)
        self.add_argument("--amount", type=float, help="amount to send", default=0.0)
        self.add_argument("--coldkey", help="name of the coldkey", required=True)
        self.add_argument("--coldkeys", nargs="*", help="name of the coldkeys", default=None)
        self.add_argument("--hotkey", help="name of the hotkey", required=True)
        self.add_argument("--hotkeys", nargs="*", help="name of the hotkeys", default=None)
        self.add_argument("--netuid", default=0, type=int)
        self.add_argument("--netuids", nargs="*", default=[])
        self.add_argument("--network", type=str, default=SUBTENSOR_ARCHIVE)
        self.add_argument("--networks", nargs="*", default=[])


def short_hotkey(hotkey: str) -> str:
    if not hotkey:
        return ""
    return hotkey[:6] + ".." + hotkey[-6:]


def twox128_le(data: bytes) -> bytes:
    """Substrate Twox128: xxh64(seed=0) || xxh64(seed=1), little-endian"""
    h0 = xxhash.xxh64(seed=0)
    h0.update(data)
    h1 = xxhash.xxh64(seed=1)
    h1.update(data)
    return struct.pack("<Q", h0.intdigest()) + struct.pack("<Q", h1.intdigest())


def storage_key_prefix(pallet: str, item: str) -> bytes:
    return twox128_le(pallet.encode()) + twox128_le(item.encode())


def get_storage_key(pallet: str, item: str, subnet_hex: str | None = None) -> str:
    prefix = storage_key_prefix(pallet, item)
    if subnet_hex:
        return "0x" + prefix.hex() + subnet_hex.lower() + "00"
    return "0x" + prefix.hex()


def run_in_thread(func):
    def wrapper(*args, **kwargs):
        threaded = kwargs.pop("threaded", True)
        if not threaded:
            return func(*args, **kwargs)
        else:
            thread = threading.Thread(target=func, args=args, kwargs=kwargs)
            thread.start()
            return thread

    return wrapper


def get_tao_price(date: str = None, currency="usd") -> float | None:
    return get_coin_price(date=date, currency=currency)


def get_btc_price(date: str = None, currency="usd") -> float | None:
    return get_coin_price(coin="bitcoin", date=date, currency=currency)


def get_coin_price(coin="bittensor", date: str = None, currency="usd") -> float | None:
    """
    :param coin: which cryptocurrency to get price
    :param currency: usd (only tested with this one)
    :param date: format 'dd-mm-yyyy' (ex: '09-04-2024')
    :return: Price in USD
    """
    if not date:
        date = datetime.datetime.now().strftime("%d-%m-%Y")

    url = f"https://api.coingecko.com/api/v3/coins/{coin}/history?date={date}"

    logger.debug(f"url: {url}")

    response = requests.get(url)
    if response.status_code != 200:
        logger.error(f"Error: {response.text}")
        return None

    data = response.json()
    try:
        price = data["market_data"]["current_price"][currency]
        logger.debug(f"price: {price}")
        return price
    except KeyError:
        logger.error("Price not found.")
        return None


def get_live_price(coin="bittensor", currency="usd"):
    url = f"https://api.coingecko.com/api/v3/simple/price" f"?ids={coin}&vs_currencies={currency}"

    response = requests.get(url)
    if response.status_code != 200:
        logger.error("Error:", response.text)
        return None

    data = response.json()

    try:
        logger.debug(f"price: {data[coin][currency]}")
        return data[coin][currency]
    except KeyError:
        logger.warning("Price not found.")
        return None


def block_to_time_duration(blocks: int) -> str:
    seconds = blocks * bittensor.BLOCKTIME
    delta = datetime.timedelta(seconds=seconds)
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    time_str = f"{days:02}d:{hours:02}h:{minutes:02}m"
    return time_str


def safe_division(a, b, default_value=0):
    try:
        result = a / b
    except Exception:
        result = default_value
    return result


def safe_mul(a, b, default=None):
    if not a:
        return default or b
    if not b:
        return default or a
    return a * b


def rao_to_tao(rao):
    if rao is None:
        return None
    return (Decimal(rao) / RAO_PER_TAO).quantize(Decimal("0.000000001"))


def tao_to_rao(tao):
    if tao is None:
        return None
    return int((Decimal(tao) * RAO_PER_TAO).to_integral_value())
