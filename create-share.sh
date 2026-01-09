#!/bin/bash
# Script to create FileBrowser shares via API
# Usage: ./create-share.sh [path]
# Example: ./create-share.sh /2

echo "Creating new share for folder..."

# Use environment variable or prompt for credentials
if [ -z "$DROPPR_PASSWORD" ]; then
    # Try to read from .env file
    if [ -f .env ] && grep -q DROPPR_PASSWORD .env; then
        source .env
    fi
fi

DROPPR_USER="${DROPPR_USER:-admin}"
if [ -z "$DROPPR_PASSWORD" ]; then
    echo "Error: DROPPR_PASSWORD environment variable not set"
    echo "Set it via: export DROPPR_PASSWORD='your_password'"
    echo "Or add DROPPR_PASSWORD=your_password to .env file"
    exit 1
fi

# Login and get token
TOKEN=$(curl -s -X POST https://droppr.lucheestiy.com/api/login \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"$DROPPR_USER\",\"password\":\"$DROPPR_PASSWORD\"}")

echo "Got authentication token"

# Create share (adjust path as needed)
FOLDER_PATH="${1:-/2}"
SHARE_RESPONSE=$(curl -s -X POST "https://droppr.lucheestiy.com/api/share${FOLDER_PATH}" \
     -H "X-Auth: $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"expires":"", "password":""}')

echo "Share created: $SHARE_RESPONSE"

# Extract hash if successful
if [[ $SHARE_RESPONSE == *"hash"* ]]; then
    HASH=$(echo "$SHARE_RESPONSE" | grep -o '"hash":"[^"]*"' | cut -d'"' -f4)
    echo "✅ Share URL: https://droppr.lucheestiy.com/api/public/dl/$HASH"
    echo "📱 This will redirect to beautiful media gallery!"
else
    echo "❌ Failed to create share: $SHARE_RESPONSE"
fi
