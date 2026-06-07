"""
Snapshot wallet balances at a specific date across Ethereum, Arbitrum, and Base.

If ETHERSCAN_API_KEY is set, discovers ALL ERC-20 tokens the wallet has held
(via Etherscan v2 tokentx) and prices them with DefiLlama historical prices.
Otherwise falls back to a curated list (USDC/USDT + Aave/Compound/Fluid).

Requirements:
    pip install web3 requests matplotlib

Usage:
    python balance-snapshot.py                                    # current balances
    python balance-snapshot.py --date 2025-12-31
    python balance-snapshot.py --from 2025-02-16 --to 2026-02-16  # weekly over 1 year
    python balance-snapshot.py --from 2025-02-16 --to 2026-02-16 --force

Configure WALLETS below.
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ------------------------------------------------------------------
# Output directory
# ------------------------------------------------------------------
OUTPUT_DIR = Path(__file__).resolve().parent / "balance-snapshot"
OUTPUT_DIR.mkdir(exist_ok=True)

try:
    from web3 import Web3
except ImportError:
    sys.exit("Install web3: pip install web3")

import requests

# ── CONFIGURATION ────────────────────────────────────────────────────────────

_wallets_env = os.getenv("SNAPSHOT_WALLETS", "")
WALLETS = [w.strip() for w in _wallets_env.split(",") if w.strip()]
if not WALLETS:
    sys.exit("Set SNAPSHOT_WALLETS in .env (comma-separated EVM addresses)")

DEFAULT_SNAPSHOT_DATE = "now"

# ── CHAIN & TOKEN DEFINITIONS ───────────────────────────────────────────────

def _rpcs(env_var: str, *defaults: str) -> list[str]:
    """Resolve RPC URL list from env (comma-separated) with built-in fallbacks."""
    override = os.getenv(env_var, "").strip()
    if override:
        return [u.strip() for u in override.split(",") if u.strip()]
    return list(defaults)


CHAINS = {
    "ethereum": {
        "rpcs": _rpcs(
            "ETHEREUM_RPC_URL",
            "https://eth-mainnet.public.blastapi.io",
            "https://ethereum-rpc.publicnode.com",
            "https://eth.drpc.org",
        ),
        "chain_id": 1,
        "tokens": {
            "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        },
        "aave_v3_pool": "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",
        "compound_v3_cusdc": "0xc3d688B66703497DAA19211EEdff47f25384cdc3",
        "compound_v3_cusdt": None,
        "fluid_fusdc": "0x9Fb7b4477576Fe5B32be4C1843aFB1e55F251B33",
        "fluid_fusdt": "0x5C20B550819128074FD538Edf79791733ccEdd18",
    },
    "arbitrum": {
        "rpcs": _rpcs(
            "ARBITRUM_RPC_URL",
            "https://arbitrum-one.public.blastapi.io",
            "https://arbitrum-one-rpc.publicnode.com",
            "https://arbitrum.drpc.org",
        ),
        "chain_id": 42161,
        "tokens": {
            "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            "USDT": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
        },
        "aave_v3_pool": "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
        "compound_v3_cusdc": "0xA5EDBDD9646f8dFF606d7448e414884C7d905dCA",
        "compound_v3_cusdt": None,
        "fluid_fusdc": "0x1A996cb54bb95462040408C06122D45D6Cdb6096",
        "fluid_fusdt": "0x4A03F37e7d3fC243e3f99341d36f4b829BEe5E03",
    },
    "base": {
        "rpcs": _rpcs(
            "BASE_RPC_URL",
            "https://base-mainnet.public.blastapi.io",
            "https://base.llamarpc.com",
            "https://base-rpc.publicnode.com",
            "https://base.drpc.org",
        ),
        "chain_id": 8453,
        "tokens": {
            "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "USDT": None,  # no native USDT on Base
        },
        "aave_v3_pool": "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",
        "compound_v3_cusdc": "0xb125E6687d4313864e53df431d5425969c15Eb2F",
        "compound_v3_cusdt": None,
        "fluid_fusdc": "0xf42f5795D9ac7e9D757dB633D693cD548Cfd9169",
        "fluid_fusdt": None,
    },
}

# Minimal ABIs
ERC20_ABI = json.loads('[{"constant":true,"inputs":[{"name":"","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]')

AAVE_POOL_ABI = json.loads('[{"inputs":[{"name":"user","type":"address"}],"name":"getUserAccountData","outputs":[{"name":"totalCollateralBase","type":"uint256"},{"name":"totalDebtBase","type":"uint256"},{"name":"availableBorrowsBase","type":"uint256"},{"name":"currentLiquidationThreshold","type":"uint256"},{"name":"ltv","type":"uint256"},{"name":"healthFactor","type":"uint256"}],"stateMutability":"view","type":"function"}]')

# Compound v3 Comet uses same balanceOf as ERC20 for supply balance
COMET_ABI = json.loads('[{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"stateMutability":"view","type":"function"},{"inputs":[{"name":"account","type":"address"}],"name":"borrowBalanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]')

# Fluid fToken (ERC-4626 vault)
FLUID_ABI = json.loads('[{"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"name":"shares","type":"uint256"}],"name":"convertToAssets","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"stateMutability":"view","type":"function"}]')

# ── HELPERS ──────────────────────────────────────────────────────────────────

DEFILLAMA_CHAIN_NAMES = {
    "ethereum": "ethereum",
    "arbitrum": "arbitrum",
    "base": "base",
}

ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "").strip()
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"


def discover_tokens_etherscan(chain_id: int, wallet: str, end_block: int) -> list[dict]:
    """List unique ERC-20 tokens the wallet has touched up to end_block.

    Returns list of {address, symbol, decimals}. Uses Etherscan v2 tokentx.
    """
    if not ETHERSCAN_API_KEY:
        return []
    seen = {}
    page = 1
    offset = 10000
    while True:
        params = {
            "chainid": chain_id,
            "module": "account",
            "action": "tokentx",
            "address": wallet,
            "startblock": 0,
            "endblock": end_block,
            "page": page,
            "offset": offset,
            "sort": "asc",
            "apikey": ETHERSCAN_API_KEY,
        }
        r = requests.get(ETHERSCAN_V2_URL, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        msg = data.get("message", "")
        result = data.get("result", [])
        if data.get("status") != "1":
            if isinstance(result, list) and not result:
                break
            if "no transactions" in msg.lower():
                break
            raise RuntimeError(f"Etherscan error (chain {chain_id}): {msg} — {result}")
        for tx in result:
            addr = (tx.get("contractAddress") or "").lower()
            if addr and addr not in seen:
                try:
                    decimals = int(tx.get("tokenDecimal") or 18)
                except (ValueError, TypeError):
                    decimals = 18
                seen[addr] = {
                    "address": addr,
                    "symbol": tx.get("tokenSymbol", "?") or "?",
                    "decimals": decimals,
                }
        if len(result) < offset:
            break
        page += 1
        time.sleep(0.25)
    return list(seen.values())


def get_historical_prices_llama(chain_name: str, addresses: list[str], ts: int) -> dict:
    """Batch fetch historical USD prices from DefiLlama. Returns {addr_lower: price}."""
    if not addresses:
        return {}
    prices = {}
    chunk_size = 60
    for i in range(0, len(addresses), chunk_size):
        chunk = addresses[i : i + chunk_size]
        coins = ",".join(f"{chain_name}:{a}" for a in chunk)
        url = f"https://coins.llama.fi/prices/historical/{ts}/{coins}"
        try:
            r = requests.get(url, params={"searchWidth": "4h"}, timeout=30)
            if r.status_code != 200:
                continue
            data = r.json().get("coins", {})
            for key, info in data.items():
                addr = key.split(":", 1)[1].lower()
                prices[addr] = float(info.get("price", 0) or 0)
        except Exception:
            continue
    return prices


def get_native_eth_price(ts: int) -> float:
    """Native ETH USD price at timestamp (used for ETH on all 3 chains)."""
    url = f"https://coins.llama.fi/prices/historical/{ts}/coingecko:ethereum"
    try:
        r = requests.get(url, params={"searchWidth": "4h"}, timeout=30)
        if r.status_code != 200:
            return 0.0
        data = r.json().get("coins", {}).get("coingecko:ethereum", {})
        return float(data.get("price", 0) or 0)
    except Exception:
        return 0.0


RPC_MAX_RETRIES = 5
RPC_BACKOFF_BASE = 0.6


def _retry(fn, *, retries: int = RPC_MAX_RETRIES):
    """Call fn() with exponential backoff on transient RPC errors."""
    last_exc = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            msg = str(e).lower()
            transient = (
                "wrong json-rpc response" in msg
                or "neither result nor error" in msg
                or "timeout" in msg
                or "rate limit" in msg
                or "too many requests" in msg
                or "503" in msg
                or "502" in msg
                or "504" in msg
            )
            if not transient or attempt == retries - 1:
                raise
            time.sleep(RPC_BACKOFF_BASE * (2 ** attempt))
    raise last_exc


def get_block_by_timestamp(chain_name: str, timestamp: int) -> int:
    """Use DefiLlama API to find block number closest to timestamp."""
    llama_chain = DEFILLAMA_CHAIN_NAMES[chain_name]
    url = f"https://coins.llama.fi/block/{llama_chain}/{timestamp}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    return int(data["height"])


def get_eth_balance(w3: Web3, wallet: str, block: int) -> float:
    bal = _retry(lambda: w3.eth.get_balance(Web3.to_checksum_address(wallet), block_identifier=block))
    return float(Web3.from_wei(bal, "ether"))


def get_erc20_balance(w3: Web3, token_addr: str, wallet: str, block: int) -> float:
    contract = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
    decimals = _retry(lambda: contract.functions.decimals().call(block_identifier=block))
    raw = _retry(lambda: contract.functions.balanceOf(Web3.to_checksum_address(wallet)).call(block_identifier=block))
    return raw / (10 ** decimals)


def get_aave_position(w3: Web3, pool_addr: str, wallet: str, block: int) -> dict:
    """Returns Aave v3 aggregate position in USD (base currency = USD with 8 decimals)."""
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(pool_addr), abi=AAVE_POOL_ABI
    )
    try:
        data = _retry(lambda: contract.functions.getUserAccountData(
            Web3.to_checksum_address(wallet)
        ).call(block_identifier=block))
        return {
            "collateral_usd": data[0] / 1e8,
            "debt_usd": data[1] / 1e8,
            "health_factor": data[5] / 1e18 if data[5] < 2**255 else float("inf"),
        }
    except Exception:
        return {"collateral_usd": 0, "debt_usd": 0, "health_factor": 0}


def get_compound_v3_position(w3: Web3, comet_addr: str, wallet: str, block: int) -> dict:
    """Returns Compound v3 supply & borrow balance."""
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(comet_addr), abi=COMET_ABI
    )
    try:
        decimals = _retry(lambda: contract.functions.decimals().call(block_identifier=block))
        supply = _retry(lambda: contract.functions.balanceOf(
            Web3.to_checksum_address(wallet)
        ).call(block_identifier=block)) / (10 ** decimals)
        borrow = _retry(lambda: contract.functions.borrowBalanceOf(
            Web3.to_checksum_address(wallet)
        ).call(block_identifier=block)) / (10 ** decimals)
        return {"supply": supply, "borrow": borrow}
    except Exception:
        return {"supply": 0, "borrow": 0}


def get_fluid_position(w3: Web3, vault_addr: str, wallet: str, block: int) -> float:
    """Returns Fluid fToken position converted to underlying asset amount."""
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(vault_addr), abi=FLUID_ABI
    )
    try:
        decimals = _retry(lambda: contract.functions.decimals().call(block_identifier=block))
        shares = _retry(lambda: contract.functions.balanceOf(
            Web3.to_checksum_address(wallet)
        ).call(block_identifier=block))
        if shares == 0:
            return 0.0
        assets = _retry(lambda: contract.functions.convertToAssets(shares).call(block_identifier=block))
        return assets / (10 ** decimals)
    except Exception:
        return 0.0


# ── DATA COLLECTION ─────────────────────────────────────────────────────────

def collect_snapshot(ts: int) -> list[tuple]:
    """Collect balances for all wallets/chains at a given unix timestamp.

    Returns list of (wallet, chain, asset, contract, protocol, balance, value_usd) tuples.
    If ETHERSCAN_API_KEY is set, discovers all ERC-20 tokens; otherwise uses curated list.
    """
    rows = []
    w3_cache = {}
    discovery_mode = bool(ETHERSCAN_API_KEY)
    eth_price = get_native_eth_price(ts)

    for chain_name, cfg in CHAINS.items():
        if chain_name not in w3_cache:
            w3 = None
            for url in cfg["rpcs"]:
                candidate = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
                try:
                    candidate.eth.block_number
                    w3 = candidate
                    break
                except Exception:
                    continue
            if w3 is None:
                print(f"  [!] {chain_name}: no working RPC, skipping")
                continue
            w3_cache[chain_name] = w3
        w3 = w3_cache[chain_name]

        block = get_block_by_timestamp(chain_name, ts)
        chain = chain_name.capitalize()
        llama_chain = DEFILLAMA_CHAIN_NAMES[chain_name]

        for wallet in WALLETS:
            # Native ETH
            eth_bal = get_eth_balance(w3, wallet, block)
            if eth_bal > 0.0001:
                rows.append((wallet, chain, "ETH", "-", "Wallet", eth_bal, eth_bal * eth_price))

            chain_discovery_ok = False
            tokens: list[dict] = []
            if discovery_mode:
                try:
                    tokens = discover_tokens_etherscan(cfg["chain_id"], wallet, block)
                    chain_discovery_ok = True
                except Exception as e:
                    print(f"  [!] {chain_name} discovery failed ({e}) — using curated list")

            if chain_discovery_ok:
                addrs = [t["address"] for t in tokens]
                prices = get_historical_prices_llama(llama_chain, addrs, ts)
                skipped_unpriced = 0
                for tok in tokens:
                    addr = tok["address"]
                    try:
                        raw = _retry(lambda a=addr: w3.eth.contract(
                            address=Web3.to_checksum_address(a), abi=ERC20_ABI
                        ).functions.balanceOf(Web3.to_checksum_address(wallet)).call(block_identifier=block))
                    except Exception:
                        continue
                    if raw <= 0:
                        continue
                    bal = raw / (10 ** tok["decimals"])
                    price = prices.get(addr, 0.0)
                    if price == 0:
                        skipped_unpriced += 1
                        continue
                    rows.append((wallet, chain, tok["symbol"], addr, "Wallet", bal, bal * price))
                if skipped_unpriced:
                    print(f"  [i] {chain_name}: {skipped_unpriced} unpriced tokens filtered (likely spam)")
            else:
                # Curated fallback
                token_addrs = [a for a in cfg["tokens"].values() if a]
                prices = get_historical_prices_llama(llama_chain, token_addrs, ts)
                for symbol, addr in cfg["tokens"].items():
                    if addr is None:
                        continue
                    bal = get_erc20_balance(w3, addr, wallet, block)
                    if bal > 0.01:
                        price = prices.get(addr.lower(), 0.0)
                        rows.append((wallet, chain, symbol, addr, "Wallet", bal, bal * price))

                # Aave v3 (USD-denominated aggregate)
                if cfg.get("aave_v3_pool"):
                    pool = cfg["aave_v3_pool"]
                    pos = get_aave_position(w3, pool, wallet, block)
                    if pos["collateral_usd"] > 0:
                        rows.append((wallet, chain, "USD (collat.)", pool, "Aave v3", pos["collateral_usd"], pos["collateral_usd"]))
                    if pos["debt_usd"] > 0:
                        rows.append((wallet, chain, "USD (debt)", pool, "Aave v3", -pos["debt_usd"], -pos["debt_usd"]))

                # Compound v3
                for symbol, key in [("USDC", "compound_v3_cusdc"), ("USDT", "compound_v3_cusdt")]:
                    comet = cfg.get(key)
                    if not comet:
                        continue
                    pos = get_compound_v3_position(w3, comet, wallet, block)
                    underlying = cfg["tokens"].get(symbol)
                    px = prices.get(underlying.lower(), 0.0) if underlying else 0.0
                    if pos["supply"] > 0:
                        rows.append((wallet, chain, symbol, comet, "Compound v3", pos["supply"], pos["supply"] * px))
                    if pos["borrow"] > 0:
                        rows.append((wallet, chain, symbol + " (debt)", comet, "Compound v3", -pos["borrow"], -pos["borrow"] * px))

                # Fluid lending vaults
                for symbol, key in [("USDC", "fluid_fusdc"), ("USDT", "fluid_fusdt")]:
                    vault = cfg.get(key)
                    if not vault:
                        continue
                    bal = get_fluid_position(w3, vault, wallet, block)
                    underlying = cfg["tokens"].get(symbol)
                    px = prices.get(underlying.lower(), 0.0) if underlying else 0.0
                    if bal > 0:
                        rows.append((wallet, chain, symbol, vault, "Fluid", bal, bal * px))

    return rows


# ── DISPLAY ──────────────────────────────────────────────────────────────────

def print_table(rows):
    """Pretty-print rows as an ASCII table."""
    # Sort by USD value desc for readability
    rows = sorted(rows, key=lambda r: r[6], reverse=True)
    hdr = ("Wallet", "Chain", "Asset", "Contract", "Protocol", "Balance", "Value (USD)")
    w_wallet = max(len(hdr[0]), max(len(r[0]) for r in rows))
    w_chain = max(len(hdr[1]), max(len(r[1]) for r in rows))
    w_asset = max(len(hdr[2]), max(len(r[2]) for r in rows))
    w_cont = max(len(hdr[3]), max(len(r[3]) for r in rows))
    w_proto = max(len(hdr[4]), max(len(r[4]) for r in rows))
    w_bal = 16
    w_val = 14

    def sep():
        print(f"  +-{'-'*w_wallet}-+-{'-'*w_chain}-+-{'-'*w_asset}-+-{'-'*w_cont}-+-{'-'*w_proto}-+-{'-'*w_bal}-+-{'-'*w_val}-+")

    def row(wallet, chain, asset, cont, proto, bal_str, val_str):
        print(f"  | {wallet:<{w_wallet}} | {chain:<{w_chain}} | {asset:<{w_asset}} | {cont:<{w_cont}} | {proto:<{w_proto}} | {bal_str:>{w_bal}} | {val_str:>{w_val}} |")

    sep()
    row(*hdr)
    sep()
    total_usd = 0.0
    for wallet, chain, asset, cont, proto, bal, val in rows:
        bal_str = f"{bal:>,.4f}" if not isinstance(bal, str) else bal
        val_str = f"{val:>,.2f}" if val else "-"
        row(wallet, chain, asset, cont, proto, bal_str, val_str)
        total_usd += val
    sep()
    print(f"\n  TOTAL: ${total_usd:,.2f}\n")


def write_csv(path, header, csv_rows):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(csv_rows)


# ── CHART ────────────────────────────────────────────────────────────────────

def generate_chart(csv_path, chart_path):
    """Read the range CSV and generate a stacked area chart by protocol."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    # Parse CSV
    dates_set = []
    data = {}  # protocol -> {date -> total_value_usd}
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = row["Date"]
            proto = row["Protocol"]
            val = float(row.get("Value (USD)") or row.get("Balance") or 0)
            if d not in dates_set:
                dates_set.append(d)
            data.setdefault(proto, {})[d] = data.get(proto, {}).get(d, 0) + val

    dates = [datetime.strptime(d, "%Y-%m-%d") for d in dates_set]
    protocols = sorted(data.keys())

    fig, ax = plt.subplots(figsize=(14, 6))

    # Stacked area
    bottoms = [0.0] * len(dates)
    for proto in protocols:
        values = [data[proto].get(d, 0) for d in dates_set]
        ax.fill_between(dates, bottoms, [b + v for b, v in zip(bottoms, values)],
                        label=proto, alpha=0.7)
        bottoms = [b + v for b, v in zip(bottoms, values)]

    # Total line
    ax.plot(dates, bottoms, color="black", linewidth=1.5, linestyle="--", label="Total")

    ax.set_title("Portfolio Value Over Time")
    ax.set_ylabel("Value (USD)")
    ax.legend(loc="upper left")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(chart_path, dpi=150)
    plt.close(fig)


# ── MAIN ─────────────────────────────────────────────────────────────────────

def parse_date(s: str) -> datetime:
    if s.lower() == "now":
        return datetime.now(timezone.utc)
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )
    except ValueError:
        sys.exit(f"Invalid date format: {s}. Use YYYY-MM-DD or 'now'.")


def parse_args():
    parser = argparse.ArgumentParser(description="Snapshot wallet balances at a specific date.")
    parser.add_argument(
        "--date",
        default=DEFAULT_SNAPSHOT_DATE,
        help="Single snapshot date (YYYY-MM-DD or 'now', default: now).",
    )
    parser.add_argument(
        "--from", dest="from_date", default=None,
        help="Range mode: start date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--to", dest="to_date", default=None,
        help="Range mode: end date (YYYY-MM-DD or 'now').",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite output file if it already exists (range mode: clears resume state).",
    )
    parser.add_argument(
        "--granularity",
        choices=["daily", "weekly"],
        default="weekly",
        help="Snapshot frequency in range mode (default: weekly).",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=5.0,
        help="Seconds to wait between snapshots in range mode (default: 5.0).",
    )
    return parser.parse_args()


def run_single(args):
    snapshot_dt = parse_date(args.date)
    display_date = snapshot_dt.strftime("%Y-%m-%d %H:%M:%S")
    ts = int(snapshot_dt.timestamp())

    print(f"\n  BALANCE SNAPSHOT — {display_date} UTC\n")

    rows = collect_snapshot(ts)
    if not rows:
        print("  No balances found.\n")
        return

    print_table(rows)

    # Dump to CSV
    file_date = snapshot_dt.strftime("%Y-%m-%d")
    outfile = OUTPUT_DIR / f"snapshot_{file_date}.csv"
    if outfile.exists() and not args.force:
        print(f"\n  [!] {outfile} already exists. Use --force to overwrite.\n")
        return

    hdr = ["Wallet", "Chain", "Asset", "Contract", "Protocol", "Balance", "Value (USD)"]
    csv_rows = [[w, c, a, co, p, f"{b:.6f}", f"{v:.2f}"] for w, c, a, co, p, b, v in rows]
    write_csv(outfile, hdr, csv_rows)
    print(f"\n  Saved to {outfile}\n")


def run_range(args):
    from_dt = parse_date(args.from_date)
    to_dt = parse_date(args.to_date)

    if from_dt >= to_dt:
        sys.exit("--from must be before --to.")

    step = timedelta(days=1 if args.granularity == "daily" else 7)
    from_str = from_dt.strftime("%Y-%m-%d")
    to_str = to_dt.strftime("%Y-%m-%d")
    outfile = OUTPUT_DIR / f"snapshot_{from_str}_to_{to_str}_{args.granularity}.csv"
    chart_file = OUTPUT_DIR / f"snapshot_{from_str}_to_{to_str}_{args.granularity}.png"

    # Build list of dates
    dates = []
    current = from_dt
    while current <= to_dt:
        dates.append(current)
        current += step

    hdr = ["Date", "Wallet", "Chain", "Asset", "Contract", "Protocol", "Balance", "Value (USD)"]

    # Resume support: read existing dates from outfile
    done_dates: set[str] = set()
    if outfile.exists():
        if args.force:
            outfile.unlink()
        else:
            with open(outfile, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    done_dates.add(row["Date"])
            if done_dates:
                print(f"\n  Resuming: {len(done_dates)} dates already in {outfile.name}")

    is_new = not outfile.exists()
    f_out = open(outfile, "a", newline="")
    writer = csv.writer(f_out)
    if is_new:
        writer.writerow(hdr)
        f_out.flush()

    total = len(dates)
    print(f"\n  RANGE SNAPSHOT — {from_str} to {to_str} ({total} {args.granularity} snapshots, sleep={args.sleep}s)\n")

    now = datetime.now(timezone.utc)
    try:
        for i, dt in enumerate(dates, 1):
            date_label = dt.strftime("%Y-%m-%d")
            prefix = f"  [{i:>{len(str(total))}}/{total}] {date_label}"
            if date_label in done_dates:
                print(f"{prefix} ... skipped (already done)")
                continue

            effective_dt = min(dt, now)
            ts = int(effective_dt.timestamp())
            print(f"{prefix} ...", end=" ", flush=True)

            try:
                rows = collect_snapshot(ts)
                count = len(rows)
                total_usd = sum(r[6] for r in rows)
                for w, c, a, co, p, b, v in rows:
                    writer.writerow([date_label, w, c, a, co, p, f"{b:.6f}", f"{v:.2f}"])
                f_out.flush()
                print(f"{count} positions, ${total_usd:,.2f}")
            except Exception as e:
                print(f"ERROR: {e}")

            if i < total and args.sleep > 0:
                time.sleep(args.sleep)
    finally:
        f_out.close()

    if outfile.stat().st_size == 0:
        print("\n  No balances found across the range.\n")
        return

    print(f"\n  Saved to {outfile}")
    generate_chart(outfile, chart_file)
    print(f"  Chart saved to {chart_file}\n")


def main():
    args = parse_args()

    if args.from_date or args.to_date:
        if not args.from_date or not args.to_date:
            sys.exit("Both --from and --to are required for range mode.")
        run_range(args)
    else:
        run_single(args)


if __name__ == "__main__":
    main()
