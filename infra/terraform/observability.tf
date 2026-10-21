# Golden signals dashboard + alarms with defined response actions (docs/RUNBOOK.md has the playbooks).

locals {
  alb_dim   = aws_lb.api.arn_suffix
  tg_dim    = aws_lb_target_group.api[0].arn_suffix
  sfn_arn   = aws_sfn_state_machine.saga.arn
  alert_arn = aws_sns_topic.alerts.arn
}

# ---------------------------------------------------------------- alarms
resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${local.name}-api-5xx-rate"
  alarm_description   = "SLO burn: more than 2% of requests return 5xx for 3 of 5 minutes. ACTION: runbook 'High error rate' - check saga failures, Aurora health, last deployment. CodeDeploy auto-rolls back if this fires during a deployment."
  comparison_operator = "GreaterThanThreshold"
  threshold           = 2
  evaluation_periods  = 5
  datapoints_to_alarm = 3
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alert_arn]
  ok_actions          = [local.alert_arn]

  metric_query {
    id          = "rate"
    expression  = "IF(requests > 5, 100 * errors / requests, 0)"
    label       = "5xx %"
    return_data = true
  }
  metric_query {
    id = "errors"
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_Target_5XX_Count"
      dimensions  = { LoadBalancer = local.alb_dim }
      period      = 60
      stat        = "Sum"
    }
  }
  metric_query {
    id = "requests"
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "RequestCount"
      dimensions  = { LoadBalancer = local.alb_dim }
      period      = 60
      stat        = "Sum"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "api_latency_p95" {
  alarm_name          = "${local.name}-api-latency-p95"
  alarm_description   = "p95 latency above the 1s SLO for 5 minutes. ACTION: runbook 'High latency' - check Aurora ACU/connections, ECS CPU and scaling activity."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  dimensions          = { LoadBalancer = local.alb_dim }
  extended_statistic  = "p95"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 4
  threshold           = 1
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alert_arn]
}

resource "aws_cloudwatch_metric_alarm" "unhealthy_hosts" {
  alarm_name          = "${local.name}-unhealthy-hosts"
  alarm_description   = "At least one API task is failing health checks. ACTION: runbook 'Unhealthy tasks'. Also stops/rolls back an in-flight deployment."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  dimensions          = { LoadBalancer = local.alb_dim, TargetGroup = local.tg_dim }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alert_arn]
}

resource "aws_cloudwatch_metric_alarm" "fraud_dlq" {
  alarm_name          = "${local.name}-fraud-dlq-not-empty"
  alarm_description   = "Messages failed 5 times and reached the dead-letter queue. ACTION: runbook 'DLQ' - inspect, fix the cause, redrive."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.fraud_dlq.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alert_arn]
}

resource "aws_cloudwatch_metric_alarm" "saga_compensation_failed" {
  alarm_name          = "${local.name}-saga-compensation-failed"
  alarm_description   = "P1: a saga could not compensate (funds parked in ESCROW). ACTION: runbook 'Stuck saga' - run release for the txn_id, then reconcile."
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  dimensions          = { StateMachineArn = local.sfn_arn }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alert_arn]
}

resource "aws_cloudwatch_metric_alarm" "reconciliation" {
  alarm_name          = "${local.name}-reconciliation-discrepancy"
  alarm_description   = "P1: ledger/balance reconciliation found a discrepancy. ACTION: runbook 'Reconciliation failure' - freeze affected wallets, inspect settlement_reports row."
  namespace           = "Momo"
  metric_name         = "ReconciliationDiscrepancies"
  dimensions          = { Service = "reconciliation" }
  statistic           = "Maximum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alert_arn]
}

resource "aws_cloudwatch_metric_alarm" "aurora_cpu" {
  alarm_name          = "${local.name}-aurora-saturation"
  alarm_description   = "Aurora ACU utilisation above 85% for 10 minutes. ACTION: runbook 'Database saturation' - raise aurora_max_acu, check slow queries in Performance Insights."
  namespace           = "AWS/RDS"
  metric_name         = "ACUUtilization"
  dimensions          = { DBClusterIdentifier = aws_rds_cluster.main.cluster_identifier }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alert_arn]
}

resource "aws_cloudwatch_metric_alarm" "unhandled_errors" {
  alarm_name          = "${local.name}-unhandled-exceptions"
  alarm_description   = "Application raised unhandled exceptions. ACTION: search logs by correlation_id (Logs Insights query in RUNBOOK)."
  namespace           = "Momo"
  metric_name         = "UnhandledError"
  dimensions          = { Service = "wallet-api" }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alert_arn]
}

# ---------------------------------------------------------------- dashboard
resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "${local.name}-golden-signals"
  dashboard_body = jsonencode({
    widgets = [
      {
        type       = "text", x = 0, y = 0, width = 24, height = 1
        properties = { markdown = "# Mobile Money - golden signals  |  **Latency** (top-left)  **Traffic** (top-right)  **Errors** (middle)  **Saturation** (bottom)" }
      },
      {
        type = "metric", x = 0, y = 1, width = 12, height = 6
        properties = {
          title = "LATENCY - API response time (p50 / p95 / p99)", region = var.region, view = "timeSeries", period = 60
          metrics = [
            ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", local.alb_dim, { stat = "p50", label = "p50" }],
            ["...", { stat = "p95", label = "p95 (SLO 1s)" }],
            ["...", { stat = "p99", label = "p99" }],
          ]
          annotations = { horizontal = [{ label = "SLO", value = 1 }] }
        }
      },
      {
        type = "metric", x = 12, y = 1, width = 12, height = 6
        properties = {
          title = "TRAFFIC - requests/min and saga executions", region = var.region, view = "timeSeries", period = 60, stat = "Sum"
          metrics = [
            ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", local.alb_dim, { label = "API requests" }],
            ["AWS/States", "ExecutionsStarted", "StateMachineArn", local.sfn_arn, { label = "Saga started" }],
            ["AWS/States", "ExecutionsSucceeded", "StateMachineArn", local.sfn_arn, { label = "Saga succeeded" }],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 7, width = 12, height = 6
        properties = {
          title = "ERRORS - 5xx, 4xx, saga failures", region = var.region, view = "timeSeries", period = 60, stat = "Sum"
          metrics = [
            ["AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "LoadBalancer", local.alb_dim, { label = "5xx", color = "#d62728" }],
            ["AWS/ApplicationELB", "HTTPCode_Target_4XX_Count", "LoadBalancer", local.alb_dim, { label = "4xx" }],
            ["AWS/States", "ExecutionsFailed", "StateMachineArn", local.sfn_arn, { label = "Saga failed (needs human)", color = "#9467bd" }],
            ["Momo", "UnhandledError", "Service", "wallet-api", { label = "Unhandled exceptions" }],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 7, width = 12, height = 6
        properties = {
          title = "BUSINESS - transaction outcomes", region = var.region, view = "timeSeries", period = 60, stat = "Sum"
          metrics = [
            ["Momo", "TransactionRequest", "Outcome", "COMPLETED", { label = "Completed" }],
            ["Momo", "TransactionRequest", "Outcome", "REJECTED", { label = "Rejected (insufficient funds / fraud)" }],
            ["Momo", "TransactionRequest", "Outcome", "FAILED", { label = "Failed + compensated" }],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 13, width = 8, height = 6
        properties = {
          title = "SATURATION - ECS CPU / memory", region = var.region, view = "timeSeries", period = 60, stat = "Average"
          metrics = [
            ["AWS/ECS", "CPUUtilization", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", aws_ecs_service.api.name],
            ["AWS/ECS", "MemoryUtilization", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", aws_ecs_service.api.name],
          ]
        }
      },
      {
        type = "metric", x = 8, y = 13, width = 8, height = 6
        properties = {
          title = "SATURATION - Aurora ACU, connections", region = var.region, view = "timeSeries", period = 60
          metrics = [
            ["AWS/RDS", "ACUUtilization", "DBClusterIdentifier", aws_rds_cluster.main.cluster_identifier, { stat = "Average" }],
            ["AWS/RDS", "DatabaseConnections", "DBClusterIdentifier", aws_rds_cluster.main.cluster_identifier, { stat = "Maximum", yAxis = "right" }],
          ]
        }
      },
      {
        type = "metric", x = 16, y = 13, width = 8, height = 6
        properties = {
          title = "SATURATION - queue depth and running tasks", region = var.region, view = "timeSeries", period = 60, stat = "Maximum"
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.fraud_review.name, { label = "fraud queue" }],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.fraud_dlq.name, { label = "DLQ", color = "#d62728" }],
            ["ECS/ContainerInsights", "RunningTaskCount", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", aws_ecs_service.api.name, { label = "API tasks", yAxis = "right" }],
          ]
        }
      },
      {
        type = "alarm", x = 0, y = 19, width = 24, height = 3
        properties = {
          title = "Alarm status"
          alarms = [
            aws_cloudwatch_metric_alarm.api_5xx.arn, aws_cloudwatch_metric_alarm.api_latency_p95.arn,
            aws_cloudwatch_metric_alarm.unhealthy_hosts.arn, aws_cloudwatch_metric_alarm.fraud_dlq.arn,
            aws_cloudwatch_metric_alarm.saga_compensation_failed.arn, aws_cloudwatch_metric_alarm.reconciliation.arn,
            aws_cloudwatch_metric_alarm.aurora_cpu.arn,
          ]
        }
      },
    ]
  })
}
