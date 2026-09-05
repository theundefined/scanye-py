#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

# Load .env if it exists (in case PYPI_TOKEN is there instead of bashrc)
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

if [ -z "$PYPI_TOKEN" ]; then
    echo "Error: PYPI_TOKEN environment variable is not set."
    echo "Please ensure it is exported in your shell or available in .env"
    exit 1
fi

echo "Cleaning up previous builds..."
rm -rf dist/ build/ src/*.egg-info

echo "Building package..."
python3 -m build

echo "Uploading package to PyPI..."
# Using __token__ as username for PyPI tokens
python3 -m twine upload dist/* -u __token__ -p "$PYPI_TOKEN"

echo "Success! Package published to PyPI."
