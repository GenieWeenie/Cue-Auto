#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="${1:-./certs}"
DOMAIN="${2:-localhost}"
DAYS="${3:-365}"

mkdir -p "$OUT_DIR"

KEY_PATH="$OUT_DIR/webhook.key"
CERT_PATH="$OUT_DIR/webhook.crt"

openssl req \
  -x509 \
  -newkey rsa:4096 \
  -sha256 \
  -nodes \
  -keyout "$KEY_PATH" \
  -out "$CERT_PATH" \
  -days "$DAYS" \
  -subj "/CN=$DOMAIN" \
  -addext "subjectAltName=DNS:$DOMAIN,DNS:localhost,IP:127.0.0.1"

echo "Generated self-signed certificate:"
echo "  key:  $KEY_PATH"
echo "  cert: $CERT_PATH"
