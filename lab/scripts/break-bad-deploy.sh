#!/usr/bin/env bash
# Seeded failure 2: deploy the tip of main, which carries a commit that breaks startup.
exec "$(dirname "$0")/jenkins-deploy.sh" main
