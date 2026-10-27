output "api_url" {
  value       = "https://${aws_lb.api.dns_name}"
  description = "Base URL of the wallet API (self-signed certificate unless certificate_arn is set: use curl -k)."
}
output "ecr_repository_url" { value = aws_ecr_repository.api.repository_url }
output "ecs_cluster" { value = aws_ecs_cluster.main.name }
output "ecs_service" { value = aws_ecs_service.api.name }
output "ecs_task_family" { value = aws_ecs_task_definition.api.family }
output "codedeploy_app" { value = var.enable_codedeploy ? aws_codedeploy_app.api[0].name : "disabled" }
output "codedeploy_deployment_group" { value = var.enable_codedeploy ? aws_codedeploy_deployment_group.api[0].deployment_group_name : "disabled" }
output "cognito_user_pool_id" { value = aws_cognito_user_pool.main.id }
output "cognito_client_id" { value = aws_cognito_user_pool_client.api.id }
output "state_machine_arn" { value = aws_sfn_state_machine.saga.arn }
output "dashboard_name" { value = aws_cloudwatch_dashboard.main.dashboard_name }
output "settlement_bucket" { value = aws_s3_bucket.settlement.bucket }
output "region" { value = var.region }
output "db_endpoint" { value = aws_rds_cluster.main.endpoint }
