#!/bin/bash
# nightly-backup.sh — dated archive of JARVIS config and sensitive files
# Run via scheduled task or cron. Keeps last 14 days.

BACKUP_DIR="/c/ServerData/Backups/jarvis"
DATE=$(date +%Y%m%d-%H%M%S)
ARCHIVE="$BACKUP_DIR/jarvis-backup-$DATE.tar.gz"
KEEP=14

# Create backup dir if needed
mkdir -p "$BACKUP_DIR"

# Source files to back up (covers gitignored paths that burned us)
FILES=(
    "C:/ServerData/Stacks/jarvis/jarvis-prompt.txt"
    "C:/ServerData/Stacks/jarvis/docker-compose.yml"
    "C:/ServerData/Stacks/jarvis/.env.example"
    "C:/ServerData/Stacks/jarvis/ui"
    "C:/ServerData/Stacks/jarvis/wa/contacts.json"
    "C:/ServerData/Stacks/jarvis/wa/sessions"
    "C:/ServerData/Stacks/jarvis/secrets.env"
    "C:/ServerData/Stacks/jarvis/jarvis/secrets.env"
)

# Build tarball (only existing files)
TAR_ARGS=()
for f in "${FILES[@]}"; do
    if [ -e "$f" ]; then
        TAR_ARGS+=("$f")
    fi
done

if [ ${#TAR_ARGS[@]} -eq 0 ]; then
    echo "No files to backup"
    exit 1
fi

tar -czf "$ARCHIVE" "${TAR_ARGS[@]}"
echo "Backup created: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"

# Prune old backups (keep $KEEP)
cd "$BACKUP_DIR"
ls -t jarvis-backup-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
echo "Pruned old backups (kept $KEEP)"
