variable "region" {
  type    = string
  default = "af-south-1" # Cape Town: closest to Zambia. Change if a service is unavailable there.
}

variable "project" {
  type    = string
  default = "momo"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "alert_email" {
  type        = string
  description = "Receives alarm notifications (SNS email subscription must be confirmed)."
}

variable "api_image_tag" {
  type        = string
  default     = "latest"
  description = "Tag of the wallet-api image in ECR. CI sets this to the git SHA."
}

variable "api_min_tasks" {
  type    = number
  default = 2 # one per AZ
  validation {
    condition     = var.api_min_tasks >= 2
    error_message = "api_min_tasks must be at least 2 so the service can run across both Availability Zones."
  }
}

variable "api_max_tasks" {
  type    = number
  default = 6
  validation {
    condition     = var.api_max_tasks >= var.api_min_tasks
    error_message = "api_max_tasks must be greater than or equal to api_min_tasks."
  }
}

variable "api_cpu" {
  type    = number
  default = 256
}

variable "api_memory" {
  type    = number
  default = 512
}

variable "aurora_min_acu" {
  type    = number
  default = 0.5
}

variable "aurora_max_acu" {
  type    = number
  default = 4
}

variable "certificate_arn" {
  type        = string
  default     = ""
  description = "ACM certificate for the ALB HTTPS listener. Empty => a self-signed certificate is generated and imported (fine for a demo; use a real one with a domain)."
}

variable "enable_fault_injection" {
  type        = bool
  default     = false
  description = "ONLY true while generating the saga-compensation evidence; lets requests carry X-Fault-Inject."
}

variable "single_nat_gateway" {
  type        = bool
  default     = true
  description = "Cost optimisation: one NAT gateway instead of one per AZ. Set false for production-grade AZ independence of egress."
}

variable "deletion_protection" {
  type    = bool
  default = false # set true outside a coursework environment
}

variable "log_retention_days" {
  type    = number
  default = 14 # cost optimisation: CloudWatch Logs default is "never expire"
}

variable "monthly_budget_usd" {
  type    = number
  default = 100
}

variable "cognito_callback_urls" {
  type    = list(string)
  default = ["https://localhost/callback"]
}

variable "enable_interface_endpoints" {
  type        = bool
  default     = false
  description = "Interface VPC endpoints cost ~USD 0.01/hour per AZ each (about 131 USD/month for the 9 used here), far more than the NAT data they replace at low traffic. Enable at high volume or when policy forbids any internet egress."
}

variable "enable_codedeploy" {
  type        = bool
  default     = false
  description = "Enable CodeDeploy blue/green deployments. Set to false if AWS CodeDeploy is not subscribed on the account."
}
