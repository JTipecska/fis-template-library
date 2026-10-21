# AWS Fault Injection Service Experiment: Bedrock Gateway Fault Injection (LiteLLM and AgentCore Gateway)

This is an experiment template for use with AWS Fault Injection Service (FIS) and fis-template-library-tooling. It injects Bedrock-accurate transient errors — HTTP 503 `ServiceUnavailableException`, 429 `ThrottlingException`, and 529 `overloaded_error` — into the upstream calls made by an LLM gateway in front of Amazon Bedrock, so you can validate that the gateway's and the application's retry, fallback, and error-translation logic behave as intended. It targets two gateway shapes: self-hosted **LiteLLM** and AWS-managed **Amazon Bedrock AgentCore Gateway**.

THIS TEMPLATE WILL INJECT REAL FAULTS! THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Hypothesis

When a configurable share of our Bedrock calls return transient 503 / 429 / 529 errors, our gateway and application remain available: retries with backoff succeed, fallbacks to an alternative model or geographic inference profile engage, and users do not see hard failures. Cross-region inference (CRIS) does not by itself move traffic off an impaired placement — our own resilience logic is what recovers the request.

## How it works

The fault is injected **without changing the calling application**. The application keeps calling its gateway exactly as before. Only the gateway's *upstream endpoint* is pointed at a small provider-agnostic injector container for the duration of the experiment. The injector reads its on/off state from AWS SSM Parameter Store (`/bedrock-fis/{InjectorId}/*`), which an FIS-orchestrated SSM Automation document toggles with guaranteed rollback.

```
application --> gateway (LiteLLM or AgentCore) --> [injector] --> Amazon Bedrock
                                                      ^
                          FIS -> SSM Automation -> SSM Parameter Store
                          (/bedrock-fis/{InjectorId}/active, fault_rate, fault_type, ...)
```

The injector **fails open**: while inactive, or if SSM is unreachable, every request passes through untouched. SSM can only *add* faults, never cause an outage of its own. Each faulted response carries `x-fault-injected` and `x-fault-execution-id` headers for correlation.

### Files

| File | Purpose |
|---|---|
| `bedrock-gateway-fault-injection-experiment-template.json` | FIS experiment template |
| `bedrock-gateway-fault-injection-automation.yaml` | SSM Automation document (enable → hold → disable, with rollback) |
| `bedrock-gateway-fault-injection-fis-role-iam-policy.json` | IAM policy for the FIS experiment role |
| `bedrock-gateway-fault-injection-ssm-automation-role-iam-policy.json` | IAM policy for the SSM Automation role |
| `bedrock-gateway-fault-injection-injector-task-role-iam-policy.json` | IAM policy for the injector container's task role (reads its own SSM subtree) |
| `fis-iam-trust-relationship.json` / `ssm-iam-trust-relationship.json` | Trust policies for the above roles |
| `injector/` | The provider-agnostic injector container (`app.py`, `Dockerfile`, `requirements.txt`) |

## Prerequisites

Before running this experiment, ensure that:

1. You have the necessary permissions to execute the FIS experiment and to run SSM Automation.
2. The IAM role in the experiment template `roleArn` has the permissions in `bedrock-gateway-fault-injection-fis-role-iam-policy.json`, and the SSM Automation role has those in `bedrock-gateway-fault-injection-ssm-automation-role-iam-policy.json`.
3. You have built and deployed the injector container (see **Deploy the injector**) somewhere your gateway can reach it, with its task role granting the policy in `bedrock-gateway-fault-injection-injector-task-role-iam-policy.json`.
4. Your gateway (LiteLLM or AgentCore Gateway) is wired to the injector (see **Wire your gateway**).
5. You are running in a non-production account, and you have a CloudWatch alarm on your application's error rate or latency to use as a stop condition.

## Deploy the injector

The injector is a small single-process container that runs anywhere a container runs — ECS/Fargate, EKS, EC2, or outside AWS. Build and push it:

```bash
cd injector
docker build -t <YOUR REGISTRY>/bedrock-gateway-fault-injector:latest .
docker push <YOUR REGISTRY>/bedrock-gateway-fault-injector:latest
```

Run it with these environment variables:

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `INJECTOR_ID` | yes | — | Scopes the `/bedrock-fis/{INJECTOR_ID}/` SSM subtree; must equal the automation's `InjectorId`. |
| `PASSTHROUGH_MODE` | no | `forward` | `forward` proxies non-faulted requests to `UPSTREAM_BASE_URL`; `echo` returns a synthetic success (pure drill, no real upstream). |
| `UPSTREAM_BASE_URL` | when `forward` | — | The real upstream the gateway would otherwise call (e.g. the Bedrock OpenAI-compatible endpoint). |
| `RESPONSE_STYLE` | no | `aws` | `aws` emits Bedrock-native `{"__type","message"}` errors; `openai` emits `{"error":{...}}` for OpenAI-compatible gateways. |
| `CONFIG_TTL_SECONDS` | no | `10` | SSM config cache TTL. |
| `UPSTREAM_TIMEOUT` | no | `60` | Upstream forward timeout (seconds). |

The injector's task role needs only read access to its own SSM subtree (`bedrock-gateway-fault-injection-injector-task-role-iam-policy.json`).

## Wire your gateway

You only change the gateway's upstream endpoint for the duration of the experiment. The application is untouched.

### Option 1 — LiteLLM (self-hosted)

Point a model deployment's `api_base` at the injector. In `config.yaml`:

```yaml
model_list:
  - model_name: bedrock-claude
    litellm_params:
      model: bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0
      # Normally the Bedrock endpoint; for the experiment, the injector:
      api_base: http://<INJECTOR HOST>:8080
```

Run the injector with `RESPONSE_STYLE=aws` and `PASSTHROUGH_MODE=forward`, `UPSTREAM_BASE_URL` set to the real Bedrock endpoint LiteLLM would have called. Injecting a 503 **upstream of LiteLLM** lets you verify LiteLLM correctly propagates the retryable error to the client (LiteLLM has had known bugs translating Bedrock 503/429 into its OpenAI/Anthropic error envelope, so this is a meaningful check).

Alternatively, for a drill that does not need a real upstream, set `PASSTHROUGH_MODE=echo`.

### Option 2 — Amazon Bedrock AgentCore Gateway (AWS-managed)

You cannot modify the managed gateway itself, so inject *behind* it: register an inference-provider target (or an HTTP passthrough target) whose endpoint is the injector, and have the injector forward to the real provider. Set `RESPONSE_STYLE=openai` (AgentCore inference targets are OpenAI-compatible on `/v1/chat/completions`) and `PASSTHROUGH_MODE=forward` with `UPSTREAM_BASE_URL` set to the real provider endpoint. The application keeps calling the same AgentCore Gateway endpoint — only the target's upstream is the injector.

Concretely, the inference provider target's `endpoint` is set to the injector (it must be a public HTTPS URL with a valid certificate), and the gateway authenticates its upstream calls with its own IAM role (`GATEWAY_IAM_ROLE` credential provider):

```bash
# 1. Create the gateway (authorizer NONE for a drill; use CUSTOM_JWT/AWS_IAM in real use).
aws bedrock-agentcore-control create-gateway \
  --name my-fis-gateway --role-arn arn:aws:iam::<ACCT>:role/<GATEWAY ROLE> \
  --authorizer-type NONE --protocol-type MCP --region <REGION>

# 2. Attach an inference provider target whose endpoint is the injector.
#    target-configuration.json:
#    { "inference": { "provider": {
#        "endpoint": "https://<INJECTOR PUBLIC HTTPS>/v1",
#        "operations": [ { "path": "/v1/chat/completions",
#                          "models": [ { "model": "anthropic.claude-3-5-sonnet-*" } ] } ] } } }
#    credential-provider-configurations.json: [ { "credentialProviderType": "GATEWAY_IAM_ROLE" } ]
aws bedrock-agentcore-control create-gateway-target \
  --gateway-identifier <GATEWAY ID> --name inj-inference-target \
  --target-configuration file://target-configuration.json \
  --credential-provider-configurations file://credential-provider-configurations.json \
  --region <REGION>
```

The gateway's IAM role needs `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream`, and the `bedrock:List*` / `bedrock:Get*` discovery actions it uses to enumerate models from the endpoint on target creation.

> **Verification note (2026-10-21, account 291534835833, us-east-1).** This path was validated against the live `bedrock-agentcore-control` API: a gateway plus an `inference.provider` target with a `GATEWAY_IAM_ROLE` credential provider was created and reached `READY`, with the gateway successfully performing model discovery against the configured endpoint using its IAM role (the target first failed with `403 bedrock:ListFoundationModels` / `ListProvisionedModelThroughputs` until those actions were granted — confirming the gateway really does make the outbound calls the injector would intercept). Pointing the target `endpoint` at a live public-HTTPS injector and observing injected 503/429/529 end-to-end was **not** completed here because it requires the injector published at a public HTTPS endpoint with a valid certificate; the injector's own SSM-driven 503/429/529 behaviour is separately proven (see the injector smoke tests). All test gateway resources were torn down after verification.

## Configure and run the experiment

1. Create the SSM Automation document from `bedrock-gateway-fault-injection-automation.yaml`.
2. In `bedrock-gateway-fault-injection-experiment-template.json`, replace the placeholders (`<YOUR REGION>`, `<YOUR AWS ACCOUNT>`, `<YOUR AUTOMATION DOCUMENT NAME>`, `<YOUR INJECTOR ID>`, role and alarm ARNs). The `documentParameters` control the fault:
   - `FaultType` — `503` | `429` | `529`
   - `FaultRate` — probability in `[0.0, 1.0]` of faulting each request
   - `LatencyMs` — optional delay before the error (exercises timeouts/cooling-off)
   - `DurationSeconds` — how long faults stay active before clean disable
3. Import and run the experiment template using the [FIS Template Library Tooling](https://github.com/aws-samples/fis-template-library-tooling).

## Tuning the fault parameters

The fault is controlled by five SSM parameters under `/bedrock-fis/{InjectorId}/`, which the SSM Automation document writes from its `documentParameters`. You can also set them directly with `aws ssm put-parameter` for ad-hoc testing. The injector re-reads them every `CONFIG_TTL_SECONDS` (default 10), so a change takes effect within one TTL.

| SSM parameter | `documentParameters` key | Values | Meaning |
|---|---|---|---|
| `active` | (set by the automation) | `true` \| `false` | Master switch. Written `true` last on enable, `false` first on disable. |
| `fault_type` | `FaultType` | `503` \| `429` \| `529` | Which error to emit. |
| `fault_rate` | `FaultRate` | `0.0`–`1.0` | Probability each request is faulted. |
| `latency_ms` | `LatencyMs` | integer ≥ 0 | Delay (ms) applied before the error. |
| `execution_id` | (set by the automation) | string | Correlation id, echoed in the `x-fault-execution-id` header. |

### Which error to inject, and when

| Fault | HTTP | Error shape (`aws` style) | Real-world meaning | Inject it to test |
|---|---|---|---|---|
| **503** | 503 | `ServiceUnavailableException` | Service temporarily out of capacity on shared on-demand infra. The official Bedrock guidance says **retry 503 with exponential backoff + jitter**. | The primary case. That your retry path backs off and recovers, and that a gateway (LiteLLM/AgentCore) propagates the 503 as retryable rather than collapsing it into a generic error. |
| **429** | 429 | `ThrottlingException` | Request exceeded an **account quota** (RPM/TPM) or rate limit. Carries `Retry-After`. | That you honour `Retry-After`, throttle your own send rate, and/or request a quota increase path — not that you hammer harder. |
| **529** | 529 | `overloaded_error` | Model backend temporarily **overloaded** (Anthropic-style); often surfaces to callers as a 503. Guidance pairs it with 503 for retry. | The same retry/backoff path as 503, plus that your client recognises the less-common 529 code. |

Recommended coverage: **always run 503 first** (it is the dominant transient signal and the one customers actually hit), then 429, then 529. 503 and 529 should trigger the same retry/backoff behaviour; 429 should trigger rate-limiting/`Retry-After` behaviour — if your app treats 429 the same as 503 and just retries immediately, that is a finding.

### Rate, latency, and sequencing

- **`fault_rate`** — start low (`0.1`) to observe partial degradation and confirm the healthy fraction still flows, then raise toward `1.0` for a full-outage drill of the pinned placement. `1.0` is the right value to prove a specific error path deterministically; `0.2`–`0.5` is more realistic for a capacity event.
- **`latency_ms`** — set non-zero (e.g. `2000`–`5000`) to exercise client/gateway **timeouts and cooling-off** before the error returns; leave `0` to test pure error handling. Keep it below your client's timeout if you want the error observed, or above it if you specifically want to trigger timeout handling.
- **Duration and sequencing between fault types** — the automation runs a single `FaultType` for `DurationSeconds`, then cleanly disables. To rehearse a **worsening or shifting** incident (e.g. 429 → 503 → 529, or escalating rate), chain several `aws:ssm:start-automation-execution` actions in the FIS template with `aws:fis:wait` actions between them, each invoking the automation with a different `FaultType`/`FaultRate` and a `startAfter` on the preceding wait. This mirrors the staged pattern used by `sqs-queue-impairment`. Keep each stage's `DurationSeconds` within the FIS action `maxDuration` (`PT1H`), and size the gaps (`aws:fis:wait`) to let your retries/alarms react between stages.

A single stage is enough for a basic drill. Use the multi-stage chain only when the demo needs to show behaviour as conditions change over time.

## Per-model-family faults (different HTTP status per model family)

A single running injector can return a **different fault for each model family at the same time**. Instead of (or in addition to) the global `fault_type`/`fault_rate`, write a `rules` parameter holding an ordered JSON list; for each request the injector reads the `model` field (from the JSON body, or the `/model/{id}/invoke` path on the Bedrock native API) and applies the **first rule whose `model_pattern` matches** (case-insensitive glob). A request whose model matches no rule is never faulted.

```bash
# anthropic.* -> 503, amazon.nova-* -> 429, *.titan-* -> 529 with 300ms latency, everything else passes through
aws ssm put-parameter --name /bedrock-fis/<InjectorId>/active --value true --type String --overwrite
aws ssm put-parameter --name /bedrock-fis/<InjectorId>/rules --type String --overwrite --value '[
  {"model_pattern":"anthropic.*",   "fault_type":"503","fault_rate":1.0},
  {"model_pattern":"amazon.nova-*", "fault_type":"429","fault_rate":1.0},
  {"model_pattern":"*.titan-*",     "fault_type":"529","fault_rate":1.0,"latency_ms":300}
]'
```

Each rule field: `model_pattern` (glob, required), `fault_type` (`503`/`429`/`529`, required), `fault_rate` (`0.0`–`1.0`, default `1.0`), `latency_ms` (default `0`). First match wins, so order from most specific to most general and put a trailing `{"model_pattern":"*", ...}` only if you want a catch-all.

Faulted responses carry `x-fault-model` (the model that was matched) and `x-fault-matched-pattern` (which rule fired) headers, alongside `x-fault-injected` and `x-fault-execution-id`, so you can see exactly why each request was faulted. If `rules` is absent, the injector uses the flat global `fault_type`/`fault_rate` for all models (backward compatible). `GET /injector-status` echoes the active rule set.

### Multiple parallel executions

Because each rule is scoped to a model-family pattern, several fault campaigns can run **concurrently against the same injector** without interfering — each owns a disjoint slice of model space. Two ways to drive this:

- **One `rules` list, many families (simplest).** Put every family's rule in the single `rules` parameter, as above. The injector evaluates all of them per request. This is one experiment that faults several families differently at once.
- **Several FIS experiments / SSM writers in parallel.** Give each team or experiment its **own injector** (its own `InjectorId` ⇒ its own `/bedrock-fis/{InjectorId}/*` subtree and its own gateway wiring), and run their FIS experiments simultaneously. Experiment A faults `anthropic.*` with 503 while experiment B faults `amazon.nova-*` with 429, each with independent start/stop/duration and its own stop-condition alarm. Use separate injectors (not one shared `rules` key) when the campaigns must start and stop independently, since concurrent writers to a single `rules` parameter would overwrite each other.

The demo value: show a multi-model application degrade **asymmetrically** — e.g. Claude traffic throws 503 and should fail over to a second profile, while Nova traffic throws 429 and should back off — proving each path's resilience independently and at the same time.

## The public endpoint

The injector has to be reachable by whatever calls it:

- For **LiteLLM self-hosted**, the injector only needs to be reachable from the LiteLLM process (same VPC/host is fine; no public exposure required).
- For **AgentCore Gateway**, the inference-target `endpoint` must be an `https://` URL the managed gateway can reach from the AWS network — in practice a **public HTTPS endpoint with a valid certificate**.

In the ECS Fargate reference setup, the public endpoint is an **Application Load Balancer (ALB)**:

- The ALB is `internet-facing` in two public subnets, listening on port 80 (add an HTTPS :443 listener with an ACM certificate for the AgentCore case, which requires `https://`).
- Its security group allows inbound from the callers you intend (in a locked-down drill, scope it to the gateway's egress rather than `0.0.0.0/0`).
- It forwards to an **IP target group** on container port 8080; ECS registers each Fargate task IP automatically.
- Target health is the injector's `GET /healthz` (always 200, never faulted), so the ALB only routes to live tasks.
- The Fargate tasks run in the same subnets with a security group that accepts traffic **only from the ALB security group** on 8080 — the tasks are not directly reachable, only via the ALB.

So the "public endpoint" is the ALB DNS name (e.g. `http://<name>.<region>.elb.amazonaws.com`); callers and gateways hit that, the ALB spreads traffic across healthy injector tasks, and the tasks read their per-family rules from SSM. This is why ECS Fargate + ALB is the recommended host over App Runner: the ALB gives a stable public endpoint you control (and can put a real cert on), task start-up is fast and transparent, and you can scale the task count behind the one endpoint.

## Injector image specification

The injector is a small, single-process HTTP service. Specs for running it on any container runtime:

| Spec | Value |
|---|---|
| Base image | `public.ecr.aws/docker/library/python:3.12-slim` |
| Runtime | Python 3.12, Flask + gunicorn (`gunicorn app:app`) |
| Listening port | `8080` (override with `PORT`) |
| Architecture | Build for the architecture you will run on. `linux/arm64` (e.g. Graviton / Fargate ARM) and `linux/amd64` are both supported — build natively for your target; cross-arch emulation is not required. |
| CPU / memory | 0.25 vCPU / 0.5 GB is ample; it is I/O-bound on the upstream forward. Scale `GUNICORN_WORKERS`/`GUNICORN_THREADS` (defaults 2 / 8) for higher concurrency. |
| User | Runs as non-root (uid 10001). |
| Health check | `GET /healthz` → `200` (never faulted, never forwarded). Dockerfile `HEALTHCHECK` already wired. |
| Status endpoint | `GET /injector-status` → current cached config (active, fault_type, fault_rate, latency_ms, execution_id) for observability. |
| AWS dependency | Read-only access to its own `/bedrock-fis/{INJECTOR_ID}/*` SSM parameters, via the task/instance role carrying `bedrock-gateway-fault-injection-injector-task-role-iam-policy.json`. Needs a region (`AWS_REGION`/`AWS_DEFAULT_REGION`) for the SSM client. |
| Fail-open | If SSM is unreachable or `active` is absent/false, every request passes through — the injector can only *add* faults, never cause an outage of its own. |
| Egress | In `forward` mode, outbound to `UPSTREAM_BASE_URL`. In `echo` mode, none. |

Build and run:

```bash
cd injector
# Build natively for your target architecture (example: arm64).
docker build --platform linux/arm64 -t bedrock-gateway-fault-injector:latest .

docker run -p 8080:8080 \
  -e INJECTOR_ID=<your-injector-id> \
  -e AWS_REGION=<your-region> \
  -e PASSTHROUGH_MODE=forward \
  -e UPSTREAM_BASE_URL=<the real upstream the gateway would call> \
  -e RESPONSE_STYLE=aws \
  bedrock-gateway-fault-injector:latest
```

Runtime options to pick a host: run it alongside/behind your gateway (ECS/Fargate task, EKS pod, or EC2). A managed public endpoint such as AWS App Runner also works but is not required and provisions more slowly; prefer ECS/Fargate or co-locating with the gateway for predictable start-up.

## Stop Conditions

This template references a placeholder CloudWatch alarm ARN for your application's error-rate or latency alarm as its stop condition; replace it with your own, or set the stop condition `source` to `none` to disable. Because the injector fails open, stopping the experiment (or the automation's rollback) returns the gateway to clean pass-through within the injector's cache TTL.

Stop conditions are based on an AWS CloudWatch alarm based on an operational or 
business metric requiring an immediate end of the fault injection. This 
template makes no assumptions about your application and the relevant metrics 
and does not include stop conditions by default.

## Next Steps
As you adapt this scenario to your needs, we recommend:
1. Creating CloudWatch alarms on your application's end-to-end error rate and p99 latency, and wiring one as the experiment stop condition.
2. Observing retry counts, success-after-retry, and fallback-model / second-profile invocations during the run.
3. Using CloudTrail `additionalEventData.inferenceRegion` to show which Region served each call, demonstrating that CRIS does not fail over off an impaired placement.
4. Starting at a low `FaultRate` and short `DurationSeconds`, then increasing as confidence grows.
5. Running `503` first (the primary transient signal), then `429` and `529`, to cover the full set of errors the official Bedrock guidance says to retry.

## Import Experiment
You can import the json experiment template into your AWS account via cli or aws cdk. For step by step instructions on how, [click here](https://github.com/aws-samples/fis-template-library-tooling).
