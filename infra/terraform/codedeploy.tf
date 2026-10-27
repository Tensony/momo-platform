# Blue/green deployment with AUTOMATIC ROLLBACK:
#   1. CodeDeploy starts the new task set behind the test listener (:8443)
#   2. traffic shifts to it in 2 linear steps (canary-style)
#   3. if the deployment fails, OR the 5xx / unhealthy-host alarms fire during the shift, CodeDeploy
#      shifts traffic back to the old task set and stops. No human involved.

data "aws_iam_policy_document" "codedeploy_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codedeploy.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "codedeploy" {
  name               = "${local.name}-codedeploy"
  assume_role_policy = data.aws_iam_policy_document.codedeploy_assume.json
}

resource "aws_iam_role_policy_attachment" "codedeploy" {
  role       = aws_iam_role.codedeploy.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AWSCodeDeployRoleForECS"
}

resource "aws_codedeploy_app" "api" {
  count            = var.enable_codedeploy ? 1 : 0
  name             = "${local.name}-api"
  compute_platform = "ECS"
}

resource "aws_codedeploy_deployment_config" "linear" {
  count                  = var.enable_codedeploy ? 1 : 0
  deployment_config_name = "${local.name}-linear-2step"
  compute_platform       = "ECS"
  traffic_routing_config {
    type = "TimeBasedLinear"
    time_based_linear {
      interval   = 2
      percentage = 50
    }
  }
}

resource "aws_codedeploy_deployment_group" "api" {
  count                  = var.enable_codedeploy ? 1 : 0
  app_name               = aws_codedeploy_app.api[0].name
  deployment_group_name  = "${local.name}-api"
  service_role_arn       = aws_iam_role.codedeploy.arn
  deployment_config_name = aws_codedeploy_deployment_config.linear[0].id

  auto_rollback_configuration {
    enabled = true
    events  = ["DEPLOYMENT_FAILURE", "DEPLOYMENT_STOP_ON_ALARM"]
  }

  alarm_configuration {
    enabled = true
    alarms  = [aws_cloudwatch_metric_alarm.api_5xx.alarm_name, aws_cloudwatch_metric_alarm.unhealthy_hosts.alarm_name]
  }

  blue_green_deployment_config {
    deployment_ready_option { action_on_timeout = "CONTINUE_DEPLOYMENT" }
    terminate_blue_instances_on_deployment_success {
      action                           = "TERMINATE"
      termination_wait_time_in_minutes = 5
    }
  }

  deployment_style {
    deployment_option = "WITH_TRAFFIC_CONTROL"
    deployment_type   = "BLUE_GREEN"
  }

  ecs_service {
    cluster_name = aws_ecs_cluster.main.name
    service_name = aws_ecs_service.api.name
  }

  load_balancer_info {
    target_group_pair_info {
      prod_traffic_route { listener_arns = [aws_lb_listener.https.arn] }
      test_traffic_route { listener_arns = [aws_lb_listener.https_test.arn] }
      target_group { name = aws_lb_target_group.api[0].name }
      target_group { name = aws_lb_target_group.api[1].name }
    }
  }
}
