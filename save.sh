#!/usr/bin/env bash
set -e

MSG="${1:-Auto update}"

git add .

if git diff --cached --quiet; then
    echo "No changes to commit."
    exit 0
fi

git commit -m "$MSG"
git push

echo "Saved to GitHub."
