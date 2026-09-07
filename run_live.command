#!/bin/bash
cd "$(dirname "$0")"

if [ -z "$JUPITER_API_KEY" ]; then
  echo "Error: JUPITER_API_KEY environment variable is not set."
  echo "Usage: export JUPITER_API_KEY='your_api_key' && ./run_live.command"
  exit 1
fi

if [ -z "$SOLANA_KEYPAIR" ]; then
  echo "Error: SOLANA_KEYPAIR environment variable is not set."
  echo "Usage: export SOLANA_KEYPAIR=\"\$HOME/.config/solana/id.json\" && ./run_live.command"
  exit 1
fi

python3 punt_oems.py --live --keypair "$SOLANA_KEYPAIR"
