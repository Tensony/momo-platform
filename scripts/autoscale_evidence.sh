#!/usr/bin/env bash
# Run after (or during) load/k6-autoscale.js to capture proof that auto scaling worked.
set -euo pipefail
cd "$(dirname "$0")/.."
TF="terraform -chdir=infra/terraform"
REGION="$($TF output -raw region)"; CLUSTER="$($TF output -raw ecs_cluster)"; SERVICE="$($TF output -raw ecs_service)"
OUT=evidence-out; mkdir -p "$OUT"
aws application-autoscaling describe-scaling-activities --region "$REGION" --service-namespace ecs \
  --resource-id "service/$CLUSTER/$SERVICE" --query 'ScalingActivities[].{time:StartTime,cause:Cause,status:StatusCode,desc:Description}' \
  --output json > "$OUT/autoscaling-activities.json"
END=$(date -u +%Y-%m-%dT%H:%M:%SZ); START=$(date -u -d '-90 minutes' +%Y-%m-%dT%H:%M:%SZ)
aws cloudwatch get-metric-statistics --region "$REGION" --namespace ECS/ContainerInsights --metric-name RunningTaskCount \
  --dimensions Name=ClusterName,Value="$CLUSTER" Name=ServiceName,Value="$SERVICE" --start-time "$START" --end-time "$END" \
  --period 60 --statistics Maximum --query 'sort_by(Datapoints,&Timestamp)' --output json > "$OUT/running-task-count.json"
python3 - <<'PY'
import json
a=json.load(open("evidence-out/autoscaling-activities.json")); t=json.load(open("evidence-out/running-task-count.json"))
print("scaling activities:",len(a))
for x in a[:10]: print(" ",x["time"],x["status"],x["desc"])
if t: print("running tasks: min",min(p["Maximum"] for p in t),"max",max(p["Maximum"] for p in t))
PY
