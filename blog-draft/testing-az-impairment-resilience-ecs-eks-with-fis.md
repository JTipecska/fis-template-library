# Testing application resilience to Availability Zone impairments on Amazon ECS Fargate and Amazon EKS Auto Mode with AWS FIS

Availability Zone (AZ) impairments are a failure mode that disaster recovery (DR) and business continuity planning (BCP) programs exist to withstand. For regulated organizations, proving that critical applications keep running through an AZ event is increasingly something you are expected to test for, and to evidence, on a recurring basis.

How you test an AZ impairment depends on what you run. Teams on Amazon Elastic Container Service (Amazon ECS) with AWS Fargate and teams on Amazon Elastic Kubernetes Service (Amazon EKS) in Auto Mode face the same question — "does my application survive the loss of an Availability Zone?" — but the mechanics of simulating that failure, and the recovery behavior you observe, differ between the two platforms.

[AWS Fault Injection Service (AWS FIS)](https://aws.amazon.com/fis/) is a managed service for running fault injection experiments on your AWS workloads, based on the [principles of chaos engineering](https://principlesofchaos.org/): you introduce controlled, disruptive events, observe how your application responds, and use the results to improve its resilience. This post describes ready-to-use FIS experiment templates — for both ECS Fargate and EKS Auto Mode — that simulate an AZ impairment, and explains how to use them as repeatable DR/BCP drills and as evidence for compliance reporting. The templates, IAM policies, SSM automation documents, and RBAC configuration are published in the [AWS FIS Template Library](https://github.com/aws-samples/fis-template-library) under `aws-samples`, so this post focuses on the concepts and expected behavior rather than reproducing the code.

## Why test AZ impairments with chaos engineering

A multi-AZ architecture is a design assumption until you prove it. Both platforms are designed to keep a workload running when an AZ becomes unavailable, but whether *your* workload actually does depends on your configuration — capacity headroom in the surviving AZs, task or pod placement rules, health checks, and your application's own retry logic. Conventional functional tests do not exercise these paths; chaos engineering does, through a consistent loop the experiments are built around:

1. **Define steady state** — a measurable output that indicates normal behavior (latency percentiles, error rate, healthy task or pod counts).
2. **State a hypothesis** — what you expect to remain true when an AZ is impaired.
3. **Run the experiment** — inject the impairment with AWS FIS under a steady load.
4. **Review and report** — compare observed behavior against the hypothesis, and capture the evidence.
5. **Improve** — fix any weakness surfaced, and repeat on a schedule.

Run this loop in a non-production environment first, under load, with monitoring and a stop condition in place, before running it closer to production.

## Choose the experiment that matches your platform

The library includes AZ impairment experiments for both container platforms. Use the one that matches what you run — or, if you operate both, run each against its respective workload. They are peers, not alternatives.

### Amazon ECS Fargate: AZ impairment (detection drill)

The [`ecs-fargate-az-impairment`](https://github.com/aws-samples/fis-template-library/tree/main/ecs-fargate-az-impairment) template simulates an AZ network impairment using data-plane and network-layer faults only: it denies intra-VPC traffic to and from the target AZ's subnet, injects packet loss into that AZ's Fargate tasks, then stops them — all scoped to one AZ by tag and AZ filter. It makes **no change to the ECS service's network configuration**.

That is deliberate. Because the impaired subnet remains a valid placement target, the scheduler may place replacement tasks straight back into the bad AZ while the impairment is in force — which is exactly what the experiment measures: whether your detection signals (alarms, health checks, dashboards) flag the AZ as degraded even while ECS keeps trying to use it. The hypothesis is that the service keeps serving from the healthy AZs at reduced capacity, with the impaired AZ observably unhealthy. All faults revert automatically when their duration elapses, so no manual cleanup is required.

### Amazon ECS Fargate: AZ evacuation (operator-response drill)

The companion [`ecs-fargate-az-evacuation`](https://github.com/aws-samples/fis-template-library/tree/main/ecs-fargate-az-evacuation) template goes further. After injecting the same fault and waiting a configurable detection delay — representing the time an operator or alarm takes to notice and act — it **evacuates the AZ** by removing the impaired subnet from the ECS service's network configuration, forcing all tasks into the healthy AZs, and restores the subnet automatically at the end of the window or on cancellation.

Use it to drill the *response* to an impairment, not just detection: does your evacuation runbook correctly pull the bad AZ out of rotation, and does the service rebalance when it is restored? Running the fault-only template first and the evacuation template second mirrors how a real incident unfolds — detect, then respond.

### Amazon EKS Auto Mode: AZ impairment

The [`eks-automode-az-impairment`](https://github.com/aws-samples/fis-template-library/tree/main/eks-automode-az-impairment) template addresses a platform that behaves differently. With EKS Auto Mode, AWS manages the underlying EC2 instances, so the traditional approach of stopping instances does not apply — you cannot stop them, and Auto Mode provisions replacements, potentially back in the impaired AZ.

To simulate the impairment realistically, the experiment combines three faults: it blocks all traffic to the target AZ's subnets at the network layer, cordons the AZ's nodes and patches the NodePool so Auto Mode will not schedule new capacity there, and force-deletes the AZ's pods so Kubernetes reschedules them into the healthy AZs. Ordering matters — the AZ is excluded before pods are deleted, leaving no placement option in the impaired AZ. The Kubernetes operations run from a short-lived EC2 client inside your VPC, so the experiment works against a private EKS endpoint without opening public access, and the client is cleaned up automatically.

One configuration note is documented with the template: point the experiment at a **custom NodePool that you own**, not the built-in Auto Mode NodePools, which are reconciled back to their managed configuration and will silently revert the AZ exclusion partway through.

What you observe depends on your workload's topology spread constraints, the capacity in the surviving AZs, and how many AZs the cluster spans — properties of your cluster, not of the experiment, which is the whole point of testing them. The documentation includes validated results that make this concrete. In a two-AZ cluster running six replicas with a strict `DoNotSchedule` spread constraint, losing one AZ left the service at 50 percent — three pods running, three unschedulable — for the full fifteen-minute impairment; relaxing to `ScheduleAnyway` kept all six running on the surviving AZ's existing nodes with no new capacity. In a three-AZ cluster with the same strict constraint, the two pods in the impaired AZ rescheduled one to each surviving zone for a clean 0/3/3 distribution, all six running throughout. The lesson is the one worth testing for: the same experiment yields a surviving or a degraded service depending entirely on how the workload is configured and how many AZs it spans.

## What to monitor

Monitoring is what turns a fault injection into a resilience test. Keep the system under a steady load before you begin so you can observe the impairment as it happens. Metrics differ by platform but the categories are the same, and the template documentation lists concrete starting points:

- **Application performance** — request latency (p99, p95, p50), error rates, throughput, and client-side retry patterns.
- **Platform health** — for ECS, running/pending task counts and deployment status; for EKS, node count per AZ and pod scheduling failures; for both, how quickly replacement capacity appears in the healthy AZs.
- **Load balancer behavior** — healthy/unhealthy host counts, target response time, and target error codes, showing how quickly traffic shifts to healthy targets.

Amazon CloudWatch is a natural home for this: build dashboards combining platform and application metrics, alarm on critical thresholds, and wire a CloudWatch alarm into the FIS experiment as a **stop condition** so it halts automatically if a real threshold is breached. The templates ship without stop conditions by default because the relevant thresholds depend on your application.

## Using these experiments for DR, BCP, and compliance reporting

An AZ impairment experiment is a DR and BCP drill in everything but name. Run on a schedule, it answers the question those plans exist to answer — can this critical application absorb the loss of an Availability Zone? — with evidence rather than assertion. The chaos engineering loop maps directly onto what resilience-testing programs ask for: identify the critical function, define expected behavior, run the scenario, report the outcome, remediate.

For regulated organizations, this evidence is increasingly expected. In Australia, [APRA Prudential Standard CPS 230 Operational Risk Management](https://www.apra.gov.au/operational-risk-management) requires regulated entities to maintain the resilience of their critical operations within defined tolerance levels and to test their ability to do so. Recurring, documented AZ impairment experiments — with their hypotheses, monitored results, and remediation actions — contribute directly to that testing evidence. The same approach supports scenario-based testing under the EU's Digital Operational Resilience Act (DORA) and the availability criteria of a SOC 2 examination, both of which look for demonstrable, repeatable resilience testing rather than one-off exercises.

The artifacts these experiments produce are what make them useful for reporting. Each FIS experiment records what was injected, against which targets, and when; your CloudWatch dashboards and alarm history capture how the application behaved. Together with the documented hypothesis and your record of follow-up actions, this forms a repeatable evidence trail — the same experiment, run quarterly against the same critical workload, produces comparable results each time. Nothing here makes an organization compliant by itself, but a disciplined chaos engineering practice built on these templates is a strong, auditable component of it.

## Conclusion

Whether you run Amazon ECS on AWS Fargate or Amazon EKS in Auto Mode, AWS FIS lets you simulate an Availability Zone impairment and verify that your application keeps serving from the healthy zones. The experiments here — AZ impairment and AZ evacuation for ECS Fargate, and AZ impairment for EKS Auto Mode — are published in the [AWS FIS Template Library](https://github.com/aws-samples/fis-template-library), ready to adapt to your own services, tags, and tolerances. Run them in a non-production environment first, under load and with monitoring and a stop condition in place, then fold them into your regular DR and BCP testing so each run adds to the evidence that your multi-AZ architecture does what you designed it to do.

To go further, explore the [AWS FIS Workshop](https://catalog.workshops.aws/fis-v2/en-US), review the [Amazon ECS Best Practices Guide](https://docs.aws.amazon.com/AmazonECS/latest/bestpracticesguide/intro.html) and the [Amazon EKS Best Practices Guide](https://docs.aws.amazon.com/eks/latest/best-practices/introduction.html), and see the [Well-Architected Reliability pillar guidance on testing resiliency with chaos engineering](https://docs.aws.amazon.com/wellarchitected/latest/framework/rel_testing_resiliency_failure_injection_resiliency.html).

---

### About the author

**Jennifer Tipecska** is a Solutions Architect at Amazon Web Services based in Sydney, Australia, where she works with financial services customers across Australia and New Zealand on cloud modernization and operational resilience. She helps regulated organizations design, test, and evidence the resilience of their critical workloads using AWS services such as AWS Fault Injection Service.
