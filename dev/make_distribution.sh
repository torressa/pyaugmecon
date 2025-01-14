#!/bin/bash

set -o pipefail
set -e

FWDIR="$(cd "`dirname "${BASH_SOURCE[0]}"`"; pwd)"
cd "$FWDIR/.."

rm -rf dist
rm -rf pyaugmecon.egg-info
python setup.py sdist
python setup.py bdist_wheel
twine upload dist/*
