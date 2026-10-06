# AWS Fault Injection Service Experiment: DNS Data Exfiltration

This is an experiment template for use with AWS Fault Injection Service (FIS) and fis-template-library-tooling. This experiment template requires deployment into your AWS account and requires resources in your AWS account to inject faults into.

THIS TEMPLATE WILL INJECT REAL FAULTS! THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Hypothesis

When an EC2 instance generates a high volume of DNS queries matching exfiltration patterns — specifically, queries to domains listed in the GuardDuty threat intelligence feed — Amazon GuardDuty detects the behavior within 15 minutes and generates a `Trojan:EC2/DNSDataExfiltration` finding with HIGH severity against the instance.

## Prerequisites

Before running this experiment, ensure that:

1. You have the necessary permissions to start FIS experiments and send SSM Run Command documents.
2. The IAM role specified in `roleArn` has the permissions defined in `security-detection-dns-exfiltration-iam-policy.json`.
3. An EC2 instance tagged `FIS-Ready=True` and `Name=fis-security-blue1` (the victim) is running and registered with SSM.
4. The victim instance has outbound internet access (to download the query list from GitHub and to send DNS queries).
5. `curl` and `dig` are available on the instance (both pre-installed on Amazon Linux 2).
6. Amazon GuardDuty is enabled in the target region with findings frequency set to 15 minutes.

## How It Works

FIS selects the victim EC2 instance by tag (`Name=fis-security-blue1`) and sends the `fis-security-dns-exfiltration` SSM Run Command document to it. The document downloads a list of domain queries from the `amazon-guardduty-tester` repository and runs `dig` against them in batch. This generates the DNS query volume and pattern that GuardDuty classifies as potential data exfiltration via DNS tunneling. No actual data is encoded or exfiltrated — the test is purely behavioural.

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
3. Creating an Amazon CloudWatch metric filter on GuardDuty findings published via EventBridge.
4. Creating an Amazon CloudWatch alarm to fire when HIGH severity exfiltration findings appear.
5. Adding a stop condition tied to that alarm to automatically halt the experiment if critical thresholds are breached.
6. Validating that AWS Security Hub aggregates the GuardDuty finding and that the appropriate security team is notified.

## Import Experiment

You can import the json experiment template into your AWS account via cli or aws cdk. For step by step instructions on how, [click here](https://github.com/aws-samples/fis-template-library-tooling).
