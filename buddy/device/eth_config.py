"""Base Sepolia wallet configuration. Non-secret — safe to commit.

The private key never lives here; it is generated on-device and stored
encrypted in /flash/wallet.dat (see eth_keystore). This file only holds
network endpoints and display defaults.
"""

CHAIN_ID = 84532                       # Base Sepolia (0x14a34)
EXPLORER = "https://sepolia.basescan.org"

# Public RPC endpoints (no API key). The first is tried first; the rest
# are fallbacks on network error / non-200 (e.g. 429 rate limit).
RPC_URLS = (
    "https://sepolia.base.org",
    "https://base-sepolia-rpc.publicnode.com",
    "https://84532.rpc.thirdweb.com",
)

# Default ERC-20 token shown on the wallet screen: Circle's testnet USDC
# on Base Sepolia (6 decimals).
ERC20_ADDRESS = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
ERC20_SYMBOL = "USDC"
ERC20_DECIMALS = 6

# Optional saved recipients so the user can pick instead of typing a full
# 42-char address on the tiny keyboard. Each entry is (label, address). The
# send flow offers these between "Self (own address)" and "Type address...".
# Testnet-only addresses; safe to commit (public on BaseScan).
ADDRESS_BOOK = (
    ("Test EOA 2", "0xC94d68094FA65E991dFfa0A941306E8460876169"),
)

# Some public endpoints sit behind Cloudflare, which 1010-bans the default
# MicroPython urllib User-Agent. Send a browser UA to be safe (matches the
# btc_price app's workaround).
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
