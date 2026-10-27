# wallet-api: stateless containers on ECS Fargate in the PRIVATE app subnets, behind a public ALB.
# Deployments are blue/green through CodeDeploy with automatic rollback (see codedeploy.tf).

resource "aws_ecr_repository" "api" {
  name                 = "${local.name}-wallet-api"
  image_tag_mutability = "MUTABLE"
  force_delete         = !var.deletion_protection
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.data.arn
  }
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 10 images (storage cost optimisation)"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_ecs_cluster" "main" {
  name = "${local.name}-cluster"
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/momo/${var.environment}/wallet-api"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.logs.arn
}

# ---------------------------------------------------------------- roles
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

# Execution role: used by the ECS agent to pull the image and write logs. Never reachable from app code.
resource "aws_iam_role" "api_execution" {
  name               = "${local.name}-api-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "api_execution" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # this action cannot be scoped to a resource
  }
  statement {
    sid       = "PullApiImage"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    resources = [aws_ecr_repository.api.arn]
  }
  statement {
    sid       = "DecryptImageLayers"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.data.arn]
  }
  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.api.arn}:*"]
  }
}
resource "aws_iam_role_policy" "api_execution" {
  role   = aws_iam_role.api_execution.id
  policy = data.aws_iam_policy_document.api_execution.json
}

# Task role: what the running application may do. Exactly three capabilities + tracing.
resource "aws_iam_role" "api_task" {
  name               = "${local.name}-api-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "api_task" {
  statement {
    sid       = "RunSagaSynchronously"
    actions   = ["states:StartSyncExecution"]
    resources = [aws_sfn_state_machine.saga.arn]
  }
  statement {
    sid       = "DbIamAuth"
    actions   = ["rds-db:connect"]
    resources = [local.db_connect_arn]
  }
  statement {
    sid       = "XRay"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
    resources = ["*"]
  }
}
resource "aws_iam_role_policy" "api_task" {
  role   = aws_iam_role.api_task.id
  policy = data.aws_iam_policy_document.api_task.json
}

# ---------------------------------------------------------------- task definition
resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-wallet-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.api_execution.arn
  task_role_arn            = aws_iam_role.api_task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name                   = "api"
      image                  = "${aws_ecr_repository.api.repository_url}:${var.api_image_tag}"
      essential              = true
      portMappings           = [{ containerPort = local.api_port, protocol = "tcp" }]
      readonlyRootFilesystem = true
      linuxParameters        = { initProcessEnabled = true }
      environment = [
        { name = "SERVICE_NAME", value = "wallet-api" },
        { name = "SAGA_MODE", value = "stepfunctions" },
        { name = "STATE_MACHINE_ARN", value = aws_sfn_state_machine.saga.arn },
        { name = "DB_HOST", value = aws_db_instance.main.address },
        { name = "DB_NAME", value = "momo" },
        { name = "DB_USER", value = "momo_app" },
        { name = "DB_POOL_MAX", value = "4" },
        { name = "COGNITO_USER_POOL_ID", value = aws_cognito_user_pool.main.id },
        { name = "COGNITO_CLIENT_ID", value = aws_cognito_user_pool_client.api.id },
        { name = "AWS_REGION", value = var.region },
        { name = "XRAY_ENABLED", value = "true" },
        { name = "ENABLE_FAULT_INJECTION", value = tostring(var.enable_fault_injection) },
        { name = "LOG_LEVEL", value = "INFO" },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.api.name
          awslogs-region        = var.region
          awslogs-stream-prefix = "api"
        }
      }
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:${local.api_port}/health',timeout=3)\""]
        interval    = 15
        timeout     = 5
        retries     = 3
        startPeriod = 20
      }
      dependsOn = [{ containerName = "xray-daemon", condition = "START" }]
    },
    {
      name              = "xray-daemon"
      image             = "public.ecr.aws/xray/aws-xray-daemon:3.3.14"
      essential         = false
      cpu               = 32
      memoryReservation = 64
      portMappings      = [{ containerPort = 2000, protocol = "udp" }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.api.name
          awslogs-region        = var.region
          awslogs-stream-prefix = "xray"
        }
      }
    },
  ])
}

# ---------------------------------------------------------------- load balancer
resource "aws_lb" "api" {
  name                       = "${local.name}-alb"
  load_balancer_type         = "application"
  subnets                    = aws_subnet.public[*].id
  security_groups            = [aws_security_group.alb.id]
  drop_invalid_header_fields = true
  idle_timeout               = 60
  enable_deletion_protection = var.deletion_protection
}

resource "aws_lb_target_group" "api" {
  count                = 2 # blue and green
  name                 = "${local.name}-api-${count.index == 0 ? "blue" : "green"}"
  port                 = local.api_port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.main.id
  deregistration_delay = 30
  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 10
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "tls_private_key" "self_signed" {
  count     = var.certificate_arn == "" ? 1 : 0
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_self_signed_cert" "self_signed" {
  count           = var.certificate_arn == "" ? 1 : 0
  private_key_pem = tls_private_key.self_signed[0].private_key_pem
  subject { common_name = "${local.name}.example.internal" }
  validity_period_hours = 8760
  allowed_uses          = ["key_encipherment", "digital_signature", "server_auth"]
}

resource "aws_acm_certificate" "self_signed" {
  count            = var.certificate_arn == "" ? 1 : 0
  private_key      = tls_private_key.self_signed[0].private_key_pem
  certificate_body = tls_self_signed_cert.self_signed[0].cert_pem
}

locals {
  certificate_arn = var.certificate_arn != "" ? var.certificate_arn : aws_acm_certificate.self_signed[0].arn
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = local.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api[0].arn
  }
  lifecycle { ignore_changes = [default_action] } # CodeDeploy swaps blue/green
}

resource "aws_lb_listener" "https_test" {
  load_balancer_arn = aws_lb.api.arn
  port              = 8443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = local.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api[1].arn
  }
  lifecycle { ignore_changes = [default_action] }
}

# ---------------------------------------------------------------- service + auto scaling
resource "aws_ecs_service" "api" {
  name                              = "${local.name}-wallet-api"
  cluster                           = aws_ecs_cluster.main.id
  task_definition                   = aws_ecs_task_definition.api.arn
  desired_count                     = var.api_min_tasks
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 30
  enable_execute_command            = false

  deployment_controller { type = var.enable_codedeploy ? "CODE_DEPLOY" : "ECS" }

  network_configuration {
    subnets          = aws_subnet.app[*].id # PRIVATE subnets; spread across both AZs
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api[0].arn
    container_name   = "api"
    container_port   = local.api_port
  }

  lifecycle {
    ignore_changes = [task_definition, load_balancer, desired_count] # owned by CodeDeploy / autoscaling
  }
  depends_on = [aws_lb_listener.https, aws_lambda_invocation.db_init]
}

resource "aws_appautoscaling_target" "api" {
  service_namespace  = "ecs"
  scalable_dimension = "ecs:service:DesiredCount"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.api.name}"
  min_capacity       = var.api_min_tasks
  max_capacity       = var.api_max_tasks
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${local.name}-api-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  resource_id        = aws_appautoscaling_target.api.resource_id
  target_tracking_scaling_policy_configuration {
    target_value       = 55
    scale_out_cooldown = 60
    scale_in_cooldown  = 300
    predefined_metric_specification { predefined_metric_type = "ECSServiceAverageCPUUtilization" }
  }
}

resource "aws_appautoscaling_policy" "api_requests" {
  name               = "${local.name}-api-requests"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  resource_id        = aws_appautoscaling_target.api.resource_id
  target_tracking_scaling_policy_configuration {
    target_value       = 300 # requests per target per minute
    scale_out_cooldown = 60
    scale_in_cooldown  = 300
    predefined_metric_specification {
      predefined_metric_type = "ALBRequestCountPerTarget"
      resource_label         = "${aws_lb.api.arn_suffix}/${aws_lb_target_group.api[0].arn_suffix}"
    }
  }
}
