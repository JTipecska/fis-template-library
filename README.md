# AWS Fault Injection Service Experiments

This repository contains a collection of AWS Fault Injection Service (FIS) experiments designed to test the resilience and fault tolerance of your AWS resources and applications. These experiments simulate various failure scenarios to help you identify potential vulnerabilities and validate your system's ability to recover from disruptions.

## Available Experiments

Browse the experiment directories to find templates for various fault injection scenarios:

- **EC2 Instance Management**: `ec2-instances-terminate/`, `ec2-spot-interruption/`, `ec2-windows-stop-iis/`
- **Database Resilience**: `aurora-cluster-failover/`, `sap-ebs-pause-database-data/`
- **SAP Systems**: `sap-ec2-instance-stop-ascs/`, `sap-ec2-instance-stop-database/`
- **Simple Queue Service (SQS)**: `sqs-queue-impairment/`
- **SageMaker AI Pipeline Resilience**: `sagemaker-ai-pipeline-disruption/sagemaker-stop-pipeline-execution/`, `sagemaker-ai-pipeline-disruption/sagemaker-stop-processing-job/`, `sagemaker-ai-pipeline-disruption/sagemaker-stop-transform-job/`, `sagemaker-ai-pipeline-disruption/sagemaker-stop-training-job/`, `sagemaker-ai-pipeline-disruption/sagemaker-s3-dependency-impairment/`

---

## SageMaker AI Pipeline Resilience Experiments

These experiments validate the resilience of AI/ML batch inference pipelines built on Amazon SageMaker. They cover the full spectrum of failure modes that can affect a pipeline — from stopping a running execution or individual job, to simulating loss of access to the S3 data dependency the pipeline relies on.

See [`sagemaker-ai-pipeline-disruption/README.md`](sagemaker-ai-pipeline-disruption/README.md) for the end-to-end use case, architecture context, and recommended experiment sequencing.

### Experiment Overview

| Experiment | Fault type | Reversible? | Key design note |
|---|---|---|---|
| [`sagemaker-stop-pipeline-execution`](sagemaker-ai-pipeline-disruption/sagemaker-stop-pipeline-execution/) | Stops executing SageMaker pipeline executions | No | Discovers via tagged **pipelines** (not executions — pipeline executions cannot be tagged directly); stops all `Executing` executions found on matching pipelines |
| [`sagemaker-stop-processing-job`](sagemaker-ai-pipeline-disruption/sagemaker-stop-processing-job/) | Stops `InProgress` SageMaker processing jobs | No | Direct pattern match to the training-job experiment; tags applied to the processing job resource itself |
| [`sagemaker-stop-transform-job`](sagemaker-ai-pipeline-disruption/sagemaker-stop-transform-job/) | Stops `InProgress` SageMaker batch transform jobs | No | Direct pattern match to the training-job experiment; validates that downstream Glue jobs and data consumers handle missing or partial inference output |
| [`sagemaker-stop-training-job`](sagemaker-ai-pipeline-disruption/sagemaker-stop-training-job/) | Stops `InProgress` SageMaker training jobs | No | Baseline pattern — all other job-stop experiments follow this same discovery → stop → observe → verify sequence |
| [`sagemaker-s3-dependency-impairment`](sagemaker-ai-pipeline-disruption/sagemaker-s3-dependency-impairment/) | Denies `s3:GetObject`, `s3:PutObject`, `s3:ListBucket`, `s3:DeleteObject` on tagged S3 buckets | **Yes** | Injects a `FISTemporaryDeny` bucket policy statement and always removes it via `onFailure`/`onCancel` handlers; uses `emptyTargetResolutionMode: skip` to prevent failure when no tagged buckets exist |

### Notable differences from the training-job pattern

The `sagemaker-stop-training-job` experiment is the baseline. The other experiments diverge from it in the following ways:

- **`sagemaker-stop-pipeline-execution`**: Cannot tag a pipeline execution directly, so targeting is indirect — the SSM Automation lists pipelines tagged `FIS-Ready=True` and then queries each pipeline's active executions. This means the `FIS-Ready` tag must be on the **pipeline resource**, not the execution.

- **`sagemaker-s3-dependency-impairment`**: The only **reversible** fault in this group. Rather than a one-way stop, it injects a deny bucket policy for a configurable duration and then restores the original policy. The restore step is wired to `onFailure` and `onCancel` to guarantee cleanup even if the experiment is interrupted. This experiment tests a different hypothesis to the job-stop experiments: it validates that SageMaker jobs fail with a clear, observable error when S3 becomes inaccessible, rather than hanging indefinitely.

- **`sagemaker-stop-processing-job`** and **`sagemaker-stop-transform-job`**: Structurally identical to the training-job experiment, but they surface a different observable: whether a parent SageMaker pipeline correctly propagates a step failure and whether downstream consumers (e.g., a Glue job writing results to a data product) detect and alert on partial or missing output from the failed step.

Each experiment directory contains:
- Complete FIS experiment template (JSON)
- Required IAM policies and trust relationships
- Comprehensive README with setup instructions
- Additional automation files where applicable

## Getting Started

To use these experiments, follow these steps:

1. **Prerequisites**: Ensure you have the necessary permissions and IAM roles configured to run FIS experiments in your AWS account.

2. **Choose an Experiment**: Browse the available experiment directories and select one that matches your testing scenario.

3. **Review Documentation**: Read the experiment's README.md file thoroughly to understand prerequisites, expected behavior, and safety considerations.

4. **Configuration**: Customize the template files by replacing placeholder values (e.g., `<YOUR AWS ACCOUNT>`, `<YOUR REGION>`) with your specific AWS account information.

5. **Deploy**: Import the experiment template into your AWS account using the [FIS Template Library Tooling](https://github.com/aws-samples/fis-template-library-tooling).

6. **Execute Safely**: Run the experiment in a non-production environment first, with proper monitoring and stop conditions in place.

7. **Monitor and Analyze**: Observe the impact on your resources and analyze the results to improve your system's resilience.

## Contributing

We welcome contributions of new FIS experiment templates! 

**📋 Before contributing, please read our [Style Guide](STYLE_GUIDE.md) which details all requirements and standards.**

Key requirements for contributions:
- Follow the standardized directory structure and file naming conventions
- Include comprehensive documentation with safety disclaimers
- Provide complete IAM policies following least privilege principles
- Include observability and monitoring recommendations
- Reference the `ec2-windows-stop-iis/` directory as the gold standard example

See [CONTRIBUTING.md](CONTRIBUTING.md) for detailed contribution guidelines.

## Disclaimer

These experiments are designed to simulate failure scenarios in your AWS environment. While precautions have been taken to minimize potential risks, running these experiments may cause temporary disruptions or outages to your resources and applications. It is highly recommended to thoroughly review and test the experiments in a non-production environment before running them in a production setting.
