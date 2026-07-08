# AWS Fault Injection Service Experiment: SageMaker S3 Dependency Impairment

This is an experiment template for use with AWS Fault Injection Service (FIS) and fis-template-library-tooling. This experiment template requires deployment into your AWS account and requires resources in your AWS account to inject faults into.

THIS TEMPLATE WILL INJECT REAL FAULTS! THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Hypothesis

When the S3 buckets used by a SageMaker pipeline become unavailable mid-execution, a CloudWatch alarm should fire within 5 minutes and the on-call team notified. The pipeline should fail gracefully with a clear error rather than hanging indefinitely. Once S3 access is restored, the platform should be able to re-run the pipeline successfully within the defined RTO.

### What does this enable me to verify?

* Monitoring and alerting correctly detects S3 access failures during pipeline or job execution (were you able to detect there was a problem?)
* Alarms are configured correctly and on-call teams are notified at the right time
* SageMaker jobs fail with meaningful error messages when S3 input data or output paths become inaccessible
* The pipeline does not hang indefinitely waiting for S3 — it surfaces the error within a reasonable time
* S3 access is fully restored after the experiment and no residual deny policy remains
* The team can recover and re-run the pipeline successfully within the defined Recovery Time Objective (RTO)

## Prerequisites

Before running this experiment, ensure that:

1. You have the IAM roles created for FIS and SSM Automation to use. Example IAM policy documents and trust policies are provided in this directory.
2. You have created the SSM Automation Document from the sample provided (`sagemaker-s3-dependency-impairment-automation.yaml`).
3. You have created the FIS Experiment Template from the sample provided (`sagemaker-s3-dependency-impairment-experiment-template.json`).
4. The S3 bucket(s) used as input data sources or output destinations by your SageMaker pipeline or jobs have the `FIS-Ready=True` tag applied to them.
5. You have appropriate monitoring and observability in place (e.g., CloudWatch alarms, PagerDuty integration) to track the impact of the experiment.
6. You have tested this experiment in a non-production environment before running it against production workloads.
7. You understand that the deny policy blocks access to all principals, including your own IAM user — do not attempt to access targeted buckets directly during the impairment window.

## How it works

This experiment uses an SSM Automation Document invoked by FIS to apply a temporary deny policy to tagged S3 buckets, simulating S3 service unavailability for a SageMaker pipeline or job. Unlike the job-stop experiments, this fault is **reversible** — access is automatically restored after the impairment window. The experiment follows this sequence:

1. **Discovery**: The SSM Automation Document lists all S3 buckets and filters for those tagged `FIS-Ready=True`.
2. **Fault injection**: A deny statement (`Sid: FISTemporaryDeny`) is injected into each target bucket's policy, blocking `s3:GetObject`, `s3:PutObject`, `s3:ListBucket`, and `s3:DeleteObject` for all principals. If a bucket has no existing policy, a new one is created. Any pre-existing `FISTemporaryDeny` statement is removed first to ensure idempotency.
3. **Observation window**: The automation waits for a configurable duration (default: 10 minutes) while S3 access is denied, allowing running SageMaker jobs to encounter and surface the failure.
4. **Restore**: The `FISTemporaryDeny` statement is removed from each bucket policy. If the policy is now empty, the policy is deleted entirely. S3 access returns to its pre-experiment state.

> **Note**: The `onFailure` and `onCancel` handlers on both the fault injection and wait steps point to the restore step, ensuring S3 access is always restored even if the experiment is cancelled or fails unexpectedly.

To verify the impairment is active, you can attempt an S3 operation on a targeted bucket during the impairment window:

```bash
aws s3 ls s3://<YOUR BUCKET NAME>/ --region <YOUR REGION>
```

During the impairment window you should receive an `AccessDenied` error. After the impairment window, the command should succeed normally.

## Stop Conditions

The experiment does not have any specific stop conditions defined. It will continue to run until all actions are completed or until manually stopped.

## Observability and stop conditions

Stop conditions are based on an AWS CloudWatch alarm based on an operational or 
business metric requiring an immediate end of the fault injection. This 
template makes no assumptions about your application and the relevant metrics 
and does not include stop conditions by default.

## Next Steps

As you adapt this scenario to your needs, we recommend:

1. Reviewing the tag names you use to ensure they fit your specific use case — tag only the specific buckets used by your SageMaker pipeline (input data, output results) rather than all S3 buckets.
2. Identifying business metrics tied to your pipeline's S3 dependency, such as SageMaker job failure rates or data product freshness.
3. Creating an Amazon CloudWatch metric and Amazon CloudWatch alarm to monitor for S3 access errors during SageMaker job execution.
4. Adding a stop condition tied to the alarm to automatically halt the experiment if critical thresholds are breached.
5. Verifying that the deny policy is fully removed after the experiment by checking the bucket policy via the AWS Console or CLI.
6. Testing that your SageMaker jobs surface a clear error within a reasonable time when S3 becomes inaccessible, rather than hanging or timing out silently.
7. Documenting the findings from your experiment and updating your incident response runbooks accordingly.

## Deployment Notes

The following considerations were identified through end-to-end testing of this experiment and should be accounted for before running it.

### IAM — S3 bucket policy operations do not support `aws:ResourceTag` conditions

`s3:PutBucketPolicy` and `s3:DeleteBucketPolicy` are S3 control-plane operations that do not populate `aws:ResourceTag` context keys in IAM policy evaluation. A tag condition on these actions will always result in `AccessDenied`, regardless of whether the bucket is tagged. **Do not use tag conditions on these actions.** Instead, scope them to explicit bucket ARNs:

```json
{
  "Effect": "Allow",
  "Action": ["s3:PutBucketPolicy", "s3:DeleteBucketPolicy"],
  "Resource": [
    "arn:aws:s3:::my-input-bucket",
    "arn:aws:s3:::my-output-bucket"
  ]
}
```

Similarly, `s3:GetBucketPolicy` must be unconditional — it is evaluated before the current bucket policy can be read, so a tag condition will also deny it.

### IAM — `s3:GetBucketTagging` is required for discovery

The discovery step calls `s3:GetBucketTagging` to filter buckets by the `FIS-Ready=True` tag. This action requires `"Resource": "*"` as S3 does not support resource-level permissions for tagging reads.

### Timing — deny policy applies 30–90 seconds after FIS fires

The SSM automation runs a discovery step before applying the deny policy. Expect 30–90 seconds between the FIS experiment starting and S3 returning `AccessDenied`. If you are observing the impairment programmatically, poll in a loop rather than using a single fixed-delay check — a single probe taken at 15 seconds will typically fire before the policy is applied.

### Scope — the deny blocks all principals, including your own IAM identity

The `FISTemporaryDeny` bucket policy statement uses `"Principal": "*"`, which blocks all IAM principals including your own user or role. Do not attempt to access targeted buckets from the console or CLI during the impairment window.

### Restore — always runs even on failure or cancellation

The `removeDenyPolicy` step is wired as the `onFailure` and `onCancel` target for both the apply and wait steps. If the experiment is cancelled mid-run or the apply step fails for any reason, the restore step still executes. Verify the bucket policy was fully cleaned up after the experiment:

```bash
aws s3api get-bucket-policy --bucket <YOUR BUCKET NAME> --region <YOUR REGION>
```

The response should contain no `FISTemporaryDeny` statement. If the bucket had no policy before the experiment, the policy should be absent entirely.

### Test environment

A complete CDK-based test environment for all five experiments in this group — including IAM roles, SageMaker pipeline, SSM documents, and FIS templates — is available at [`fis-ssm-ai-pipleine-experiments`](https://gitlab.aws.dev/jenntip/fis-ssm-ai-pipleine-experiments). It includes a runner script (`scripts/run_all_experiments.py`) that exercises all five experiments consecutively and captures per-experiment results.

## Import Experiment

You can import the json experiment template into your AWS account via cli or aws cdk. For step by step instructions on how, [click here](https://github.com/aws-samples/fis-template-library-tooling).
