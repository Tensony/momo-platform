import subprocess

import pytest
from scripts import run_live_proofs
from scripts.proofs.run_proofs import Api


def test_live_proof_configuration_requires_selected_region_and_resources(monkeypatch):
    monkeypatch.setattr(run_live_proofs, "REGION", None)
    monkeypatch.setattr(run_live_proofs, "USER_POOL_ID", None)
    monkeypatch.setattr(run_live_proofs, "CLIENT_ID", None)
    monkeypatch.setattr(run_live_proofs, "ALB_URL", None)

    with pytest.raises(SystemExit, match="AWS_REGION"):
        run_live_proofs.validate_config()


def test_proof_api_verifies_tls_by_default_and_only_disables_it_explicitly(monkeypatch):
    import urllib3

    monkeypatch.setattr(urllib3, "disable_warnings", lambda *_args, **_kwargs: None)
    assert Api("https://example.test", "token")._s().verify is True
    assert Api("https://example.test", "token", verify_tls=False)._s().verify is False


def test_aws_cli_errors_do_not_echo_sensitive_arguments(monkeypatch):
    command = []

    def failed_run(args, **kwargs):
        command.extend(args)
        return subprocess.CompletedProcess(args, 1, "", "AWS request failed")

    monkeypatch.setattr(run_live_proofs, "REGION", "af-south-1")
    monkeypatch.setattr(run_live_proofs.subprocess, "run", failed_run)

    with pytest.raises(RuntimeError, match="AWS request failed") as error:
        run_live_proofs.run_aws(["cognito-idp", "initiate-auth", "--auth-parameters", "PASSWORD=secret"])

    assert "--no-verify-ssl" not in command
    assert "secret" not in str(error.value)
