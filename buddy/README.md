# buddy

MicroPython app bundle for the M5Stack Cardputer-Adv. Installed onto `/flash/` by the [`m5-onboard`](../onboard/) skill — see the [monorepo README](../README.md) for the end-to-end flow.

## What's on the device

```
/flash/
├── main.py              launcher menu (replaces UIFlow's boot flow)
├── buddy_*.py           shared libs (BLE, UI, state, protocol, chars)
├── burst_frames.py      sprite frames
└── apps/
    ├── claude_buddy.py  BLE client that pairs with Claude.app's Hardware Buddy
    ├── hello_cardputer.py
    └── snake.py
```

`main.py` scans `/flash/apps/` at boot and shows every `.py` as a menu entry. Drop a new file in there, re-run `m5-onboard go` (or `install_apps.py --src buddy`), and it shows up.

## Claude Buddy (BLE)

Open Claude → Developer menu → **Hardware Buddy** → Connect. BLE-only. Stats (approvals / denials / level) persist across reboots via NVS under the `buddy` namespace.

## Base Wallet (on-chain, testnet)

`apps/base_wallet.py` is a self-contained Ethereum wallet for the **Base Sepolia** testnet (chain id 84532). The private key is generated on-device (hardware RNG), encrypted at rest with a PIN, and used to **sign transactions locally** — keccak256, secp256k1 ECDSA (RFC 6979 deterministic, low-s, EIP-155) and RLP are all pure MicroPython, since the UIFlow firmware ships no crypto beyond sha256/AES. Signed transactions are broadcast over WiFi+HTTPS via the public `sepolia.base.org` RPC.

What it does: create/unlock a PIN-protected wallet, show the address + native ETH balance + an ERC-20 balance (Base Sepolia USDC by default), and send native ETH or an ERC-20 transfer with an explicit confirm screen before signing.

```
/flash/
├── eth_keccak.py       keccak256 (Ethereum 0x01 padding)
├── eth_secp256k1.py    ECDSA sign (RFC6979 + recovery id), pubkey derivation
├── eth_rlp.py          RLP encoder
├── eth_account.py      address derivation, legacy EIP-155 tx, ERC-20 calldata
├── eth_rpc.py          Base Sepolia JSON-RPC over HTTPS
├── eth_keystore.py     PIN → PBKDF2 → AES-256-CBC keystore (wallet.dat)
├── eth_config.py       RPC endpoints / chain id / token (non-secret)
└── apps/base_wallet.py the wallet UI
```

The shared `eth_*` libs sit at `/flash` root so they import cleanly but don't show up as launcher menu entries (the launcher only lists `apps/`).

> **TESTNET ONLY.** The key lives in flash encrypted by a short PIN on a dev board — this is not a secure element. Fund the address only with throwaway Base Sepolia test ETH from a faucet (Alchemy / thirdweb / QuickNode). First boot shows your new address; copy it to a faucet, then press **B** to load balances and **S** to send.

The crypto is verified on-device against canonical vectors (EIP-155 example transaction, Hardhat account #0 address, keccak empty/abc) and a full sign+RLP+keccak pipeline runs in ~0.6 s.

## Iterating on device code

`scripts/` has dev tooling for editing device sources without re-running the full onboard flow:

```bash
# Push a subset of files over USB-serial
python3 scripts/push.py --port /dev/cu.usbmodem1101 --files apps/snake.py

# Watch device logs
python3 scripts/tail_serial.py --port /dev/cu.usbmodem1101

# One-shot REPL exec
python3 scripts/repl_run.py --port /dev/cu.usbmodem1101 --script "import os; print(os.listdir('/flash'))"
```

`gen_burst_frames.py` regenerates `burst_frames.py` from source sprites.

## References

- `references/` — BLE protocol notes for the Claude Buddy app

## License

Apache 2.0 — see the [root LICENSE](../LICENSE) and [LICENSE-THIRD-PARTY.md](../LICENSE-THIRD-PARTY.md).
