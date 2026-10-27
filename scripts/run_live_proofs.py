import json
import os
import secrets
import subprocess
import sys
import time

AWS_CLI = r"C:\Program Files\Amazon\AWSCLIV2\aws.exe"
AWS_PROFILE = "momo-dev"
REGION = "us-east-1"
USER_POOL_ID = "us-east-1_3VY9lbEfS"
CLIENT_ID = "16g6c982echi7t8erlltbmvenm"
ALB_URL = "https://momo-dev-alb-5850337.us-east-1.elb.amazonaws.com"

def run_aws(args):
    cmd = [AWS_CLI] + args + ["--region", REGION, "--output", "json", "--no-verify-ssl"]
    env = os.environ.copy()
    env["AWS_PROFILE"] = AWS_PROFILE
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if res.returncode != 0:
        raise RuntimeError(f"AWS CLI failed: {cmd}\nstderr: {res.stderr}\nstdout: {res.stdout}")
    # filter out insecure request warnings if present
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

def get_token(email, group):
    pw = f"Aa1!{secrets.token_hex(8)}"
    print(f"Ensuring user {email} in {group}...")
    try:
        run_aws([
            "cognito-idp", "admin-create-user",
            "--user-pool-id", USER_POOL_ID,
            "--username", email,
            "--message-action", "SUPPRESS",
            "--user-attributes", f"Name=email,Value={email}", "Name=email_verified,Value=true"
        ])
    except Exception:
        # already exists
        pass

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
    print("=== Step 1: Temporarily enable fault injection and relax fraud limits ===")
    run_aws([
        "ssm", "put-parameter",
        "--name", "/momo/fraud/config",
        "--type", "String",
        "--overwrite",
        "--value", json.dumps({"velocity_max_txns": 100000})
    ])

    fn_config = run_aws(["lambda", "get-function-configuration", "--function-name", "momo-dev-saga-steps"])
    env_vars = fn_config.get("Environment", {}).get("Variables", {})
    env_vars["ENABLE_FAULT_INJECTION"] = "true"

    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as tf:
        json.dump({"Variables": env_vars}, tf)
        tf_name = tf.name
    try:
        run_aws([
            "lambda", "update-function-configuration",
            "--function-name", "momo-dev-saga-steps",
            "--environment", f"file://{tf_name}"
        ])
    finally:
        if os.path.exists(tf_name):
            os.remove(tf_name)
    print("Waiting 10s for Lambda configuration update to settle...")
    time.sleep(10)

    try:
        print("\n=== Step 2: Fetch Cognito Tokens ===")
        user_tok = get_token("proof-customer@example.com", "customers")
        user2_tok = get_token("proof-customer2@example.com", "customers")
        agent_tok = get_token("proof-agent@example.com", "agents")
        admin_tok = get_token("proof-admin@example.com", "admins")

        print("\n=== Step 3: Run proofs against live ALB ===")
        proof_cmd = [
            sys.executable,
            r"scripts\proofs\run_proofs.py",
            "--base-url", ALB_URL,
            "--admin-token", admin_tok,
            "--agent-token", agent_tok,
            "--user-token", user_tok,
            "--user2-token", user2_tok
        ]
        ret = subprocess.run(proof_cmd)
        print(f"Proofs exited with code: {ret.returncode}")
    finally:
        print("\n=== Step 4: Restore environment configuration ===")
        env_vars["ENABLE_FAULT_INJECTION"] = "false"
        try:
            with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as tf:
                json.dump({"Variables": env_vars}, tf)
                tf_clean_name = tf.name
            try:
                run_aws([
                    "lambda", "update-function-configuration",
                    "--function-name", "momo-dev-saga-steps",
                    "--environment", f"file://{tf_clean_name}"
                ])
            finally:
                if os.path.exists(tf_clean_name):
                    os.remove(tf_clean_name)
            run_aws([
                "ssm", "put-parameter",
                "--name", "/momo/fraud/config",
                "--type", "String",
                "--overwrite",
                "--value", json.dumps({"velocity_max_txns": 10, "max_amount_cents": 500000})
            ])
            print("Restoration complete.")
        except Exception as e:
            print(f"Warning during cleanup: {e}")

if __name__ == "__main__":
    main()
