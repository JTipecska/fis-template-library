# AWS Fault Injection Service Experiment: Lateral Movement (Internal Port Scan)

This is an experiment template for use with AWS Fault Injection Service (FIS) and fis-template-library-tooling. This experiment template requires deployment into your AWS account and requires resources in your AWS account to inject faults into.

THIS TEMPLATE WILL INJECT REAL FAULTS! THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Hypothesis

When an EC2 instance performs an internal TCP port scan against another instance in the same VPC, Amazon GuardDuty detects the reconnaissance behavior within 15 minutes and generates a `Recon:EC2/PortProbeUnprotectedPort` finding with MEDIUM severity against the scanning instance.

## Prerequisites

Before running this experiment, ensure that:

1. You have the necessary permissions to start FIS experiments and send SSM Run Command documents.
2. The IAM role specified in `roleArn` has the permissions defined in `security-detection-lateral-movement-iam-policy.json`.
3. An EC2 instance tagged `FIS-Ready=True` and `Name=fis-security-red1` (the scanner) is running and registered with SSM.
4. An EC2 instance with private IP `10.0.0.11` (Blue1, the scan target) is running in the same VPC.
5. The scanning instance has `nmap` pre-installed (provided by the CDK userdata in this repo).
6. Amazon GuardDuty is enabled in the target region with findings frequency set to 15 minutes.

## How It Works

FIS selects the scanning EC2 instance by tag (`Name=fis-security-red1`) and sends the `fis-security-lateral-movement` SSM Run Command document to it. The document runs `nmap -sT` (TCP connect scan) against the victim IP (`10.0.0.11`). This generates the outbound connection probe pattern that GuardDuty's network traffic monitoring identifies as internal reconnaissance. The finding has MEDIUM severity because port scanning on its own is not conclusive evidence of malicious intent.

## Stop Conditions

The experiment does not have any specific stop conditions defined. It will continue to run until the SSM command completes or until the 5-minute duration elapses.

Stop conditions are based on an AWS CloudWatch alarm based on an operational or
business metric requiring an immediate end of the fault injection. This template
makes no assumptions about your application and the relevant metrics and does not
include stop conditions by default.

## Next Steps

As you adapt this scenario to your needs, we recommend:

1. Reviewing the tag names you use to ensure they fit your specific use case.
2. Identifying business metrics tied to your security posture (e.g., GuardDuty finding rate).
3. Creating an Amazon CloudWatch metric filter on GuardDuty findings published to CloudWatch or EventBridge.
4. Creating an Amazon CloudWatch alarm to fire when MEDIUM or higher severity GuardDuty findings appear.
5. Adding a stop condition tied to that alarm to automatically halt the experiment if critical thresholds are breached.
6. Correlating lateral movement findings with other findings (e.g., a preceding SSH brute force finding) using Amazon Detective to understand attack chains.

## Import Experiment

You can import the json experiment template into your AWS account via cli or aws cdk. For step by step instructions on how, [click here](https://github.com/aws-samples/fis-template-library-tooling).
