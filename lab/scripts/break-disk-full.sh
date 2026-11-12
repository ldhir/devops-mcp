#!/usr/bin/env bash
# Seeded failure 1: someone leaves debug payload capture on; MongoDB's disk fills.
curl -s -X POST http://127.0.0.1:8080/admin/debug-capture/on; echo
