# Experiment Results

Results from end-to-end testing of both VPC peering route disruption variants.

## Test Environment

```
         us-east-1                              us-west-2
┌─────────────────────────┐          ┌─────────────────────────┐
│  Staging-East (10.0/16) │◄─peering─►  Staging-West (10.1/16) │
└───────────┬─────────────┘          └───────────┬─────────────┘
            │ TGW                                 │ TGW
┌───────────┴─────────────┐          ┌───────────┴─────────────┐
│  Prod-East (10.2/16)    │◄─peering─►  Prod-West (10.3/16)    │
└─────────────────────────┘          └─────────────────────────┘
```

Connectivity paths:
- Staging-east ↔ Staging-west: VPC Peering (cross-region)
- Prod-east ↔ Prod-west: VPC Peering (cross-region)
- Staging ↔ Prod (same region): Transit Gateway

---

## Experiment 1: Unidirectional Route Deletion

**Variant:** `vpc-peering-route-disruption-automation.yaml`  
**Scope:** Single route table (staging-east only)  
**Result:** PASSED

### What happened

Deleted the `10.1.0.0/16 → pcx-xxx` route from staging-east only.

### Route table state during fault

| VPC | Route to 10.1.0.0/16 | TGW routes | Status |
|-----|----------------------|------------|--------|
| Staging-East | **DELETED** | Intact | Disrupted |
| Staging-West | Active (peering) | Intact | Unmodified |
| Prod-East | N/A | Intact | Unmodified |
| Prod-West | N/A | Intact | Unmodified |

### Finding

Creates an **asymmetric failure**. Traffic from staging-east to staging-west is broken, but staging-west can still route packets toward staging-east (return traffic fails, so TCP breaks bidirectionally, but UDP/ICMP is one-way leaky).

---

## Experiment 2: Bidirectional Route Deletion

**Variant:** `vpc-peering-route-disruption-automation.yaml` (one per region)  
**Scope:** Both staging route tables simultaneously  
**Result:** PASSED

### What happened

Two FIS experiments started simultaneously — one deletes `10.1.0.0/16` from staging-east, the other deletes `10.0.0.0/16` from staging-west.

### Route table state during fault

| VPC | Peering route | TGW route to prod | Status |
|-----|--------------|-------------------|--------|
| Staging-East | **DELETED** (10.1.0.0/16) | Active (10.2.0.0/16 → TGW) | Disrupted |
| Staging-West | **DELETED** (10.0.0.0/16) | Active (10.3.0.0/16 → TGW) | Disrupted |
| Prod-East | Active | Active | Unmodified |
| Prod-West | Active | Active | Unmodified |

### Findings

1. Full bidirectional peering isolation achieved — no traffic flows between staging VPCs in either direction.
2. All staging-to-production TGW paths remain active and unaffected.
3. Production-to-production peering is completely independent and untouched.
4. Requires two FIS experiments (one per region) because FIS cannot reference SSM documents cross-region.
5. Near-simultaneous but not atomic — brief window (seconds) where one direction may be disrupted before the other.

### Limitation discovered

If a default route (`0.0.0.0/0 → TGW`) exists in the staging route tables, deleting the specific peering route causes traffic to **fall through** to the TGW default route — traffic can still reach the other staging VPC indirectly through the transit gateway.

---

## Experiment 3: Blackhole via Dummy ENI

**Variant:** `vpc-peering-route-blackhole-automation.yaml`  
**Scope:** Single route table (staging-east) with default TGW route present  
**Result:** PASSED

### Setup

Added a default route `0.0.0.0/0 → TGW` to the staging-east route table to simulate the TGW fallback scenario.

### What happened

The automation:
1. Created a temporary unattached ENI in the same VPC
2. Replaced the `10.1.0.0/16 → pcx-xxx` route with `10.1.0.0/16 → eni-xxx` using `ec2:ReplaceRoute`
3. The route state became "blackhole" — traffic is silently dropped
4. After the duration, replaced back to the original peering target
5. Deleted the temporary ENI

### Route table state during fault

| Destination | Target | State | Traffic outcome |
|-------------|--------|-------|-----------------|
| 10.1.0.0/16 | eni-0495ce106013fb26a | **blackhole** | Dropped |
| 10.2.0.0/16 | tgw-04a0f952ff6a88137 | active | Flows normally |
| 0.0.0.0/0 | tgw-04a0f952ff6a88137 | active | Flows normally |

### Findings

1. **Blackhole confirmed** — Route to target CIDR shows state "blackhole", traffic is silently dropped.
2. **No TGW fallthrough** — Despite `0.0.0.0/0 → TGW` being present, traffic to `10.1.0.0/16` is dropped because the specific `/16` route (now blackholed) takes precedence over the `/0` default.
3. **Staging-to-prod preserved** — TGW route `10.2.0.0/16` remains active; production connectivity is unaffected.
4. **Default route unmodified** — `0.0.0.0/0 → TGW` untouched; all non-target-CIDR traffic continues flowing via TGW as normal.
5. **Atomic operation** — `ec2:ReplaceRoute` is a single API call. There is no moment where the route is absent, eliminating the fallthrough window that exists with delete/create.
6. **Clean ENI lifecycle** — Temporary ENI created, used as blackhole target, deleted after restore. No orphaned resources.
7. **Full rollback validated** — Route restored to `10.1.0.0/16 → pcx-xxx` (active), ENI confirmed deleted.

---

## Summary: Choosing the Right Variant

| Scenario | Recommended variant |
|----------|-------------------|
| Peering is the only path to target VPC | Route deletion (`vpc-peering-route-disruption-automation.yaml`) |
| Default route or TGW fallback exists | Blackhole ENI (`vpc-peering-route-blackhole-automation.yaml`) |
| Need bidirectional isolation | Deploy chosen variant in both regions, run simultaneously |
| Shared TGW route table (cannot blackhole at TGW level) | Blackhole ENI (operates at VPC route table level) |
| Large subnet count (NACL approach doesn't scale) | Blackhole ENI (single API call regardless of subnet count) |

## Key Technical Insights

1. **Route specificity prevents fallthrough** — A `/16` blackhole route always takes precedence over a `/0` default route. This is the fundamental property that makes the blackhole approach work.

2. **Unattached ENI = automatic blackhole** — When a route points to a network interface that is not attached to a running instance, AWS marks the route state as "blackhole" and drops matching traffic. No additional configuration needed.

3. **ReplaceRoute is atomic** — Unlike delete-then-create, `ec2:ReplaceRoute` swaps the target in a single API call. There is never a moment where the route is absent from the table.

4. **Cross-region FIS limitation** — A single FIS experiment can only reference SSM documents in the same region. Bidirectional cross-region disruption requires coordinated experiments in each region.

5. **TGW route table architecture matters** — If TGW route tables are shared across environments, blackholing at the TGW level would affect all attachments (including production). The ENI blackhole approach operates at the VPC route table level, affecting only the specific VPC.
