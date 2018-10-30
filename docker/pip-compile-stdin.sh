#!/bin/sh

cat - > requirements.in
pip-compile --upgrade --generate-hashes requirements.in
