# AWS Fault Injection Service Experiment: Cryptocurrency Mining Activity

This is an experiment template for use with AWS Fault Injection Service (FIS) and fis-template-library-tooling. This experiment template requires deployment into your AWS account and requires resources in your AWS account to inject faults into.

THIS TEMPLATE WILL INJECT REAL FAULTS! THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Hypothesis

When an EC2 instance contacts URLs associated with cryptocurrency mining pools that appear on GuardDuty's threat intelligence lists (specifically ProofPoint), Amazon GuardDuty detects the activity within 15 minutes and generates a `CryptoCurrency:EC2/BitcoinTool.B!DNS` finding with HIGH severity against the instance.

## Prerequisites

Before running this experiment, ensure that:

1. You have the necessary permissions to start FIS experiments and send SSM Run Command documents.
2. The IAM role specified in `roleArn` has the permissions defined in `security-detection-crypto-currency-mining-activity-iam-policy.json`.
3. An EC2 instance tagged `FIS-Ready=True` and `Name=fis-security-blue1` (the victim) is running and registered with SSM.
4. The victim instance has outbound internet access (to make HTTP requests to the mining pool URLs).
5. `curl` is available on the instance (pre-installed on Amazon Linux 2).
6. Amazon GuardDuty is enabled in the target region with findings frequency set to 15 minutes.

## How It Works

FIS selects the victim EC2 instance by tag (`Name=fis-security-blue1`) and sends the `fis-security-crypto-currency-mining-activity` SSM Run Command document to it. The document uses `curl` to make HTTP requests to two MinerGate pool URLs with fabricated paths. These URLs appear on GuardDuty's threat intelligence lists. No mining software is downloaded and no actual mining activity occurs — the test only generates the network interaction that triggers threat-intelligence-based detection.

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
4. Creating an Amazon CloudWatch alarm to fire when HIGH severity cryptocurrency findings appear.
5. Adding a stop condition tied to that alarm to automatically halt the experiment if critical thresholds are breached.
6. Validating that the finding triggers an automated isolation response (e.g., via AWS Systems Manager Incident Manager or a Lambda-based playbook).

## Import Experiment

You can import the json experiment template into your AWS account via cli or aws cdk. For step by step instructions on how, [click here](https://github.com/aws-samples/fis-template-library-tooling).
