#!/usr/bin/env bash
set -e

# Scanye Release Script

# 1. Check if we are on main branch
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$BRANCH" != "main" ]]; then
  echo "Error: Not on main branch."
  exit 1
fi

# 2. Get version from pyproject.toml
VERSION=$(grep -m 1 "version =" pyproject.toml | cut -d '"' -f 2)
echo "Current version: $VERSION"

# 3. Tag the release
echo "Tagging version v$VERSION..."
git tag -a "v$VERSION" -m "Release v$VERSION"

# 4. Push tags
echo "Pushing tags..."
git push origin "v$VERSION"

echo "Release v$VERSION tagged and pushed."
