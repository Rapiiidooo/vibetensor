"""
Snapshot wallet balances (ETH / USDC / USDT) at a specific date
across Ethereum, Arbitrum, and Base.

Also queries Aave v3, Compound v3, and Fluid lending vault (fToken) balances.

Requirements:
    pip install web3 requests matplotlib

Usage:
    python balance_snapshot.py                                    # current balances
    python balance_snapshot.py --date 2024-12-31
    python balance_snapshot.py --from 2025-02-16 --to 2026-02-16  # weekly over 1 year
    python balance_snapshot.py --from 2025-02-16 --to 2026-02-16 --force

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
OUTPUT_DIR = Path(__file__).resolve().parent / "balance_snapshot"
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

CHAINS = {
    "ethereum": {
        "rpc": "https://eth.drpc.org",
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
        "rpc": "https://arbitrum.drpc.org",
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
        "rpc": "https://base.drpc.org",
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

def get_block_by_timestamp(chain_name: str, timestamp: int) -> int:
    """Use DefiLlama API to find block number closest to timestamp."""
    llama_chain = DEFILLAMA_CHAIN_NAMES[chain_name]
    url = f"https://coins.llama.fi/block/{llama_chain}/{timestamp}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    return int(data["height"])


def get_eth_balance(w3: Web3, wallet: str, block: int) -> float:
    bal = w3.eth.get_balance(Web3.to_checksum_address(wallet), block_identifier=block)
    return float(Web3.from_wei(bal, "ether"))


def get_erc20_balance(w3: Web3, token_addr: str, wallet: str, block: int) -> float:
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI
    )
    decimals = contract.functions.decimals().call(block_identifier=block)
    raw = contract.functions.balanceOf(
        Web3.to_checksum_address(wallet)
    ).call(block_identifier=block)
    return raw / (10 ** decimals)


def get_aave_position(w3: Web3, pool_addr: str, wallet: str, block: int) -> dict:
    """Returns Aave v3 aggregate position in USD (base currency = USD with 8 decimals)."""
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(pool_addr), abi=AAVE_POOL_ABI
    )
    try:
        data = contract.functions.getUserAccountData(
            Web3.to_checksum_address(wallet)
        ).call(block_identifier=block)
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
        decimals = contract.functions.decimals().call(block_identifier=block)
        supply = contract.functions.balanceOf(
            Web3.to_checksum_address(wallet)
        ).call(block_identifier=block) / (10 ** decimals)
        borrow = contract.functions.borrowBalanceOf(
            Web3.to_checksum_address(wallet)
        ).call(block_identifier=block) / (10 ** decimals)
        return {"supply": supply, "borrow": borrow}
    except Exception:
        return {"supply": 0, "borrow": 0}


def get_fluid_position(w3: Web3, vault_addr: str, wallet: str, block: int) -> float:
    """Returns Fluid fToken position converted to underlying asset amount."""
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(vault_addr), abi=FLUID_ABI
    )
    try:
        decimals = contract.functions.decimals().call(block_identifier=block)
        shares = contract.functions.balanceOf(
            Web3.to_checksum_address(wallet)
        ).call(block_identifier=block)
        if shares == 0:
            return 0.0
        assets = contract.functions.convertToAssets(shares).call(block_identifier=block)
        return assets / (10 ** decimals)
    except Exception:
        return 0.0


# ── DATA COLLECTION ─────────────────────────────────────────────────────────

def collect_snapshot(ts: int) -> list[tuple]:
    """Collect balances for all wallets/chains at a given unix timestamp.

    Returns list of (wallet, chain, asset, contract, protocol, balance) tuples.
    """
    rows = []
    w3_cache = {}

    for chain_name, cfg in CHAINS.items():
        if chain_name not in w3_cache:
            w3 = Web3(Web3.HTTPProvider(cfg["rpc"], request_kwargs={"timeout": 30}))
            try:
                w3.eth.block_number
            except Exception:
                continue
            w3_cache[chain_name] = w3
        w3 = w3_cache[chain_name]

        block = get_block_by_timestamp(chain_name, ts)
        chain = chain_name.capitalize()

        for wallet in WALLETS:
            # Native ETH
            eth_bal = get_eth_balance(w3, wallet, block)
            if eth_bal > 0.0001:
                rows.append((wallet, chain, "ETH", "-", "Wallet", eth_bal))

            # ERC-20 tokens
            for symbol, addr in cfg["tokens"].items():
                if addr is None:
                    continue
                bal = get_erc20_balance(w3, addr, wallet, block)
                if bal > 0.01:
                    rows.append((wallet, chain, symbol, addr, "Wallet", bal))

            # Aave v3
            if cfg.get("aave_v3_pool"):
                pool = cfg["aave_v3_pool"]
                pos = get_aave_position(w3, pool, wallet, block)
                if pos["collateral_usd"] > 0:
                    rows.append((wallet, chain, "USD (collat.)", pool, "Aave v3", pos["collateral_usd"]))
                if pos["debt_usd"] > 0:
                    rows.append((wallet, chain, "USD (debt)", pool, "Aave v3", -pos["debt_usd"]))

            # Compound v3
            for symbol, key in [("USDC", "compound_v3_cusdc"), ("USDT", "compound_v3_cusdt")]:
                comet = cfg.get(key)
                if not comet:
                    continue
                pos = get_compound_v3_position(w3, comet, wallet, block)
                if pos["supply"] > 0:
                    rows.append((wallet, chain, symbol, comet, "Compound v3", pos["supply"]))
                if pos["borrow"] > 0:
                    rows.append((wallet, chain, symbol + " (debt)", comet, "Compound v3", -pos["borrow"]))

            # Fluid lending vaults
            for symbol, key in [("USDC", "fluid_fusdc"), ("USDT", "fluid_fusdt")]:
                vault = cfg.get(key)
                if not vault:
                    continue
                bal = get_fluid_position(w3, vault, wallet, block)
                if bal > 0:
                    rows.append((wallet, chain, symbol, vault, "Fluid", bal))

    return rows


# ── DISPLAY ──────────────────────────────────────────────────────────────────

def print_table(rows):
    """Pretty-print rows as an ASCII table."""
    hdr = ("Wallet", "Chain", "Asset", "Contract", "Protocol", "Balance")
    w_wallet = max(len(hdr[0]), max(len(r[0]) for r in rows))
    w_chain = max(len(hdr[1]), max(len(r[1]) for r in rows))
    w_asset = max(len(hdr[2]), max(len(r[2]) for r in rows))
    w_cont = max(len(hdr[3]), max(len(r[3]) for r in rows))
    w_proto = max(len(hdr[4]), max(len(r[4]) for r in rows))
    w_bal = 14

    def sep():
        print(f"  +-{'-'*w_wallet}-+-{'-'*w_chain}-+-{'-'*w_asset}-+-{'-'*w_cont}-+-{'-'*w_proto}-+-{'-'*w_bal}-+")

    def row(wallet, chain, asset, cont, proto, bal_str):
        print(f"  | {wallet:<{w_wallet}} | {chain:<{w_chain}} | {asset:<{w_asset}} | {cont:<{w_cont}} | {proto:<{w_proto}} | {bal_str:>{w_bal}} |")

    sep()
    row(*hdr)
    sep()
    for wallet, chain, asset, cont, proto, bal in rows:
        bal_str = f"{bal:>,.2f}" if not isinstance(bal, str) else bal
        row(wallet, chain, asset, cont, proto, bal_str)
    sep()


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
    data = {}  # protocol -> {date -> total_balance}
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = row["Date"]
            proto = row["Protocol"]
            bal = float(row["Balance"])
            if d not in dates_set:
                dates_set.append(d)
            data.setdefault(proto, {})[d] = data.get(proto, {}).get(d, 0) + bal

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

    ax.set_title("Portfolio Balance Over Time")
    ax.set_ylabel("Balance (USD / token units)")
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
        help="Overwrite output file if it already exists.",
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

    hdr = ["Wallet", "Chain", "Asset", "Contract", "Protocol", "Balance"]
    csv_rows = [[w, c, a, co, p, f"{b:.2f}"] for w, c, a, co, p, b in rows]
    write_csv(outfile, hdr, csv_rows)
    print(f"\n  Saved to {outfile}\n")


def run_range(args):
    from_dt = parse_date(args.from_date)
    to_dt = parse_date(args.to_date)

    if from_dt >= to_dt:
        sys.exit("--from must be before --to.")

    from_str = from_dt.strftime("%Y-%m-%d")
    to_str = to_dt.strftime("%Y-%m-%d")
    outfile = OUTPUT_DIR / f"snapshot_{from_str}_to_{to_str}.csv"
    chart_file = OUTPUT_DIR / f"snapshot_{from_str}_to_{to_str}.png"

    if outfile.exists() and not args.force:
        print(f"\n  [!] {outfile} already exists. Use --force to overwrite.\n")
        return

    # Build list of weekly dates
    dates = []
    current = from_dt
    while current <= to_dt:
        dates.append(current)
        current += timedelta(days=7)

    total = len(dates)
    print(f"\n  RANGE SNAPSHOT — {from_str} to {to_str} ({total} weeks)\n")

    hdr = ["Date", "Wallet", "Chain", "Asset", "Contract", "Protocol", "Balance"]
    all_csv_rows = []

    now = datetime.now(timezone.utc)
    for i, dt in enumerate(dates, 1):
        # Cap to current time if date is in the future
        effective_dt = min(dt, now)
        date_label = dt.strftime("%Y-%m-%d")
        ts = int(effective_dt.timestamp())
        print(f"  [{i:>{len(str(total))}}/{total}] {date_label} ...", end=" ", flush=True)

        try:
            rows = collect_snapshot(ts)
            count = len(rows)
            for w, c, a, co, p, b in rows:
                all_csv_rows.append([date_label, w, c, a, co, p, f"{b:.2f}"])
            print(f"{count} positions")
        except Exception as e:
            print(f"ERROR: {e}")

        # Small delay to be nice to RPCs
        if i < total:
            time.sleep(1)

    if not all_csv_rows:
        print("\n  No balances found across the range.\n")
        return

    write_csv(outfile, hdr, all_csv_rows)
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
