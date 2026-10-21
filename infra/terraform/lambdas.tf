# Four independently deployable Lambda services share one dependency layer (built by scripts/build_lambdas.sh):
#   saga-steps       - one saga step per invocation (called by Step Functions)
#   fraud-engine     - sync decision (Step Functions) + async review (SQS)
#   reconciliation   - scheduled integrity checks, settlement reports, outbox sweep
#   db-init          - applies db/schema.sql once per schema change

locals {
  lambda_common_env = {
    DB_HOST                = aws_rds_cluster.main.endpoint
    DB_NAME                = "momo"
    DB_USER                = "momo_app"
    DB_POOL_MAX            = "2"
    XRAY_ENABLED           = "true"
    LOG_LEVEL              = "INFO"
    ENABLE_FAULT_INJECTION = tostring(var.enable_fault_injection)
  }
  db_connect_arn = "arn:${data.aws_partition.current.partition}:rds-db:${var.region}:${local.account_id}:dbuser:${aws_rds_cluster.main.cluster_resource_id}/momo_app"
}

resource "aws_lambda_layer_version" "deps" {
  layer_name          = "${local.name}-python-deps"
  filename            = "${local.build_dir}/deps.zip"
  source_code_hash    = filebase64sha256("${local.build_dir}/deps.zip")
  compatible_runtimes = ["python3.12"]
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# ---------------------------------------------------------------- log groups
resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = toset(["saga-steps", "fraud-engine", "reconciliation", "db-init"])
  name              = "/aws/lambda/${local.name}-${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.logs.arn
}

# ---------------------------------------------------------------- saga-steps
resource "aws_iam_role" "saga_steps" {
  name               = "${local.name}-saga-steps"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "saga_steps" {
  statement {
    sid       = "DbIamAuth"
    actions   = ["rds-db:connect"]
    resources = [local.db_connect_arn]
  }
  statement {
    sid       = "PublishCompletedEvents"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.events.arn]
  }
  statement {
    sid       = "UseDataKeyForSns"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.data.arn]
  }
  statement {
    sid       = "XRay" # X-Ray APIs do not support resource-level permissions
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda["saga-steps"].arn}:*"]
  }
  statement {
    sid       = "VpcNetworkInterfaces" # required for any VPC-attached Lambda; EC2 ENI APIs have no resource scoping
    actions   = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface"]
    resources = ["*"]
  }
}
resource "aws_iam_role_policy" "saga_steps" {
  role   = aws_iam_role.saga_steps.id
  policy = data.aws_iam_policy_document.saga_steps.json
}

resource "aws_lambda_function" "saga_steps" {
  function_name    = "${local.name}-saga-steps"
  role             = aws_iam_role.saga_steps.arn
  runtime          = "python3.12"
  handler          = "momo.saga.handlers.handler"
  filename         = "${local.build_dir}/saga-steps.zip"
  source_code_hash = filebase64sha256("${local.build_dir}/saga-steps.zip")
  layers           = [aws_lambda_layer_version.deps.arn]
  timeout          = 20
  memory_size      = 256
  tracing_config { mode = "Active" }
  vpc_config {
    subnet_ids         = aws_subnet.app[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }
  environment {
    variables = merge(local.lambda_common_env, {
      SERVICE_NAME    = "saga-steps"
      EVENT_TOPIC_ARN = aws_sns_topic.events.arn
    })
  }
  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.saga_steps]
}

# ---------------------------------------------------------------- fraud-engine
resource "aws_iam_role" "fraud" {
  name               = "${local.name}-fraud-engine"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "fraud" {
  statement {
    sid       = "DbIamAuth"
    actions   = ["rds-db:connect"]
    resources = [local.db_connect_arn]
  }
  statement {
    sid       = "ReadFraudConfig"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.fraud_config.arn]
  }
  statement {
    sid       = "ConsumeReviewQueue"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]
    resources = [aws_sqs_queue.fraud_review.arn]
  }
  statement {
    sid       = "DecryptQueue"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.data.arn]
  }
  statement {
    sid       = "XRay"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda["fraud-engine"].arn}:*"]
  }
  statement {
    sid       = "VpcNetworkInterfaces"
    actions   = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface"]
    resources = ["*"]
  }
}
resource "aws_iam_role_policy" "fraud" {
  role   = aws_iam_role.fraud.id
  policy = data.aws_iam_policy_document.fraud.json
}

resource "aws_lambda_function" "fraud" {
  function_name    = "${local.name}-fraud-engine"
  role             = aws_iam_role.fraud.arn
  runtime          = "python3.12"
  handler          = "momo.fraud.handler.handler"
  filename         = "${local.build_dir}/fraud-engine.zip"
  source_code_hash = filebase64sha256("${local.build_dir}/fraud-engine.zip")
  layers           = [aws_lambda_layer_version.deps.arn]
  timeout          = 25
  memory_size      = 256
  tracing_config { mode = "Active" }
  vpc_config {
    subnet_ids         = aws_subnet.app[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }
  environment {
    variables = merge(local.lambda_common_env, {
      SERVICE_NAME       = "fraud-engine"
      FRAUD_CONFIG_PARAM = aws_ssm_parameter.fraud_config.name
    })
  }
  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.fraud]
}

resource "aws_lambda_event_source_mapping" "fraud_review" {
  event_source_arn                   = aws_sqs_queue.fraud_review.arn
  function_name                      = aws_lambda_function.fraud.arn
  batch_size                         = 10
  maximum_batching_window_in_seconds = 5
  function_response_types            = ["ReportBatchItemFailures"]
  scaling_config { maximum_concurrency = 5 } # protects Aurora connections
}

# ---------------------------------------------------------------- reconciliation
resource "aws_s3_bucket" "settlement" {
  bucket_prefix = "${local.name}-settlement-"
  force_destroy = !var.deletion_protection
}

resource "aws_s3_bucket_public_access_block" "settlement" {
  bucket                  = aws_s3_bucket.settlement.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "settlement" {
  bucket = aws_s3_bucket.settlement.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true # fewer KMS calls: cost optimisation
  }
}

resource "aws_s3_bucket_versioning" "settlement" {
  bucket = aws_s3_bucket.settlement.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "settlement" {
  bucket = aws_s3_bucket.settlement.id
  rule {
    id     = "tier-and-expire"
    status = "Enabled"
    filter {}
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    expiration { days = 365 }
    noncurrent_version_expiration { noncurrent_days = 30 }
  }
}

data "aws_iam_policy_document" "settlement_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.settlement.arn, "${aws_s3_bucket.settlement.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}
resource "aws_s3_bucket_policy" "settlement" {
  bucket = aws_s3_bucket.settlement.id
  policy = data.aws_iam_policy_document.settlement_bucket.json
}

resource "aws_iam_role" "reconciliation" {
  name               = "${local.name}-reconciliation"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "reconciliation" {
  statement {
    sid       = "DbIamAuth"
    actions   = ["rds-db:connect"]
    resources = [local.db_connect_arn]
  }
  statement {
    sid       = "WriteSettlementReports"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.settlement.arn}/settlement/*"]
  }
  statement {
    sid       = "PublishOutboxEvents"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.events.arn]
  }
  statement {
    sid       = "UseDataKey"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.data.arn]
  }
  statement {
    sid       = "XRay"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda["reconciliation"].arn}:*"]
  }
  statement {
    sid       = "VpcNetworkInterfaces"
    actions   = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface"]
    resources = ["*"]
  }
}
resource "aws_iam_role_policy" "reconciliation" {
  role   = aws_iam_role.reconciliation.id
  policy = data.aws_iam_policy_document.reconciliation.json
}

resource "aws_lambda_function" "reconciliation" {
  function_name    = "${local.name}-reconciliation"
  role             = aws_iam_role.reconciliation.arn
  runtime          = "python3.12"
  handler          = "momo.reconciliation.handler.handler"
  filename         = "${local.build_dir}/reconciliation.zip"
  source_code_hash = filebase64sha256("${local.build_dir}/reconciliation.zip")
  layers           = [aws_lambda_layer_version.deps.arn]
  timeout          = 300
  memory_size      = 512
  tracing_config { mode = "Active" }
  vpc_config {
    subnet_ids         = aws_subnet.app[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }
  environment {
    variables = merge(local.lambda_common_env, {
      SERVICE_NAME      = "reconciliation"
      SETTLEMENT_BUCKET = aws_s3_bucket.settlement.bucket
      EVENT_TOPIC_ARN   = aws_sns_topic.events.arn
    })
  }
  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.reconciliation]
}

resource "aws_cloudwatch_event_rule" "reconcile_daily" {
  name                = "${local.name}-reconcile-daily"
  schedule_expression = "cron(30 0 * * ? *)" # 00:30 UTC: settle the previous day
}
resource "aws_cloudwatch_event_target" "reconcile_daily" {
  rule  = aws_cloudwatch_event_rule.reconcile_daily.name
  arn   = aws_lambda_function.reconciliation.arn
  input = jsonencode({ task = "reconcile" })
}

resource "aws_cloudwatch_event_rule" "reconcile_hourly" {
  name                = "${local.name}-integrity-hourly"
  schedule_expression = "rate(1 hour)"
}
resource "aws_cloudwatch_event_target" "reconcile_hourly" {
  rule  = aws_cloudwatch_event_rule.reconcile_hourly.name
  arn   = aws_lambda_function.reconciliation.arn
  input = jsonencode({ task = "reconcile" })
}

resource "aws_cloudwatch_event_rule" "outbox_sweep" {
  name                = "${local.name}-outbox-sweep"
  schedule_expression = "rate(5 minutes)"
}
resource "aws_cloudwatch_event_target" "outbox_sweep" {
  rule  = aws_cloudwatch_event_rule.outbox_sweep.name
  arn   = aws_lambda_function.reconciliation.arn
  input = jsonencode({ task = "outbox_sweep" })
}

resource "aws_lambda_permission" "events" {
  for_each = {
    daily  = aws_cloudwatch_event_rule.reconcile_daily.arn
    hourly = aws_cloudwatch_event_rule.reconcile_hourly.arn
    sweep  = aws_cloudwatch_event_rule.outbox_sweep.arn
  }
  statement_id  = "AllowEventBridge-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reconciliation.function_name
  principal     = "events.amazonaws.com"
  source_arn    = each.value
}

# ---------------------------------------------------------------- db-init (schema as code)
resource "aws_iam_role" "db_init" {
  name               = "${local.name}-db-init"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "db_init" {
  statement {
    sid       = "ReadMasterSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_rds_cluster.main.master_user_secret[0].secret_arn]
  }
  statement {
    sid       = "DecryptMasterSecret"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.data.arn]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda["db-init"].arn}:*"]
  }
  statement {
    sid       = "VpcNetworkInterfaces"
    actions   = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface"]
    resources = ["*"]
  }
}
resource "aws_iam_role_policy" "db_init" {
  role   = aws_iam_role.db_init.id
  policy = data.aws_iam_policy_document.db_init.json
}

resource "aws_lambda_function" "db_init" {
  function_name    = "${local.name}-db-init"
  role             = aws_iam_role.db_init.arn
  runtime          = "python3.12"
  handler          = "momo.db_init.handler"
  filename         = "${local.build_dir}/db-init.zip"
  source_code_hash = filebase64sha256("${local.build_dir}/db-init.zip")
  layers           = [aws_lambda_layer_version.deps.arn]
  timeout          = 120
  memory_size      = 256
  vpc_config {
    subnet_ids         = aws_subnet.app[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }
  environment {
    variables = {
      DB_HOST              = aws_rds_cluster.main.endpoint
      DB_NAME              = "momo"
      DB_MASTER_SECRET_ARN = aws_rds_cluster.main.master_user_secret[0].secret_arn # an ARN, not a secret
      SCHEMA_PATH          = "/var/task/schema.sql"
    }
  }
  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.db_init]
}

resource "aws_lambda_invocation" "db_init" {
  function_name = aws_lambda_function.db_init.function_name
  input         = jsonencode({ schema_sha = filesha256("${path.module}/../../db/schema.sql") })
  depends_on    = [aws_rds_cluster_instance.main, aws_vpc_endpoint.interface]
}
