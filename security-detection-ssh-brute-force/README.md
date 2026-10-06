# AWS Fault Injection Service Experiment: SSH Brute Force Attack

This is an experiment template for use with AWS Fault Injection Service (FIS) and fis-template-library-tooling. This experiment template requires deployment into your AWS account and requires resources in your AWS account to inject faults into.

THIS TEMPLATE WILL INJECT REAL FAULTS! THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Hypothesis

When an SSH brute force attack is launched from one EC2 instance against another within the same VPC, Amazon GuardDuty detects the attack pattern within 15 minutes and generates an `UnauthorizedAccess:EC2/SSHBruteForce` finding with HIGH severity against the attacker instance.

## Prerequisites

Before running this experiment, ensure that:

1. You have the necessary permissions to start FIS experiments and send SSM Run Command documents.
2. The IAM role specified in `roleArn` has the permissions defined in `security-detection-ssh-brute-force-iam-policy.json`.
3. An EC2 instance tagged `FIS-Ready=True` and `Name=fis-security-red1` (the attacker) is running and registered with SSM.
4. An EC2 instance with private IP `10.0.0.11` (Blue1, the victim) is running in the same VPC.
5. The attacker instance has `crowbar`, `paramiko`, a `users` file, and `compromised_keys/` directory pre-installed (provided by the CDK userdata in this repo).
6. Amazon GuardDuty is enabled in the target region with findings frequency set to 15 minutes.

## How It Works

FIS selects the attacker EC2 instance by tag (`Name=fis-security-red1`) and sends the `fis-security-ssh-brute-force` SSM Run Command document to it. The document runs a shell script that uses `crowbar` to attempt SSH connections to the victim IP (`10.0.0.11`) using phony compromised SSH keys across 5 iterations. No connection is ever established — the keys are fabricated. GuardDuty observes the repeated failed SSH connection attempts and classifies Red1 as the actor in an `UnauthorizedAccess:EC2/SSHBruteForce` finding.

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
4. Creating an Amazon CloudWatch alarm to fire when HIGH severity GuardDuty findings appear.
5. Adding a stop condition tied to that alarm to automatically halt the experiment if critical thresholds are breached.
6. Extending the experiment to validate that GuardDuty findings automatically trigger a response via AWS Security Hub or Amazon Detective.

## Import Experiment

You can import the json experiment template into your AWS account via cli or aws cdk. For step by step instructions on how, [click here](https://github.com/aws-samples/fis-template-library-tooling).
