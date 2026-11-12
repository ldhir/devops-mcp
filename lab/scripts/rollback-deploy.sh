#!/usr/bin/env bash
# Undo seeded failure 2: deploy the last known good commit.
exec "$(dirname "$0")/jenkins-deploy.sh" last-good
