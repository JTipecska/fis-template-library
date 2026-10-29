# Simulating Bedrock 503 / throttling with FIS — data collection

Prepared for: Mark Jones' question "any easy way to simulate Bedrock 503 errors with FIS?"
Status: research only, no build. Date: 2026-10-21.

## Bottom line

There is **no FIS managed action for Amazon Bedrock today**. FIS does not list Bedrock as a
supported target service, and the one generic fault that could in principle inject 503s
(`aws:fis:inject-api-unavailable-error`) is documented as supporting only the `ec2` and
`kinesis` API namespaces — not `bedrock`. No AWS-published or internal (FOMSE/team) SSM-document
FIS template for Bedrock 503/throttling was found. If Mark needs this now, it has to be built,
and the realistic pattern is a client-side interceptor driven by an SSM document (the same
mechanism this repo already uses elsewhere), not a native FIS Bedrock action.

No announcement was found committing FIS to a managed Bedrock fault action. The relevant
forward-looking signal is a *Bedrock-side* capability (the RIV "our own gateway" item Mark
referenced), not an FIS feature.

## 1. Does FIS have a managed Bedrock fault action?

No. Evidence:

- The **FIS Actions reference** lists supported target services: CloudWatch, DynamoDB, Aurora
  DSQL, EBS, EC2, ECS, EKS, ElastiCache, Kinesis Data Streams, Lambda, MemoryDB, Network, RDS,
  S3, Systems Manager, Direct Connect. Bedrock is not among them.
  https://docs.aws.amazon.com/fis/latest/userguide/fis-actions-reference.html
- The three generic API-fault actions exist:
  - `aws:fis:inject-api-internal-error`
  - `aws:fis:inject-api-throttle-error` (ThrottlingException, HTTP 400/429)
  - `aws:fis:inject-api-unavailable-error` (**ServiceUnavailable, HTTP 503** — the one Mark wants)
  Each targets an `aws:iam:role` and takes a `service` parameter, but the docs state **"The
  supported value is `ec2` and `kinesis`."** So these cannot target `bedrock` API calls today.
  They also only affect calls made by the target IAM role via the AWS SDK (client-side
  interception), they do not degrade the Bedrock service itself.

## 2. Is a managed FIS Bedrock capability announced / on the roadmap?

No FIS-managed Bedrock fault action was found announced. What exists:

- **Shared-responsibility framing** (internal TAM wiki, "Amazon Bedrock: Building Resilient
  Applications"): AWS owns Bedrock service resilience; the customer owns application-layer
  resilience (retries, circuit breakers, multi-region, model redundancy). It lists chaos
  engineering as a customer responsibility but points at generic FIS, with **no Bedrock-specific
  fault**. https://w.amazon.com/bin/view/AWS/TAM_Bedrock_Support/Bedrock_Resilient_Applications/
- **Forward signal is Bedrock-side, not FIS-side.** Per Mark's thread: Harbinger customer
  messaging now states Bedrock inference endpoints should **not** be treated as an HA/DR
  capability, and a new capability ("our own gateway") is targeted at RIV, with a mid-term
  plan to combine with AgentCore Gateway. That is a mitigation/product direction, not an FIS
  fault-injection feature. (Harbinger notice and RIV broadcast links are in Mark's Slack thread.)

Net: if the ask is "will AWS ship a managed FIS action that makes Bedrock return 503," there is
no evidence that is committed. Plan to build the test harness yourself.

## 3. Has anyone already built an SSM / FIS template for Bedrock faults?

No ready-made Bedrock-503 SSM+FIS template was found (public aws-samples, awslabs, or internal
code search). The closest building blocks:

- **This repo already has `agentcore-strands-agent-faults/`** — a chaos experiment against a
  Strands agent on Bedrock AgentCore (`agentcore-strands-agent-faults/strands_agentcore_chaos.py`
  plus a `-template.json` and `-automation.yaml`). It is the nearest existing pattern in the
  library for faulting an agentic/Bedrock workload and is the natural place to model a
  Bedrock-error experiment on.
- **FIS partial-failure pattern (AWS Cloud Ops blog, Jun 2025):** FIS + ALB weighted routing +
  a Lambda that returns custom error responses (e.g. 503) to a configurable % of traffic, with
  automated setup/rollback via CloudFormation. This is the AWS-blessed way to inject HTTP error
  responses without a native action, and it transfers directly to a Bedrock front-door.
  https://aws.amazon.com/blogs/mt/simulating-partial-failures-with-aws-fault-injection-service/
- **adhorn/chaos-ssm-documents** and **adhorn/aws-fis-templates-cdk** — generic SSM documents for
  latency/error injection usable from FIS `aws:ssm:send-command` / `start-automation-execution`.
  Not Bedrock-specific. https://github.com/adhorn/chaos-ssm-documents
- **aws-samples/serverless-chaos-experiments** — FIS driving SSM documents for serverless faults;
  same mechanism, not Bedrock-specific.
  https://github.com/aws-samples/serverless-chaos-experiments
- Many internal repos simulate Bedrock ThrottlingException/503 **in unit tests** (fake
  `invoke_model` raising `ThrottlingException` or HTTP 503), proving the client-interceptor
  approach is what teams actually use — but none package it as a reusable FIS experiment.

## 4. Practical ways to build it (for a demo + test harness)

Collected options, from least to most faithful; no build done:

1. **SSM-document client interceptor (recommended for a Bedrock SDK caller).**
   FIS experiment → `aws:ssm:send-command` / `start-automation-execution` → an SSM document on
   the host/container running the agent that wraps the `bedrock-runtime` client (or installs a
   boto3 fault layer) to raise `ServiceUnavailableException` (503) / `ThrottlingException` (429)
   for a configurable %/duration, then cleans up. Mirrors the mechanism already used across this
   library and in `agentcore-strands-agent-faults/`.
2. **Proxy / front-door error injection (FIS + ALB weighted routing + Lambda).** If the agent
   calls Bedrock through a gateway/proxy, point a % of traffic at a Lambda that returns 503.
   This is the AWS Cloud Ops blog pattern and needs no application code change.
3. **`aws:fis:inject-api-unavailable-error`** — note for completeness that it is **not usable**
   for Bedrock today (service param only supports `ec2`/`kinesis`). Worth re-checking later in
   case AWS adds a `bedrock` namespace.

The narrative angle Jennifer raised also holds up on the evidence: CRIS account/region pinning
turns a regional capacity event into a customer blast-radius problem (per the
AmazonBedrockFrontendService sharded-sticky-routing design doc in Mark's thread) — a clean
"non-optimal cell architecture" case study, and exactly the failure a Bedrock-503 experiment
would let a customer rehearse.

## Source-access notes (added 2026-10-21, 2nd pass)

- **Harbinger notice — content now supplied by Jennifer (official reactive wording).**
  Service: Bedrock. Region: Global. Type: REACTIVE_WORDING. Last updated 2026-10-03.
  The command-center URL itself is auth-gated (HTTP 401) and its SPA case ID could not be
  resolved by the internal reader, so the text below comes from the official notice Jennifer
  pasted, not a live fetch. Key official positions:
  - CRIS lets you call a model through an **inference profile** instead of a single Region;
    Bedrock picks which covered Region serves each request. Two profile types: **global**
    (any supported commercial Region where the model is offered) and **geographic**
    (US / EU / APAC — for data residency).
  - **"Cross-Region inference is primarily a capacity mechanism."** It places traffic where
    capacity is available and balances all-customer demand across Regions for throughput and
    consistent performance under load. **"It is not, by itself, a failover or disaster recovery
    mechanism, and it should not be relied on as one. It does not guarantee that requests will be
    moved to another Region if the Region serving becomes impaired."**
  - Recommended resilience moves (customer-owned): use more than one **geographic** profile
    (e.g. US + EU) where residency allows; shift traffic gradually; pre-request quota increases
    for every profile/model you may shift to; **retry only transient errors (HTTP 503 and 529)
    with exponential backoff + jitter and an attempt cap**; pre-test any fallback model; monitor
    errors, latency, and prompt-cache hit rate. Served Region is visible in CloudTrail
    `additionalEventData.inferenceRegion`.
  - Before moving traffic: **prompt caching** is Region-local (a move cold-starts the cache →
    added latency + new cache-write cost; caches expire quickly so intermittent traffic may not
    benefit); **OpenAI Responses API stored responses** stay in their creating Region, so a
    conversation using `previous_response_id` should not be moved mid-flight.

  This official wording confirms the thread's framing: CRIS is capacity optimization, explicitly
  not HA/DR, and 503/529 are the transient errors a resilient caller is expected to retry — which
  is exactly what a Bedrock-503 FIS experiment should exercise.

- **RIV broadcast (broadcast.amazon.com/videos/2192604) — retrieved in full.** It is the
  **October 2026 GenAI/ML Roadmap** session. CONFIDENTIAL / partly embargoed — do not copy,
  record, or share with customers; roadmap ambassadors brief customers directly. Relevant points:
  - **Bedrock Gateway** — a *first-party* LLM gateway (comparable to LiteLLM / Portkey / Bifrost),
    unified control plane across Bedrock, SageMaker, EKS/EC2 self-hosted, and third-party
    providers. **Planned to launch at re:Invent**; "very much in development," 3–4 weeks of
    further updates expected. Resilience features called out: **automatic retries and fallbacks,
    a cooling-off period, spreading model load across regions and across models, and rate limits
    to protect shared quota from runaway scenarios.** This is the "our own gateway" Mark referenced.
  - **Bedrock Model Router** — also teed up for re:Invent; 4 preconfigured routers
    (Anthropic / open-source / OpenAI / custom), cost / quality / balanced modes, cache-aware,
    emits a visible routing score. Configurable routers are a fast-follow post-re:Invent.
  - **Convergence** — active Bedrock ↔ AgentCore Gateway discussion (AgentCore already has
    inference targets + rate limiting that look gateway-like); guidance on which to choose is
    planned. Matches Jennifer's "we already have AgentCore Gateway" point and Mark's "mid-term is
    to combine."
  - No FIS content in the broadcast. It does **not** announce any FIS-managed Bedrock fault.

  Takeaway for the test harness: the thing worth faulting in a demo is increasingly the
  **gateway's** retry/fallback/region-spread behaviour, not raw CRIS. A Bedrock-503 experiment is
  exactly what proves (or disproves) that a gateway fallback actually triggers.

## Sources

- FIS Actions reference (service support + 503/throttle actions):
  https://docs.aws.amazon.com/fis/latest/userguide/fis-actions-reference.html
- FIS partial-failures pattern (503 via ALB+Lambda):
  https://aws.amazon.com/blogs/mt/simulating-partial-failures-with-aws-fault-injection-service/
- Bedrock resilient applications (shared responsibility, internal):
  https://w.amazon.com/bin/view/AWS/TAM_Bedrock_Support/Bedrock_Resilient_Applications/
- APG pattern pairing FIS with Bedrock (note: Bedrock is used for *insights*, not as a fault target):
  https://apg-library.amazonaws.com/content/e3c357dd-b74c-415b-accb-4f5364baa843
- CRIS sharded-sticky-routing design doc (Mark's thread):
  https://code.amazon.com/packages/AmazonBedrockFrontendService/blobs/mainline/--/docs/sharded-sticky-routing/design.md
- Existing nearest prior art in this repo: `agentcore-strands-agent-faults/`
