# AWS Fault Injection Service Experiment: Disrupting VPC Peering Route Table Connectivity

This is an experiment template for use with AWS Fault Injection Service (FIS) and fis-template-library-tooling. This experiment template requires deployment into your AWS account and requires resources in your AWS account to inject faults into.

THIS TEMPLATE WILL INJECT REAL FAULTS! THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Hypothesis

Our application will remain available and resilient when VPC peering connectivity is disrupted between two VPCs, simulating a scenario where cross-VPC routing fails. Dependent services in the peer VPC should be detected as unavailable within the monitoring window, and the application should gracefully degrade or fail over to alternative paths.

### What does this enable me to verify?

* Appropriate monitoring and observability of cross-VPC connectivity is in place (were you able to detect the routing failure?)
* Alarms are configured correctly (were the right people notified at the right time?)
* Your application gracefully degrades when a peered VPC becomes unreachable
* Circuit breakers and retry logic handle the connectivity loss appropriately
* Recovery is automatic once routes are restored

## Prerequisites

Before running this experiment, ensure that:

1. You have the roles created for FIS and SSM Automation to use. Example IAM policy documents and trust policies are provided.
2. You have created the SSM Automation Document from the sample provided (vpc-peering-route-disruption-automation.yaml).
3. You have created the FIS Experiment Template from the sample provided (vpc-peering-route-disruption-template.json).
4. The VPC peering connection is in an `active` state.
5. The route table(s) you want to target have the `FIS-Ready=True` tag.
6. The route table(s) contain a route entry for the peer VPC CIDR pointing to the peering connection.
7. You have appropriate monitoring and observability in place to track the impact of the experiment.

## How it works

This experiment disrupts VPC peering connectivity by removing route table entries that direct traffic to the peer VPC. The peering connection itself remains intact, so routes can be cleanly restored after the experiment.

The SSM Automation Document performs these steps:

1. **Validate prerequisites** — Confirms the peering connection is active and the specified route tables contain routes to the peer VPC CIDR.
2. **Capture original state** — Records the current route configurations (route table ID, destination CIDR, peering connection ID) for rollback.
3. **Delete peering routes** — Removes routes pointing to the peer VPC CIDR from the specified route tables. Traffic to the peer VPC will be dropped.
4. **Wait for duration** — Holds the fault for the configured period.
5. **Restore routes** — Recreates the original routes using the captured state.
6. **Validate restoration** — Confirms all routes are restored and active.

If any fault injection step fails, the automation automatically jumps to the restore step. The automation also handles FIS-initiated cancellation by restoring routes before exiting.

To verify the experiment is working, you can test connectivity to a resource in the peer VPC during the impairment:

```bash
watch -n 5 'ping -c 1 -W 2 <PEER VPC RESOURCE IP> && echo "REACHABLE" || echo "UNREACHABLE"'
```

During the impairment period, you should see "UNREACHABLE" responses.

## Observability and stop conditions

Stop conditions are based on an AWS CloudWatch alarm tied to an operational or business metric requiring an immediate end of the fault injection. This template makes no assumptions about your application and the relevant metrics, so it does not include stop conditions by default (`"stopConditions": [{ "source": "none" }]`).

**Choose the stop-condition metric carefully.** Do *not* alarm on network metrics that this experiment is designed to perturb (e.g., VPC Flow Log reject counts on the peering path). Instead, alarm on a signal that should stay healthy if your resilience works:

* An application error-rate or latency metric that measures customer impact directly.
* Load balancer 5xx count or target response time on services that depend on the peered VPC.
* A synthetic canary that exercises the cross-VPC dependency and reports availability.

Create the alarm, then reference it in the experiment template's `stopConditions`:

```json
"stopConditions": [
  {
    "source": "aws:cloudwatch:alarm",
    "value": "arn:aws:cloudwatch:<YOUR REGION>:<YOUR AWS ACCOUNT>:alarm:vpc-peering-fis-customer-impact"
  }
]
```

See [Stop conditions for AWS FIS](https://docs.aws.amazon.com/fis/latest/userguide/stop-conditions.html).

## Next Steps

As you adapt this scenario to your needs, we recommend:

1. Reviewing the tag names you use to ensure they fit your specific use case.
2. Identifying business metrics tied to cross-VPC communication, such as request latency or error rates for services in the peer VPC.
3. **Before running anything beyond a short test, add a stop condition** tied to a customer-impact alarm so the experiment aborts automatically if critical thresholds are breached (see [Observability and stop conditions](#observability-and-stop-conditions) for guidance).
4. Implementing appropriate timeouts and circuit breakers for cross-VPC calls in your application.
5. Testing your application's failover or graceful degradation behavior when the peer VPC is unreachable.
6. Documenting the findings from your experiment and updating your incident response procedures accordingly.

## Import Experiment

You can import the json experiment template into your AWS account via cli or aws cdk. For step by step instructions on how, [click here](https://github.com/aws-samples/fis-template-library-tooling).
