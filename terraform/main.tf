# Test cluster for PlatformPilot: small EKS + CloudWatch Container Insights + read-only IAM for the agent.
# NOTE: written for reproducibility; `terraform validate` it before use. Costs real money (EKS control plane + nodes).
terraform {
  required_version = ">= 1.6"
  required_providers { aws = { source = "hashicorp/aws", version = "~> 5.60" } }
}

variable "region"       { default = "us-east-1" }
variable "cluster_name" { default = "platformpilot-test" }

provider "aws" { region = var.region }

data "aws_availability_zones" "az" { state = "available" }

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.13"
  name    = var.cluster_name
  cidr    = "10.20.0.0/16"
  azs     = slice(data.aws_availability_zones.az.names, 0, 2)
  private_subnets = ["10.20.1.0/24", "10.20.2.0/24"]
  public_subnets  = ["10.20.101.0/24", "10.20.102.0/24"]
  enable_nat_gateway = true
  single_nat_gateway = true
}

module "eks" {
  source          = "terraform-aws-modules/eks/aws"
  version         = "~> 20.24"
  cluster_name    = var.cluster_name
  cluster_version = "1.30"
  vpc_id          = module.vpc.vpc_id
  subnet_ids      = module.vpc.private_subnets
  cluster_endpoint_public_access = true
  enable_cluster_creator_admin_permissions = true
  cluster_addons = { amazon-cloudwatch-observability = {} }  # Container Insights metrics
  eks_managed_node_groups = {
    default = { instance_types = ["t3.medium"], min_size = 2, max_size = 3, desired_size = 2 }
  }
}

# Read-only CloudWatch access for the agent's metrics tool.
resource "aws_iam_policy" "agent_ro" {
  name   = "${var.cluster_name}-agent-cloudwatch-ro"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["cloudwatch:GetMetricStatistics", "cloudwatch:GetMetricData", "cloudwatch:ListMetrics"], Resource = "*" }]
  })
}

output "update_kubeconfig" { value = "aws eks update-kubeconfig --name ${var.cluster_name} --region ${var.region}" }
output "agent_policy_arn"  { value = aws_iam_policy.agent_ro.arn }
