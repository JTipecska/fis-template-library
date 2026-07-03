# AWS Fault Injection Service Experiment: SageMaker Batch Transform Job Stop

This is an experiment template for use with AWS Fault Injection Service (FIS) and fis-template-library-tooling. This experiment template requires deployment into your AWS account and requires resources in your AWS account to inject faults into.

THIS TEMPLATE WILL INJECT REAL FAULTS! THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Example Hypothesis

When a SageMaker batch transform job is stopped mid-run, a CloudWatch alarm should fire within 5 minutes and the on-call team notified. Downstream systems that depend on the batch inference output — such as Glue jobs writing results to a data product — should detect the missing or incomplete output and raise an alert rather than silently proceeding. The platform should be able to recover and re-run the transform job within the defined RTO.

### What does this enable me to verify?

* Monitoring and alerting correctly detects a batch transform job failure (were you able to detect there was a problem?)
* Alarms are configured correctly and on-call teams are notified at the right time
* Downstream consumers of batch inference results detect partial or missing output and do not silently use incomplete data
* Any parent SageMaker pipeline correctly surfaces the transform step failure and stops execution
* The team can recover and re-run the transform job within the defined Recovery Time Objective (RTO)
* Any automated retry logic at the pipeline or orchestration layer functions as expected

## Prerequisites

Before running this experiment, ensure that:

1. You have the IAM roles created for FIS and SSM Automation to use. Example IAM policy documents and trust policies are provided in this directory.
2. You have created the SSM Automation Document from the sample provided (`sagemaker-stop-transform-job-automation.yaml`).
3. You have created the FIS Experiment Template from the sample provided (`sagemaker-stop-transform-job-experiment-template.json`).
4. At least one SageMaker batch transform job is actively running (`InProgress` status) at the time the experiment is started.
5. The transform job(s) you want to target have the `FIS-Ready=True` tag applied to them.
6. You have appropriate monitoring and observability in place (e.g., CloudWatch alarms, PagerDuty integration) to track the impact of the experiment.
7. You have tested this experiment in a non-production environment before running it against production workloads.

## How it works

This experiment uses an SSM Automation Document invoked by FIS to discover and stop running SageMaker batch transform jobs. The experiment follows this sequence:

1. **Discovery**: The SSM Automation Document lists all `InProgress` SageMaker transform jobs in the region and filters for those tagged `FIS-Ready=True`. If no matching jobs are found, the automation fails immediately.
2. **Fault injection**: A stop request is sent to each discovered transform job via the `StopTransformJob` API. This is an irreversible action — the job will transition to `Stopping` then `Stopped` status and will not resume.
3. **Observation window**: The automation waits for a configurable duration (default: 10 minutes) to allow downstream systems and monitoring to react to the interruption.
4. **Verification**: The final status of each stopped job is retrieved and logged, confirming the experiment completed as intended.

> **Note**: Stopping a transform job is a one-way operation. There is no automatic restore step. You will need to manually restart any transform jobs or re-trigger parent pipelines as part of your recovery validation.

To confirm the experiment is working, you can monitor transform job status via the AWS CLI:

```bash
aws sagemaker describe-transform-job \
  --transform-job-name <YOUR TRANSFORM JOB NAME> \
  --region <YOUR REGION> \
  --query 'TransformJobStatus'
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
2. Identifying business metrics tied to your batch inference pipeline, such as transform job success rates or downstream inference data freshness.
3. Creating an Amazon CloudWatch metric and Amazon CloudWatch alarm to monitor SageMaker transform job failures.
4. Adding a stop condition tied to the alarm to automatically halt the experiment if critical thresholds are breached.
5. Verifying that downstream Glue jobs or data consumers check for the presence and completeness of transform output before processing it.
6. Documenting the observed RTO — from the time the transform job stops to the time it successfully completes a re-run — and comparing it against your target.
7. Documenting the findings from your experiment and updating your incident response runbooks accordingly.

## Import Experiment

You can import the json experiment template into your AWS account via cli or aws cdk. For step by step instructions on how, [click here](https://github.com/aws-samples/fis-template-library-tooling).
