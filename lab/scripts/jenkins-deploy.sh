#!/usr/bin/env bash
# Deploy a branch, tag or commit of orders-api through Jenkins and wait for the result.
# Usage: jenkins-deploy.sh <ref>      e.g. jenkins-deploy.sh main | last-good | 8d3fb2b
set -euo pipefail
cd "$(dirname "$0")/.."
REF="${1:?give a branch, tag or commit}"
set -a; . ./.env; set +a
J=http://127.0.0.1:8081; A="admin:$JENKINS_ADMIN_PASSWORD"; JAR=$(mktemp)
CRUMB=$(curl -s -c "$JAR" -u "$A" "$J/crumbIssuer/api/json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["crumbRequestField"]+":"+d["crumb"])')
BEFORE=$(curl -s -u "$A" "$J/job/deploy-orders-api/api/json?tree=nextBuildNumber" | python3 -c 'import json,sys; print(json.load(sys.stdin)["nextBuildNumber"])')
curl -s -o /dev/null -b "$JAR" -u "$A" -H "$CRUMB" -X POST "$J/job/deploy-orders-api/buildWithParameters?REF=$REF"
rm -f "$JAR"
for _ in $(seq 1 90); do
  R=$(curl -s -u "$A" "$J/job/deploy-orders-api/$BEFORE/api/json?tree=number,result,building,description" || true)
  if echo "$R" | grep -q '"building":false'; then break; fi
  python3 -c 'import time; time.sleep(3)'
done
echo "$R" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("build #%s %s  %s" % (d["number"], d["result"], d.get("description") or ""))'
