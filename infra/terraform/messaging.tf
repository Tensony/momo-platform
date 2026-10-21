# Asynchronous path:  saga finalize -> outbox -> SNS (txn-events) -> SQS (fraud-review) -> fraud Lambda
# Poison messages land in the DLQ after 5 receives and raise an alarm.

resource "aws_sns_topic" "events" {
  name              = "${local.name}-txn-events"
  kms_master_key_id = aws_kms_key.data.id
}

resource "aws_sqs_queue" "fraud_dlq" {
  name                      = "${local.name}-fraud-review-dlq"
  kms_master_key_id         = aws_kms_key.data.id
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "fraud_review" {
  name                       = "${local.name}-fraud-review"
  kms_master_key_id          = aws_kms_key.data.id
  visibility_timeout_seconds = 180 # >= 6x the consumer's timeout
  receive_wait_time_seconds  = 20  # long polling: fewer empty receives, lower cost
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.fraud_dlq.arn
    maxReceiveCount     = 5
  })
}

data "aws_iam_policy_document" "fraud_queue" {
  statement {
    sid       = "AllowSnsToSend"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.fraud_review.arn]
    principals {
      type        = "Service"
      identifiers = ["sns.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_sns_topic.events.arn]
    }
  }
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["sqs:*"]
    resources = [aws_sqs_queue.fraud_review.arn]
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

resource "aws_sqs_queue_policy" "fraud_review" {
  queue_url = aws_sqs_queue.fraud_review.id
  policy    = data.aws_iam_policy_document.fraud_queue.json
}

resource "aws_sns_topic_subscription" "fraud_review" {
  topic_arn            = aws_sns_topic.events.arn
  protocol             = "sqs"
  endpoint             = aws_sqs_queue.fraud_review.arn
  raw_message_delivery = true # keeps the correlation_id message attribute on the SQS message
}

resource "aws_sns_topic" "alerts" {
  name              = "${local.name}-alerts"
  kms_master_key_id = aws_kms_key.data.id
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_ssm_parameter" "fraud_config" {
  name        = "/${var.project}/fraud/config"
  type        = "String"
  value       = "{}" # empty => defaults from src/momo/fraud/config.py; edit in place to tune rules
  description = "Runtime fraud rule thresholds (JSON)"
  lifecycle { ignore_changes = [value] }
}
