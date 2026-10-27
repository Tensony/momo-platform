import json
import os
import secrets
import subprocess
import sys
import tempfile
import time

AWS_CLI = os.environ.get("AWS_CLI", "aws")
AWS_PROFILE = os.environ.get("AWS_PROFILE")
REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID")
CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID")
ALB_URL = os.environ.get("API_BASE_URL")
PROJECT = os.environ.get("AWS_PROJECT", "momo")
ENVIRONMENT = os.environ.get("AWS_ENVIRONMENT", "dev")
SAGA_STEPS_FUNCTION = os.environ.get("SAGA_STEPS_FUNCTION", f"{PROJECT}-{ENVIRONMENT}-saga-steps")
FRAUD_CONFIG_PARAM = os.environ.get("FRAUD_CONFIG_PARAM", f"/{PROJECT}/fraud/config")


def validate_config():
    required = {
        "AWS_REGION (or AWS_DEFAULT_REGION)": REGION,
        "COGNITO_USER_POOL_ID": USER_POOL_ID,
        "COGNITO_CLIENT_ID": CLIENT_ID,
        "API_BASE_URL": ALB_URL,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit(f"Set the required live-proof configuration: {', '.join(missing)}")


def run_aws(args):
    cmd = [AWS_CLI] + args + ["--region", REGION, "--output", "json"]
    env = os.environ.copy()
    if AWS_PROFILE:
        env["AWS_PROFILE"] = AWS_PROFILE
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if res.returncode != 0:
        operation = " ".join(args[:2])
        raise RuntimeError(f"AWS CLI operation {operation} failed: {res.stderr.strip()}")
    # Strip warning banners before decoding the CLI's JSON response.
    stdout = res.stdout.strip()
    if stdout.startswith("urllib3") or stdout.startswith("Warning:"):
        lines = stdout.splitlines()
        stdout = "\n".join(
            line for line in lines
            if not line.startswith("urllib3")
            and not line.startswith("Warning:")
            and "InsecureRequestWarning" not in line
        ).strip()
    return json.loads(stdout) if stdout else {}


def update_lambda_environment(function_name, variables):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".json") as tf:
        json.dump({"Variables": variables}, tf)
        path = tf.name
    try:
        run_aws([
            "lambda", "update-function-configuration",
            "--function-name", function_name,
            "--environment", f"file://{path}",
        ])
    finally:
        os.remove(path)


def get_token(email, group):
    pw = f"Aa1!{secrets.token_hex(8)}"
    print(f"Ensuring user {email} in {group}...")
    try:
        run_aws([
            "cognito-idp", "admin-get-user",
            "--user-pool-id", USER_POOL_ID,
            "--username", email,
        ])
    except RuntimeError as exc:
        if "UserNotFoundException" not in str(exc):
            raise
        run_aws([
            "cognito-idp", "admin-create-user",
            "--user-pool-id", USER_POOL_ID,
            "--username", email,
            "--message-action", "SUPPRESS",
            "--user-attributes", f"Name=email,Value={email}", "Name=email_verified,Value=true"
        ])

    run_aws([
        "cognito-idp", "admin-set-user-password",
        "--user-pool-id", USER_POOL_ID,
        "--username", email,
        "--password", pw,
        "--permanent"
    ])

    run_aws([
        "cognito-idp", "admin-add-user-to-group",
        "--user-pool-id", USER_POOL_ID,
        "--username", email,
        "--group-name", group
    ])

    auth = run_aws([
        "cognito-idp", "initiate-auth",
        "--client-id", CLIENT_ID,
        "--auth-flow", "USER_PASSWORD_AUTH",
        "--auth-parameters", f"USERNAME={email},PASSWORD={pw}"
    ])
    token = auth["AuthenticationResult"]["AccessToken"]
    print(f"Got token for {email} (len={len(token)})")
    return token

def main():
    validate_config()
    fn_config = run_aws(["lambda", "get-function-configuration", "--function-name", SAGA_STEPS_FUNCTION])
    env_vars = fn_config.get("Environment", {}).get("Variables", {})
    original_env_vars = dict(env_vars)
    parameter = run_aws([
        "ssm", "get-parameter",
        "--name", FRAUD_CONFIG_PARAM,
        "--with-decryption",
    ])["Parameter"]
    original_fraud_config = parameter["Value"]
    parameter_type = parameter["Type"]

    try:
        print("=== Step 1: Temporarily enable fault injection and relax fraud limits ===")
        run_aws([
            "ssm", "put-parameter",
            "--name", FRAUD_CONFIG_PARAM,
            "--type", parameter_type,
            "--overwrite",
            "--value", json.dumps({"velocity_max_txns": 100000})
        ])
        env_vars = dict(original_env_vars)
        env_vars["ENABLE_FAULT_INJECTION"] = "true"
        update_lambda_environment(SAGA_STEPS_FUNCTION, env_vars)
        print("Waiting 10s for Lambda configuration update to settle...")
        time.sleep(10)

        print("\n=== Step 2: Fetch Cognito Tokens ===")
        user_tok = get_token("proof-customer@example.com", "customers")
        user2_tok = get_token("proof-customer2@example.com", "customers")
        agent_tok = get_token("proof-agent@example.com", "agents")
        admin_tok = get_token("proof-admin@example.com", "admins")

        print("\n=== Step 3: Run proofs against live ALB ===")
        proof_cmd = [
            sys.executable,
            os.path.join("scripts", "proofs", "run_proofs.py"),
            "--base-url", ALB_URL,
        ]
        if os.environ.get("PROOF_INSECURE_TLS", "").lower() == "true":
            proof_cmd.append("--insecure")
        proof_env = os.environ.copy()
        proof_env.update({
            "PROOF_ADMIN_TOKEN": admin_tok,
            "PROOF_AGENT_TOKEN": agent_tok,
            "PROOF_USER_TOKEN": user_tok,
            "PROOF_USER2_TOKEN": user2_tok,
        })
        result = subprocess.run(proof_cmd, env=proof_env, check=False)
        if result.returncode:
            raise SystemExit(result.returncode)
    finally:
        errors = []
        try:
            update_lambda_environment(SAGA_STEPS_FUNCTION, original_env_vars)
        except Exception as exc:
            errors.append(f"Could not restore Lambda environment: {exc}")
        run_aws([
            "ssm", "put-parameter",
            "--name", FRAUD_CONFIG_PARAM,
            "--type", parameter_type,
            "--overwrite",
            "--value", original_fraud_config,
        ])
        print("\n=== Step 4: Restore environment configuration ===")
        if errors:
            raise RuntimeError("; ".join(errors))
        print("Restoration complete.")

if __name__ == "__main__":
    main()
