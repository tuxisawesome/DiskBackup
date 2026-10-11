#!/bin/sh
# Starts DiskBackup.
cd "$(dirname "$0")" && exec python3 -m diskbackup "$@"
