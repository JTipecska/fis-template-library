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

1. You have the roles created for FIS and SSM Automation to use. Example IAM policy documents and trust policies are provided (separate roles for FIS and SSM Automation).
2. You have created the SSM Automation Document from one of the provided samples:
   - `vpc-peering-route-disruption-automation.yaml` — Route deletion variant (simple, no TGW fallback)
   - `vpc-peering-route-blackhole-automation.yaml` — Blackhole variant (prevents TGW fallthrough)
3. You have created the corresponding FIS Experiment Template:
   - `vpc-peering-route-disruption-template.json` — For the deletion variant
   - `vpc-peering-route-blackhole-template.json` — For the blackhole variant
4. The VPC peering connection is in an `active` state.
5. The route table(s) you want to target have the `FIS-Ready=True` tag.
6. The route table(s) contain a route entry for the peer VPC CIDR pointing to the peering connection.
7. (Blackhole variant only) A subnet exists in the same VPC as the target route tables — needed for temporary ENI creation.
8. You have appropriate monitoring and observability in place to track the impact of the experiment.

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

## Bidirectional disruption for cross-region peering

A single experiment instance removes routes from one side of the peering connection only, creating an **asymmetric failure**. Traffic originating from the non-disrupted side is still routed toward the peer (though TCP connections will fail because return traffic has no path). Stateless protocols (UDP, ICMP) originating from the non-disrupted side can still reach the disrupted VPC.

For a complete bidirectional disruption of cross-region peering, deploy the SSM Automation Document in **both regions** and run one FIS experiment per region simultaneously:

```bash
# Deploy the SSM document in both regions
aws ssm create-document --name "FIS-Disrupt-VPC-Peering-Routes" \
  --document-type Automation --document-format YAML \
  --content file://vpc-peering-route-disruption-automation.yaml --region <REGION A>

aws ssm create-document --name "FIS-Disrupt-VPC-Peering-Routes" \
  --document-type Automation --document-format YAML \
  --content file://vpc-peering-route-disruption-automation.yaml --region <REGION B>

# Start both experiments simultaneously for full bidirectional disruption
aws fis start-experiment --experiment-template-id <REGION_A_TEMPLATE_ID> --region <REGION A> &
aws fis start-experiment --experiment-template-id <REGION_B_TEMPLATE_ID> --region <REGION B> &
wait
```

Each experiment targets the route table in its own region:
- **Region A experiment**: deletes the route to the peer VPC CIDR from the local route table
- **Region B experiment**: deletes the route to the peer VPC CIDR from the local route table

Both experiments manage their own rollback independently — if one fails, the other still restores its routes.

## Indirect paths through Transit Gateway

When VPCs are connected through multiple mechanisms (e.g., VPC peering for cross-region and Transit Gateway for cross-environment connectivity within a region), deleting peering routes alone may not achieve full network isolation. Traffic can potentially traverse an indirect path through the Transit Gateway and adjacent VPCs.

For example, consider two VPCs (VPC-A and VPC-B) connected by peering, where each is also attached to a Transit Gateway that connects to other VPCs:

```
VPC-A ──TGW──► VPC-C ──Peering──► VPC-D ──TGW──► VPC-B
```

Deleting the direct VPC-A → VPC-B peering route does **not** block this indirect path. If route tables in the intermediate VPCs allow transitive routing, traffic from VPC-A can still reach VPC-B through VPC-C and VPC-D.

**Implications for isolation testing:**

- This experiment is effective for environments where VPC peering is the **only** path between the target VPCs.
- If Transit Gateway or other routing mechanisms provide alternative paths, this experiment should be combined with additional controls:
  - **NACL-based denial** (e.g., FIS `aws:network:disrupt-connectivity`) to block traffic at the subnet level regardless of routing path
  - **TGW route table manipulation** to remove route propagation that allows indirect transit between the target VPCs
- Use VPC Flow Logs or VPC Reachability Analyzer to confirm that no indirect paths exist before relying on route deletion alone for isolation.

## Blackhole variant (prevents TGW fallthrough)

This directory includes a second automation document (`vpc-peering-route-blackhole-automation.yaml`) that addresses the TGW fallback problem. Instead of deleting routes, it **replaces** them with a route pointing to a temporary unattached ENI — creating a blackhole.

### When to use the blackhole variant

Use this variant when:
- The VPC has a default route (`0.0.0.0/0 → TGW`) or other catch-all route that would carry traffic after a specific route is deleted
- You need to block traffic to the target CIDR while preserving all other connectivity through the default route (e.g., to production VPCs via the same TGW)
- NACL-based approaches are not feasible due to large subnet counts or control plane scaling limits
- The TGW route table is shared across environments, preventing a TGW-level blackhole

### How the blackhole variant works

1. **Create temporary ENI** — An unattached network interface is created in the same VPC. An unattached ENI as a route target causes traffic to be silently dropped (blackhole).
2. **Replace route atomically** — `ec2:ReplaceRoute` swaps the peering connection target with the ENI target in a single API call. There is no window where the route is absent and traffic could fall through to the default route.
3. **Wait for duration** — Traffic to the target CIDR is dropped while all other routes (including the default TGW route) continue functioning normally.
4. **Restore route atomically** — `ec2:ReplaceRoute` swaps back to the original peering connection target.
5. **Delete temporary ENI** — Cleanup of the temporary resource.

### Comparison of approaches

| Aspect | Route deletion | Blackhole (ENI) |
|--------|---------------|-----------------|
| Route during fault | Absent | Present but blackholed |
| Default route fallthrough | Traffic reaches target via TGW | Traffic is dropped |
| API operation | DeleteRoute / CreateRoute | ReplaceRoute (atomic swap) |
| Brief gap in coverage | Yes (between delete and restore) | No (atomic replace) |
| Temporary resources | None | One ENI (auto-cleaned) |
| Use when | Peering is the only path | Default/TGW fallback exists |

### Deploying the blackhole variant

```bash
aws ssm create-document --name "FIS-Blackhole-VPC-Peering-Routes" \
  --document-type Automation --document-format YAML \
  --content file://vpc-peering-route-blackhole-automation.yaml \
  --region <YOUR REGION>
```

The `SubnetId` parameter must reference a subnet in the same VPC as the targeted route tables. Any subnet will work — the ENI is never attached to an instance.

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
