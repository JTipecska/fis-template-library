# AWS Fault Injection Service Experiment: SageMaker Pipeline Execution Stop

This is an experiment template for use with AWS Fault Injection Service (FIS) and fis-template-library-tooling. This experiment template requires deployment into your AWS account and requires resources in your AWS account to inject faults into.

THIS TEMPLATE WILL INJECT REAL FAULTS! THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE

## Hypothesis

When a SageMaker pipeline execution is stopped mid-run, a CloudWatch alarm should fire within 5 minutes and the on-call team notified. Downstream systems that depend on the pipeline's output — such as Glue jobs or data products — should detect the missing results and raise an alert rather than silently proceeding. The platform should be able to recover and re-run the pipeline within the defined RTO.

### What does this enable me to verify?

* Monitoring and alerting correctly detects a pipeline execution failure (were you able to detect there was a problem?)
* Alarms are configured correctly and on-call teams are notified at the right time
* Downstream time-based triggers handle missing pipeline output gracefully and do not silently proceed
* Steps within the pipeline that were in progress fail cleanly and do not leave partial output in S3
* The team can recover and re-run the pipeline within the defined Recovery Time Objective (RTO)
* Any automated retry or re-execution logic functions as expected

## Prerequisites

Before running this experiment, ensure that:

1. You have the IAM roles created for FIS and SSM Automation to use. Example IAM policy documents and trust policies are provided in this directory.
2. You have created the SSM Automation Document from the sample provided (`sagemaker-stop-pipeline-execution-automation.yaml`).
3. You have created the FIS Experiment Template from the sample provided (`sagemaker-stop-pipeline-execution-template.json`).
4. At least one SageMaker pipeline execution is actively running (`Executing` status) at the time the experiment is started.
5. The SageMaker pipeline(s) whose executions you want to target have the `FIS-Ready=True` tag applied to the **pipeline** (not the execution — pipeline executions do not have tags directly).
6. You have appropriate monitoring and observability in place (e.g., CloudWatch alarms, PagerDuty integration) to track the impact of the experiment.
7. You have tested this experiment in a non-production environment before running it against production workloads.

## How it works

This experiment uses an SSM Automation Document invoked by FIS to discover and stop running SageMaker pipeline executions. The experiment follows this sequence:

1. **Discovery**: The SSM Automation Document lists all SageMaker pipelines in the region and filters for those tagged `FIS-Ready=True`. For each matching pipeline, it lists all executions with status `Executing`. If no matching executions are found, the automation fails immediately.
2. **Fault injection**: A stop request is sent to each discovered pipeline execution via the `StopPipelineExecution` API. This is an irreversible action — any steps currently running will be cancelled and the execution will transition to `Stopping` then `Stopped`.
3. **Observation window**: The automation waits for a configurable duration (default: 10 minutes) to allow downstream systems and monitoring to react to the interruption.
4. **Verification**: The final status of each stopped execution is retrieved and logged, confirming the experiment completed as intended.

> **Note**: Stopping a pipeline execution is a one-way operation. There is no automatic restore step. You will need to manually re-trigger any pipeline executions as part of your recovery validation.

To confirm the experiment is working, you can monitor pipeline execution status via the AWS CLI:

```bash
aws sagemaker describe-pipeline-execution \
  --pipeline-execution-arn <YOUR PIPELINE EXECUTION ARN> \
  --region <YOUR REGION> \
  --query 'PipelineExecutionStatus'
```

You should see the status transition from `Executing` → `Stopping` → `Stopped`.

## Stop Conditions

The experiment does not have any specific stop conditions defined. It will continue to run until all actions are completed or until manually stopped.

## Observability and stop conditions

Stop conditions are based on an AWS CloudWatch alarm based on an operational or 
business metric requiring an immediate end of the fault injection. This 
template makes no assumptions about your application and the relevant metrics 
and does not include stop conditions by default.

## Next Steps

As you adapt this scenario to your needs, we recommend:

1. Reviewing the tag names you use to ensure they fit your specific use case.
2. Identifying business metrics tied to your pipeline, such as execution success rates or downstream data freshness.
3. Creating an Amazon EventBridge rule on `SageMaker Model Building Pipeline Execution Status Change` events filtering for `currentPipelineExecutionStatus: ["Stopped", "Failed"]` to reliably detect both outcomes — the CloudWatch `ExecutionsFailed` metric only fires for `Failed` status and will not trigger when this experiment stops an execution.
4. Adding a stop condition tied to a CloudWatch alarm to automatically halt the experiment if critical thresholds are breached.
5. Verifying that downstream time-based jobs correctly detect missing pipeline output and raise an alert rather than running against stale data.
6. Documenting the observed RTO — from the time the pipeline stops to the time it successfully completes a re-run — and comparing it against your target.
7. Documenting the findings from your experiment and updating your incident response runbooks accordingly.

## Deployment Notes

The following considerations were identified through end-to-end testing of this experiment and should be accounted for before running it.

### Tagging — pipeline resource, not execution

Pipeline executions cannot be tagged directly. The `FIS-Ready=True` tag must be applied to the **pipeline resource itself**. The SSM automation lists pipelines by tag and then queries each pipeline's active executions — it does not filter executions by tag.

### IAM — `sagemaker:StopPipelineExecution` resource ARN format

The SSM automation role requires `sagemaker:StopPipelineExecution` and `sagemaker:DescribePipelineExecution` on execution ARNs (`arn:aws:sagemaker:<region>:<account>:pipeline/*/execution/*`), and `sagemaker:ListPipelines`, `sagemaker:ListPipelineExecutions`, and `sagemaker:ListTags` on `*`. These cannot be scoped by tag condition because the list/tag operations require a wildcard resource.

### CloudWatch alarm — `ExecutionsFailed` metric does not fire for `Stopped` executions

The `ExecutionsFailed` metric in the `AWS/SageMaker/ModelBuildingPipeline` namespace only emits when a pipeline execution transitions to `Failed` status. A pipeline execution stopped via this experiment transitions to `Stopped`, which does not emit `ExecutionsFailed`. To observe this experiment through CloudWatch, use an **EventBridge rule** on `SageMaker Model Building Pipeline Execution Status Change` events filtering for `currentPipelineExecutionStatus: ["Stopped", "Failed"]`, rather than a metric alarm.

### Test environment

A complete CDK-based test environment for all five experiments in this group — including IAM roles, SageMaker pipeline, SSM documents, and FIS templates — is available at [`fis-ssm-ai-pipleine-experiments`](https://gitlab.aws.dev/jenntip/fis-ssm-ai-pipleine-experiments). It includes a runner script (`scripts/run_all_experiments.py`) that exercises all five experiments consecutively and captures per-experiment results.

## Import Experiment

You can import the json experiment template into your AWS account via cli or aws cdk. For step by step instructions on how, [click here](https://github.com/aws-samples/fis-template-library-tooling).
