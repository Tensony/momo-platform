#!/usr/bin/env bash
# First-time deploy into a CLEAN AWS account from this repository, then every later release.
#   AWS credentials must be configured (aws configure sso / env vars). Requires: terraform, docker, aws, python3.
#   usage: scripts/deploy.sh <alert-email> [region]
set -euo pipefail
cd "$(dirname "$0")/.."
EMAIL="${1:?usage: deploy.sh <alert-email> [region]}"
REGION="${2:-af-south-1}"
TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo manual)}"
TF="terraform -chdir=infra/terraform"
VARS=(-var "alert_email=$EMAIL" -var "region=$REGION" -var "api_image_tag=$TAG")

echo "==> 1/5 build Lambda artifacts";   scripts/build_lambdas.sh
echo "==> 2/5 terraform init";           $TF init -input=false
echo "==> 3/5 create the image registry first (chicken-and-egg: ECS needs an image to start)"
$TF apply -input=false -auto-approve "${VARS[@]}" -target=aws_ecr_repository.api -target=aws_kms_key.data
REPO="$($TF output -raw ecr_repository_url)"
echo "==> 4/5 build and push image $REPO:$TAG"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${REPO%%/*}"
docker build -t "$REPO:$TAG" .
docker push "$REPO:$TAG"
echo "==> 5/5 apply everything"
$TF apply -input=false -auto-approve "${VARS[@]}"
echo; $TF output
echo "Confirm the SNS e-mail subscription that AWS just sent to $EMAIL, or alarms will not reach you."
