#!/usr/bin/env bash

set -euo pipefail

# =========================
# ESXi configuration
# =========================

ESXI_HOST="192.168.1.100"
ESXI_USER="root"
ESXI_PASSWORD="YourPassword"

DATASTORE="datastore1"
DESTINATION_DIR="ISO"

# =========================
# Check arguments
# =========================

if [[ $# -ne 1 ]]; then
    echo "Usage:"
    echo "  $0 <image.iso>"
    echo
    echo "Example:"
    echo "  $0 ubuntu-24.04.iso"
    exit 1
fi

ISO_FILE="$1"

# =========================
# Check ISO
# =========================

if [[ ! -f "$ISO_FILE" ]]; then
    echo "ERROR: File does not exist:"
    echo "  $ISO_FILE"
    exit 1
fi

if [[ "${ISO_FILE,,}" != *.iso ]]; then
    echo "ERROR: File must have .iso extension"
    exit 1
fi

# =========================
# Check GOVC
# =========================

if ! command -v govc >/dev/null 2>&1; then
    echo "ERROR: govc is not installed."
    echo
    echo "Install govc:"
    echo "https://github.com/vmware/govmomi/releases"
    exit 1
fi

# =========================
# GOVC configuration
# =========================

export GOVC_URL="https://${ESXI_HOST}/sdk"
export GOVC_USERNAME="${ESXI_USER}"
export GOVC_PASSWORD="${ESXI_PASSWORD}"

# ESXi normally uses self-signed certificate
export GOVC_INSECURE=1

# =========================
# Test connection
# =========================

echo "Testing connection to ESXi: ${ESXI_HOST}"

if ! govc about >/dev/null 2>&1; then
    echo "ERROR: Cannot connect to ESXi."
    exit 1
fi

echo "Connection OK."

# =========================
# Check datastore
# =========================

echo "Checking datastore: ${DATASTORE}"

if ! govc datastore.info "${DATASTORE}" >/dev/null 2>&1; then
    echo "ERROR: Datastore '${DATASTORE}' does not exist."
    echo
    echo "Available datastores:"
    govc datastore.info
    exit 1
fi

# =========================
# Create ISO directory
# =========================

echo "Creating directory: ${DESTINATION_DIR}"

govc datastore.mkdir \
    -ds="${DATASTORE}" \
    "${DESTINATION_DIR}" 2>/dev/null || true

# =========================
# Upload
# =========================

ISO_NAME="$(basename "$ISO_FILE")"

REMOTE_PATH="${DESTINATION_DIR}/${ISO_NAME}"

echo
echo "Uploading:"
echo "  Source:    ${ISO_FILE}"
echo "  ESXi:      ${ESXI_HOST}"
echo "  Datastore: ${DATASTORE}"
echo "  Target:    ${REMOTE_PATH}"
echo

govc datastore.upload \
    -ds="${DATASTORE}" \
    "$ISO_FILE" \
    "$REMOTE_PATH"

# =========================
# Verification
# =========================

echo
echo "Verifying upload..."

if govc datastore.ls \
    -ds="${DATASTORE}" \
    "${REMOTE_PATH}" >/dev/null 2>&1; then

    echo
    echo "========================================="
    echo "Upload completed successfully"
    echo "========================================="
    echo "ESXi:      ${ESXI_HOST}"
    echo "Datastore: ${DATASTORE}"
    echo "ISO:       ${REMOTE_PATH}"
    echo
else
    echo "ERROR: Upload completed but file verification failed."
    exit 1
fi