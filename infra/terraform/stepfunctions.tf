# The saga orchestrator. EXPRESS + synchronous: the API calls StartSyncExecution and gets the outcome
# in the response. Express is at-least-once, which is safe because every step is idempotent.

resource "aws_cloudwatch_log_group" "saga" {
  name              = "/aws/vendedlogs/states/${local.name}-transfer-saga"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.logs.arn
}

data "aws_iam_policy_document" "sfn_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sfn" {
  name               = "${local.name}-saga-sfn"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
}

data "aws_iam_policy_document" "sfn" {
  statement {
    sid       = "InvokeSagaLambdas"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.saga_steps.arn, aws_lambda_function.fraud.arn]
  }
  statement {
    sid       = "XRay"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
    resources = ["*"]
  }
  statement {
    sid = "DeliverExecutionLogs" # CloudWatch Logs delivery APIs do not support resource-level scoping
    actions = [
      "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }
}
resource "aws_iam_role_policy" "sfn" {
  role   = aws_iam_role.sfn.id
  policy = data.aws_iam_policy_document.sfn.json
}

resource "aws_sfn_state_machine" "saga" {
  name     = "${local.name}-transfer-saga"
  role_arn = aws_iam_role.sfn.arn
  type     = "EXPRESS"
  definition = templatefile("${path.module}/../../statemachine/transfer_saga.asl.json", {
    steps_function_arn = aws_lambda_function.saga_steps.arn
    fraud_function_arn = aws_lambda_function.fraud.arn
  })
  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.saga.arn}:*"
    include_execution_data = false # amounts and wallet ids stay out of the logs
    level                  = "ERROR"
  }
  tracing_configuration { enabled = true }
  depends_on = [aws_iam_role_policy.sfn]
}
