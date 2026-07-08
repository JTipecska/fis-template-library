# AWS Fault Injection Service Experiment: SageMaker AI Batch Inference Pipeline Resilience

This is an experiment template for use with AWS Fault Injection Service (FIS) and fis-template-library-tooling. This experiment template requires deployment into your AWS account and requires resources in your AWS account to inject faults into.

THIS TEMPLATE WILL INJECT REAL FAULTS! THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE

## Problem Statement

AI/ML batch inference pipelines present unique resilience challenges compared to traditional application stacks. Unlike stateless web services, these pipelines are long-running, time-bounded, produce outputs that downstream systems depend on, and consume managed AWS services (SageMaker, Glue) that do not expose the same fault injection primitives as EC2 or RDS.

When an inference pipeline fails mid-execution — whether due to an AZ impairment, a dependency disruption, or a bad code or data deployment — downstream production systems that are time-triggered may attempt to process incomplete or missing results. Validating how those systems handle this condition is critical to understanding true end-to-end resilience.

## Use Case

This experiment targets an ML batch inference pipeline with the following architecture:

1. **Input**: Data product stored in Amazon S3 (Delta Lake format)
2. **Inference**: Amazon SageMaker Pipeline consisting of:
   - One or more SageMaker Processing Jobs (pre/post-processing)
   - A SageMaker Batch Transform Job (model inference)
3. **Output**: AWS Glue job that reads inference results and writes them back to a downstream data product in S3
4. **Consumers**: Time-triggered downstream production systems that check for the presence of batch job results at a scheduled time and fail with an alert if results are missing or incomplete

**RTO target**: 4 hours  
**RPO target**: Point of failure (re-run from the last successful checkpoint)  
**Monitoring**: CloudWatch alarms integrated with PagerDuty

## Hypothesis

When a fault is injected into the SageMaker pipeline or its dependent jobs during execution, downstream systems will detect the absence or incompleteness of inference results at their scheduled trigger time, raise a CloudWatch alarm, and page on-call within the RTO window. The pipeline can be re-run from scratch to restore the data product within 4 hours.

## Experiment Scenarios

The following failure modes are recommended for testing in order of increasing blast radius. Always begin in a non-production environment.

### Scenario 1: Pipeline Execution Disruption — Stop in Flight

Simulate a mid-execution pipeline failure by stopping the running pipeline execution.

**Mechanism**: SSM Automation runbook executes:
```bash
aws sagemaker stop-pipeline-execution \
  --pipeline-execution-arn <pipeline-execution-arn>
```

**What to observe**:
- Does the SageMaker pipeline transition to `Stopped` status?
- Do downstream jobs (Glue) fail cleanly or leave partial output?
- Does the downstream time-triggered system detect missing results and alert?

---

### Scenario 2: Job Execution Disruption — Stop an Individual Job

Simulate failure of a single job within the pipeline without stopping the whole pipeline.

**Processing Job**:
```bash
aws sagemaker stop-processing-job \
  --processing-job-name <job-name>
```

**Batch Transform Job**:
```bash
aws sagemaker stop-transform-job \
  --transform-job-name <job-name>
```

**Glue Batch Job**:
```bash
aws glue batch-stop-job-run \
  --job-name <job-name> \
  --job-run-ids <run-id>
```

**What to observe**:
- Does stopping a single job cause the SageMaker pipeline to fail?
- Are partial inference outputs written to S3? If so, does the Glue job process them, and how does the downstream system respond to incomplete data?
- Is the failure surfaced through CloudWatch within the expected time window?

---

### Scenario 3: Dependency Impairment — S3 Access Denial

Simulate S3 service unavailability by denying S3 bucket permissions to the SageMaker execution role during a pipeline run.

**Mechanism**: Apply a deny policy to the S3 bucket or the SageMaker IAM role during execution using FIS with an IAM-based action. See the [Securely validate business application resilience with AWS FIS and IAM](https://aws.amazon.com/blogs/security/securely-validate-business-application-resilience-with-aws-fis-and-iam/) blog post for the recommended approach.

**What to observe**:
- Does the pipeline fail with a clear permission-related error?
- Is the error surfaced in CloudWatch Logs and does it trigger an alarm?
- Does the Glue job fail safely when the upstream S3 data product is unavailable?

---

### Scenario 4: Pipeline Starting Disruption — Pre-Execution Impairment

Simulate service unavailability before the pipeline starts by impairing the EventBridge schedule or S3 trigger that launches the pipeline.

**Mechanisms**:
- Disable the EventBridge Scheduler rule that triggers the pipeline
- Apply a deny policy to the S3 source bucket so the pipeline cannot read input data on startup

**What to observe**:
- Does the pipeline fail to start or fail early with a clear error?
- How quickly is the failure detected given that no partial outputs will exist?
- Is the on-call alert triggered within the RTO window?

---

### Scenario 5: Bad Code Deployment

Simulate a bad code push that causes the pipeline to fail during execution.

**Mechanism**: Deploy a Processing Job container or script revision that intentionally raises an exception or returns a non-zero exit code.

**What to observe**:
- Does the SageMaker pipeline propagate the job failure and stop?
- Are error details surfaced in CloudWatch Logs?
- Can the pipeline be re-deployed and re-run within the 4-hour RTO?

---

### Scenario 6: Bad Data Injection

Simulate corrupt or unexpected input data that causes the inference pipeline to fail.

**Mechanism**: Write a malformed or schema-invalid file to the S3 input path before the pipeline starts.

**What to observe**:
- Does the Processing Job or Batch Transform Job fail with a data-related error?
- Is the error distinguishable from infrastructure failures in logs and alarms?
- Is there any risk of partial or incorrect inference results reaching the data product?

---

## Prerequisites

Before running any of these experiments, ensure that:

1. You have the necessary IAM permissions to stop SageMaker pipeline executions, processing jobs, batch transform jobs, and Glue job runs.
2. The IAM role used by FIS has the permissions defined in the accompanying IAM policy document.
3. Target SageMaker pipelines, jobs, and Glue jobs have the `FIS-Ready=True` tag.
4. CloudWatch alarms are configured to monitor pipeline execution status and job failure metrics.
5. Experiments are first validated in a non-production (dev) environment.
6. You have verified the downstream system's scheduled trigger time so you can observe the alert behavior at the expected failure detection window.
7. An incident response runbook exists for restoring the pipeline and re-running it within the RTO.

## Validated Test Environment

A CDK-based test environment that deploys a full SageMaker batch inference pipeline and exercises all five experiments end-to-end is available at [`fis-ssm-ai-pipleine-experiments`](https://gitlab.aws.dev/jenntip/fis-ssm-ai-pipleine-experiments). It includes:

- `stacks/infra_stack.py` — S3 buckets (tagged `FIS-Ready=True`), IAM roles for FIS and SSM Automation, CloudWatch alarms, SNS topic
- `stacks/pipeline_stack.py` — SageMaker pipeline (Processing → Training → Transform steps), SSM Automation documents, FIS experiment templates, EventBridge rule for `Stopped` + `Failed` pipeline execution alerts
- `scripts/run_all_experiments.py` — runner script that starts a pipeline execution, fires each FIS experiment consecutively, and captures per-experiment results

The following cross-cutting deployment lessons were validated through test runs against this environment:

### Tag placement — pipeline vs. execution vs. job

All five experiments use `FIS-Ready=True` as the target filter, but the tag must be applied to different resource types:

- **Experiments 2–4** (processing/training/transform): tag goes on the **job resource itself**. Tags defined in a SageMaker Pipeline step are propagated automatically to the underlying job.
- **Experiment 1** (pipeline execution stop): pipeline executions cannot be tagged. The tag must be on the **pipeline resource**. The SSM automation lists tagged pipelines, then queries their active executions.
- **Experiment 5** (S3 impairment): tag goes on the **S3 buckets** used as input/output.

### S3 IAM — `aws:ResourceTag` does not work for bucket policy operations

`s3:PutBucketPolicy` and `s3:DeleteBucketPolicy` do not populate `aws:ResourceTag` context keys during IAM evaluation. A tag-based condition on these actions always results in `AccessDenied`. Similarly, `s3:GetBucketPolicy` must be unconditional. See the individual README for Experiment 5 for the correct IAM pattern.

### Observability — use EventBridge for pipeline `Stopped` status

The CloudWatch `ExecutionsFailed` metric only fires when a pipeline execution reaches `Failed` status. Experiments that cause a `Stopped` transition (Experiment 1, and indirectly Experiments 2–4 when a job failure causes the parent pipeline to stop) will not trigger a metric alarm. Use an EventBridge rule on `SageMaker Model Building Pipeline Execution Status Change` filtering for `currentPipelineExecutionStatus: ["Stopped", "Failed"]` to cover both cases.

### SSM automation startup latency for S3 impairment

For Experiment 5, the SSM automation runs a discovery step before applying the deny policy. Expect 30–90 seconds between FIS firing and S3 returning `AccessDenied`. Any validation logic should poll with retries rather than using a single fixed-delay check.

## Stop Conditions

The experiment does not have any specific stop conditions defined. It will continue to run until manually stopped or until the targeted stop/deny action has completed. For production testing, attach a CloudWatch alarm stop condition before proceeding.

## Observability and stop conditions

Stop conditions are based on an AWS CloudWatch alarm based on an operational or 
business metric requiring an immediate end of the fault injection. This 
template makes no assumptions about your application and the relevant metrics 
and does not include stop conditions by default.

## Next Steps

As you adapt this scenario to your needs, we recommend:

1. Reviewing the tag names you use to ensure they fit your specific use case.
2. Identifying the CloudWatch metrics most relevant to your pipeline: `SageMaker/ModelBuildingPipeline/ExecutionStatus`, processing job failure counts, Glue job run failure counts, and S3 output object counts.
3. Creating Amazon CloudWatch alarms on these metrics and integrating them with PagerDuty (or your existing alerting path) to validate end-to-end alerting within the RTO window.
4. Adding a stop condition tied to the alarm to automatically halt the experiment if critical thresholds are breached.
5. For the bad-data scenario, ensuring your data validation step produces a distinct error code or metric to distinguish it from infrastructure failures in post-experiment analysis.
6. After each experiment, documenting the actual detection time, recovery time, and any gaps relative to the 4-hour RTO target.

## Import Experiment

You can import the json experiment template into your AWS account via cli or aws cdk. For step by step instructions on how, [click here](https://github.com/aws-samples/fis-template-library-tooling).
