#!/usr/bin/env bash
# Creates demo users (customer, agent, admin) in Cognito and prints access tokens for the proof scripts.
# Passwords are generated at run time and printed once; nothing is stored in the repository.
set -euo pipefail
cd "$(dirname "$0")/.."
TF="terraform -chdir=infra/terraform"
POOL="$($TF output -raw cognito_user_pool_id)"; CLIENT="$($TF output -raw cognito_client_id)"; REGION="$($TF output -raw region)"
mk() { # email group
  local email="$1" group="$2" pw; pw="Aa1!$(openssl rand -hex 8)"
  aws cognito-idp admin-create-user --region "$REGION" --user-pool-id "$POOL" --username "$email" \
     --message-action SUPPRESS --user-attributes Name=email,Value="$email" Name=email_verified,Value=true >/dev/null 2>&1 || true
  aws cognito-idp admin-set-user-password --region "$REGION" --user-pool-id "$POOL" --username "$email" --password "$pw" --permanent
  aws cognito-idp admin-add-user-to-group --region "$REGION" --user-pool-id "$POOL" --username "$email" --group-name "$group"
  aws cognito-idp initiate-auth --region "$REGION" --client-id "$CLIENT" --auth-flow USER_PASSWORD_AUTH \
     --auth-parameters "USERNAME=$email,PASSWORD=$pw" --query 'AuthenticationResult.AccessToken' --output text
}
echo "export USER_TOKEN=$(mk proof-customer@example.com customers)"
echo "export AGENT_TOKEN=$(mk proof-agent@example.com agents)"
echo "export ADMIN_TOKEN=$(mk proof-admin@example.com admins)"
