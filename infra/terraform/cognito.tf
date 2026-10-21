# Authentication: Amazon Cognito. The API verifies the access token itself (src/momo/api/auth.py):
# signature (JWKS), issuer, token_use=access, client_id, expiry. Authorization uses Cognito groups.

resource "aws_cognito_user_pool" "main" {
  name                     = "${local.name}-users"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "OPTIONAL"

  software_token_mfa_configuration { enabled = true }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 3
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  admin_create_user_config { allow_admin_create_user_only = false }
  deletion_protection = var.deletion_protection ? "ACTIVE" : "INACTIVE"
}

resource "aws_cognito_user_pool_client" "api" {
  name         = "${local.name}-api"
  user_pool_id = aws_cognito_user_pool.main.id

  generate_secret                      = false # public client (mobile/web): no secret to leak
  explicit_auth_flows                  = ["ALLOW_USER_SRP_AUTH", "ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  access_token_validity                = 60
  id_token_validity                    = 60
  refresh_token_validity               = 7
  allowed_oauth_flows_user_pool_client = false
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = var.cognito_callback_urls

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

resource "aws_cognito_user_group" "groups" {
  for_each     = toset(["customers", "merchants", "agents", "admins"])
  name         = each.key
  user_pool_id = aws_cognito_user_pool.main.id
  description  = "${each.key} role"
}
