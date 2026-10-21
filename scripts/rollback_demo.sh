#!/usr/bin/env bash
# Demonstrates AUTOMATED ROLLBACK for the marker: deploys a deliberately broken release and shows
# CodeDeploy reverting to the previous task definition without human action.
#   1. record the currently serving task definition
#   2. register a new task definition whose container fails its /health check (FORCE_UNHEALTHY=true)
#   3. start a CodeDeploy blue/green deployment to it
#   4. wait: the green task set never becomes healthy -> CodeDeploy fails the deployment and rolls back
#   5. show that the original task definition is still serving and the evidence JSON
set -euo pipefail
cd "$(dirname "$0")/.."
TF="terraform -chdir=infra/terraform"
REGION="$($TF output -raw region)"; CLUSTER="$($TF output -raw ecs_cluster)"; SERVICE="$($TF output -raw ecs_service)"
APP="$($TF output -raw codedeploy_app)"; GROUP="$($TF output -raw codedeploy_deployment_group)"
OUT=evidence-out; mkdir -p "$OUT"

GOOD_TD="$(aws ecs describe-services --region "$REGION" --cluster "$CLUSTER" --services "$SERVICE" \
           --query 'services[0].taskDefinition' --output text)"
echo "serving (good) task definition: $GOOD_TD"

BAD_JSON="$(aws ecs describe-task-definition --region "$REGION" --task-definition "$GOOD_TD" --query taskDefinition \
  | python3 -c '
import json,sys
td=json.load(sys.stdin)
for k in ("taskDefinitionArn","revision","status","requiresAttributes","compatibilities","registeredAt","registeredBy"):
    td.pop(k,None)
for c in td["containerDefinitions"]:
    if c["name"]=="api":
        c["environment"].append({"name":"FORCE_UNHEALTHY","value":"true"})
print(json.dumps(td))')"
BAD_TD="$(aws ecs register-task-definition --region "$REGION" --cli-input-json "$BAD_JSON" \
          --query taskDefinition.taskDefinitionArn --output text)"
echo "broken task definition: $BAD_TD"

APPSPEC="$(python3 -c "
import json
print(json.dumps({'version':'0.0','Resources':[{'TargetService':{'Type':'AWS::ECS::Service','Properties':{
  'TaskDefinition':'$BAD_TD','LoadBalancerInfo':{'ContainerName':'api','ContainerPort':8080}}}}]}))")"
DEP="$(aws deploy create-deployment --region "$REGION" --application-name "$APP" --deployment-group-name "$GROUP" \
        --revision "{\"revisionType\":\"AppSpecContent\",\"appSpecContent\":{\"content\":$(python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "$APPSPEC")}}" \
        --query deploymentId --output text)"
echo "deployment $DEP started; waiting for CodeDeploy to fail it and roll back..."

for i in $(seq 1 60); do
  STATUS="$(aws deploy get-deployment --region "$REGION" --deployment-id "$DEP" --query deploymentInfo.status --output text)"
  echo "  [$i] $STATUS"
  case "$STATUS" in Failed|Stopped|Succeeded) break;; esac
  sleep 20
done
aws deploy get-deployment --region "$REGION" --deployment-id "$DEP" > "$OUT/rollback-deployment.json"
python3 - "$OUT/rollback-deployment.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))["deploymentInfo"]
print("status            :",d["status"])
print("rollback          :",d.get("rollbackInfo",{}).get("rollbackMessage","(none)"))
print("error             :",d.get("errorInformation",{}))
PY
NOW_TD="$(aws ecs describe-services --region "$REGION" --cluster "$CLUSTER" --services "$SERVICE" \
          --query 'services[0].taskDefinition' --output text)"
echo "task definition serving after rollback: $NOW_TD"
[ "$NOW_TD" = "$GOOD_TD" ] && echo "PASS: automatic rollback restored $GOOD_TD" || { echo "FAIL: service is not on the original task definition"; exit 1; }
