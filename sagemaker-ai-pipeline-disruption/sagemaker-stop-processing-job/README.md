# AWS Fault Injection Service Experiment: SageMaker Processing Job Stop

This is an experiment template for use with AWS Fault Injection Service (FIS) and fis-template-library-tooling. This experiment template requires deployment into your AWS account and requires resources in your AWS account to inject faults into.

THIS TEMPLATE WILL INJECT REAL FAULTS! THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE

## Hypothesis

When a SageMaker processing job is stopped mid-run, a CloudWatch alarm should fire within 5 minutes and the on-call team notified. Any SageMaker pipeline containing this processing step should surface the failure and not continue to subsequent steps. The platform should be able to recover and re-run the failed job or pipeline within the defined RTO.

### What does this enable me to verify?

* Monitoring and alerting correctly detects a processing job failure (were you able to detect there was a problem?)
* Alarms are configured correctly and on-call teams are notified at the right time
* A parent SageMaker pipeline, if any, correctly surfaces the processing step failure and stops execution
* Downstream systems handle missing or partial processing output gracefully
* The team can recover and re-run the processing job or pipeline within the defined Recovery Time Objective (RTO)
* Any automated retry logic at the pipeline or orchestration layer functions as expected

## Prerequisites

Before running this experiment, ensure that:

1. You have the IAM roles created for FIS and SSM Automation to use. Example IAM policy documents and trust policies are provided in this directory.
2. You have created the SSM Automation Document from the sample provided (`sagemaker-stop-processing-job-automation.yaml`).
3. You have created the FIS Experiment Template from the sample provided (`sagemaker-stop-processing-job-template.json`).
4. At least one SageMaker processing job is actively running (`InProgress` status) at the time the experiment is started.
5. The processing job(s) you want to target have the `FIS-Ready=True` tag applied to them.
6. You have appropriate monitoring and observability in place (e.g., CloudWatch alarms, PagerDuty integration) to track the impact of the experiment.
7. You have tested this experiment in a non-production environment before running it against production workloads.

## How it works

This experiment uses an SSM Automation Document invoked by FIS to discover and stop running SageMaker processing jobs. The experiment follows this sequence:

1. **Discovery**: The SSM Automation Document lists all `InProgress` SageMaker processing jobs in the region and filters for those tagged `FIS-Ready=True`. If no matching jobs are found, the automation fails immediately.
2. **Fault injection**: A stop request is sent to each discovered processing job via the `StopProcessingJob` API. This is an irreversible action — the job will transition to `Stopping` then `Stopped` status and will not resume.
3. **Observation window**: The automation waits for a configurable duration (default: 10 minutes) to allow monitoring and any parent pipeline to react to the interruption.
4. **Verification**: The final status of each stopped job is retrieved and logged, confirming the experiment completed as intended.

> **Note**: Stopping a processing job is a one-way operation. There is no automatic restore step. You will need to manually restart any processing jobs or re-trigger parent pipelines as part of your recovery validation.

To confirm the experiment is working, you can monitor processing job status via the AWS CLI:

```bash
aws sagemaker describe-processing-job \
  --processing-job-name <YOUR PROCESSING JOB NAME> \
  --region <YOUR REGION> \
  --query 'ProcessingJobStatus'
```

You should see the status transition from `InProgress` → `Stopping` → `Stopped`.

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
2. Identifying business metrics tied to your processing jobs, such as job success rates or output data freshness.
3. Creating an Amazon CloudWatch metric and Amazon CloudWatch alarm to monitor SageMaker processing job failures.
4. Adding a stop condition tied to the alarm to automatically halt the experiment if critical thresholds are breached.
5. Verifying that any parent SageMaker pipeline correctly surfaces a processing step failure and does not continue to subsequent steps with missing input data.
6. Documenting the observed RTO — from the time the processing job stops to the time it successfully completes a re-run — and comparing it against your target.
7. Documenting the findings from your experiment and updating your incident response runbooks accordingly.

## Deployment Notes

The following considerations were identified through end-to-end testing of this experiment and should be accounted for before running it.

### Tagging — tag the job resource directly

The `FIS-Ready=True` tag must be on the processing job itself. If the job is a step within a SageMaker Pipeline, tags defined in the pipeline definition's `ProcessingStep` arguments are propagated to the underlying job resource and will be picked up correctly.

### Container entrypoint — do not rely on image default

The SSM automation stops the processing job regardless of what the container is doing, but the job must reach `InProgress` status first. If you are using a training-optimised image (e.g., the SageMaker scikit-learn DLC) for a processing job, the image's default `ENTRYPOINT` is typically `train`, which will fail immediately on a processing job and cause it to transition to `Failed` before FIS can stop it. Always set `ContainerEntrypoint` explicitly in `AppSpecification` to point to your processing script:

```json
"AppSpecification": {
  "ImageUri": "<your-image>",
  "ContainerEntrypoint": ["python3", "/opt/ml/processing/input/code/your_script.py"]
}
```

### IAM — tag condition is supported for processing jobs

Unlike S3 bucket policy operations, `sagemaker:StopProcessingJob` and `sagemaker:DescribeProcessingJob` correctly evaluate `aws:ResourceTag` conditions in IAM identity policies. You can scope these actions to `aws:ResourceTag/FIS-Ready: True` safely.

### Test environment

A complete CDK-based test environment for all five experiments in this group — including IAM roles, SageMaker pipeline, SSM documents, and FIS templates — is available at [`fis-ssm-ai-pipleine-experiments`](https://gitlab.aws.dev/jenntip/fis-ssm-ai-pipleine-experiments). It includes a runner script (`scripts/run_all_experiments.py`) that exercises all five experiments consecutively and captures per-experiment results.

## Import Experiment

You can import the json experiment template into your AWS account via cli or aws cdk. For step by step instructions on how, [click here](https://github.com/aws-samples/fis-template-library-tooling).
