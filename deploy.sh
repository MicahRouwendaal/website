#!/usr/bin/env bash
set -euo pipefail

DOMAIN_ARG="${1:-}"

if [ ! -f ".env" ]; then
  cp .env.example .env
fi

if [ -n "$DOMAIN_ARG" ]; then
  if grep -q '^DOMAIN=' .env; then
    sed -i.bak "s/^DOMAIN=.*/DOMAIN=$DOMAIN_ARG/" .env
    rm -f .env.bak
  else
    echo "DOMAIN=$DOMAIN_ARG" >> .env
  fi
fi

docker compose up -d --build
