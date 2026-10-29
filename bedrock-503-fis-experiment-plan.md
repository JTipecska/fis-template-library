# bedrock-gateway-fault-injection — direct gateway egress fault-injection experiment plan

Goal: give Mark a demo and test harness that injects Bedrock `ServiceUnavailableException` (503), `ThrottlingException` (429), and the 529 overloaded signal into a Bedrock-calling workload behind a gateway. The experiment must prove that no-fault requests reach real Bedrock, faulted requests receive deterministic responses through the same application-facing gateway, and the application's retry, fallback, region-spread, and error-translation logic behaves as intended.

Scope decision (2026-10-22): support `GatewayType=agentcore`, `GatewayType=llm-proxy-ec2`, `GatewayType=llm-proxy-ecs`, and `GatewayType=llm-proxy-eks`. The normal production path for every type is direct gateway-to-Bedrock integration. During the experiment, the gateway owner temporarily changes the provider/upstream endpoint to the HTTPS proxy. Direct SDK-to-Bedrock DNS or endpoint interception remains out of scope.

## Why the proxy is required

The gateway is the control point already present in the application path. The application does not change its gateway URL. The gateway owner changes the provider/upstream endpoint from Bedrock to the proxy for the experiment window; the proxy implements the same request contract and forwards no-fault traffic to real Bedrock.

FIS does not directly modify AgentCore, LiteLLM, EC2, ECS, or EKS configuration and does not provide a native Bedrock fault action. Its role is orchestration: start and stop SSM Automation, enforce the CloudWatch stop condition, and guarantee that the proxy's fault controls are disabled on completion or rollback.

## GatewayType setup matrix

| GatewayType | Runtime setup | Temporary experiment change |
|---|---|---|
| `agentcore` | AgentCore inference provider target points directly to Bedrock | Change the provider target endpoint to the proxy HTTPS endpoint |
| `llm-proxy-ec2` | LiteLLM model deployment points directly to Bedrock | Change `api_base` or the model upstream to the proxy |
| `llm-proxy-ecs` | LiteLLM model deployment points directly to Bedrock | Change `api_base` or the model upstream to the proxy |
| `llm-proxy-eks` | LiteLLM model deployment points directly to Bedrock | Change `api_base` or the model upstream to the proxy |

`GatewayType` documents the test context and selects the setup instructions. The gateway owner performs the endpoint change and rollback; FIS changes only the proxy's SSM state.

## LiteLLM connection and minimal proxy insertion

LiteLLM's direct Bedrock provider uses boto3 and the LiteLLM workload identity. The direct deployment uses `bedrock/<model-id>` or `bedrock/converse/<model-id>`. The fault lane does not try to intercept that boto3 client. Instead, the same LiteLLM model alias is temporarily configured as an OpenAI-compatible deployment:

```yaml
model_list:
  - model_name: bedrock-test
    litellm_params:
      model: openai/<BEDROCK_MODEL_ID>
      api_base: https://<PROXY HTTPS ENDPOINT>/v1
      api_key: os.environ/FAULT_PROXY_API_KEY
```

LiteLLM then sends `POST /v1/chat/completions` to the proxy. The proxy validates the bearer key, applies the SSM rule, and signs a separate Bedrock request with its own workload identity for non-faulted calls. The application URL and model alias do not change. EC2, ECS, and EKS use the same LiteLLM configuration; only network reachability, secret delivery, and the direct-lane workload identity differ. The portable configs and smoke client are in `bedrock-gateway-fault-injection/litellm-test/`.

The LiteLLM workload keeps its existing Bedrock permission for direct rollback. It does not need a new Bedrock permission solely for the proxy lane. The proxy workload needs the SSM read and scoped Bedrock invoke permissions.

## Why the egress proxy is required

The application should not change its Gateway URL or request contract. The controlled change is the Gateway's provider/upstream target for the experiment window. For AgentCore, the direct inference-provider endpoint changes to the proxy. For LiteLLM, the model deployment temporarily changes from `bedrock/...` to `openai/<model-id>` with `api_base=https://<proxy>/v1` and a proxy bearer key. The proxy either injects a fault or creates a fresh task-role-authenticated Bedrock request. API Gateway, VPC Link, and a private ALB are optional edge controls only; they are not part of the core flow.

FIS does not directly modify AgentCore or provide a native Bedrock fault action. Its role is orchestration: start and stop SSM Automation, enforce the CloudWatch stop condition, and guarantee that the proxy's fault controls are disabled on completion or rollback.

## Which errors, and why
Per the official Bedrock reactive wording (Harbinger, 2026-10-03), the transient errors a resilient
caller is told to retry are **HTTP 503 and 529**, with backoff+jitter and an attempt cap; 429
`ThrottlingException` is the capacity/quota signal. The injector emits:
- `ServiceUnavailableException` — HTTP 503 (primary ask)
- HTTP 529 — "overloaded" transient signal called out alongside 503
- `ThrottlingException` — HTTP 429 (quota/capacity pressure, the CRIS pinning failure mode)
- optional: `LatencyMs` pre-error delay, to exercise timeouts / cooling-off

## Architecture (as designed)

```text
Normal production:
Application -> gateway -> direct Bedrock integration -> model

Experiment window:
Application -> same gateway -> HTTPS proxy -> Bedrock Runtime -> model

FIS -> SSM Automation -> SSM Parameter Store
       /bedrock-fis/{InjectorId}/*
```

- ECS/Fargate is the reference AWS runtime for the proxy; the same container can run alongside LiteLLM on EC2, ECS, or EKS.
- AgentCore uses a direct inference provider target whose endpoint is temporarily changed to the proxy.
- LiteLLM changes the model deployment's upstream or `api_base` temporarily.
- The proxy creates a new SigV4 Bedrock request; it never forwards the gateway's inbound signature.
- The proxy fails open to real Bedrock when inactive or when SSM is unavailable.
- Faulted responses carry `x-fault-injected`, `x-fault-model`, `x-fault-matched-pattern`, and `x-fault-execution-id`.
- API Gateway IAM, VPC Link, and private ALB are optional edge hardening, not required by the core experiment.

## Wiring per gateway (no application change)

- **AgentCore Gateway:** direct production target -> Bedrock. During the experiment, set the provider target endpoint to the proxy HTTPS endpoint. Configure `GET /v1/models` and the same inference operation paths as the direct provider target.
- **LiteLLM on EC2:** change the model deployment's `api_base` or upstream URL to the proxy. Keep the proxy reachable from the EC2 instance and use the existing approved service-to-service authentication.
- **LiteLLM on ECS:** change the model deployment's `api_base` or upstream URL to the proxy. Use the ECS service network and task identity already approved for the deployment.
- **LiteLLM on EKS:** change the model deployment's `api_base` or upstream URL to the proxy. Use the cluster's approved service-to-service authentication and network policy.

## Files (as designed)
```
bedrock-gateway-fault-injection/
  README.md                                                     # gold-standard structure; authenticated AgentCore and LiteLLM wiring
  bedrock-gateway-fault-injection-architecture.md              # authenticated request-path architecture
  bedrock-gateway-fault-injection-security-design.md            # trust boundaries and security controls
  AWSFIS.json
  bedrock-gateway-fault-injection-experiment-template.json      # FIS: start SSM automation + CW-alarm stop condition
  bedrock-gateway-fault-injection-automation.yaml               # SSM doc: enable -> hold -> disable, with rollback
  bedrock-gateway-fault-injection-fis-role-iam-policy.json
  bedrock-gateway-fault-injection-ssm-automation-role-iam-policy.json
  bedrock-gateway-fault-injection-injector-task-role-iam-policy.json
  bedrock-gateway-fault-injection-agentcore-gateway-role-iam-policy.json
  fis-iam-trust-relationship.json
  ssm-iam-trust-relationship.json
  injector/
    app.py                                                      # provider-agnostic fault injector
    Dockerfile
    requirements.txt
```
Note on the three IAM policies: the STYLE_GUIDE shows a single `{experiment}-iam-policy.json`, but
this experiment has three distinct principals (the FIS role, the SSM Automation role, and the
injector's container task role), so it ships one least-privilege policy per principal — the same
multi-policy pattern several existing experiments use (e.g. `database-io-exhaustion`).

## Parameters (automation documentParameters)

`GatewayType` identifies the gateway setup being tested: `agentcore`, `llm-proxy-ec2`, `llm-proxy-ecs`, or `llm-proxy-eks`. It documents the setup context; the gateway owner still performs the temporary endpoint change and rollback.

`InjectorId` selects the injector's SSM namespace. For example, the live proving environment uses
`INJECTOR_ID=ecs-fargate`, so the parameters live under `/bedrock-fis/ecs-fargate/`. A new
installation can choose another value, but the container environment variable, Automation
`InjectorId`, and SSM path must all match.

- `GatewayType` — `agentcore` | `llm-proxy-ec2` | `llm-proxy-ecs` | `llm-proxy-eks`; setup context only
- `FaultType` — `503` | `429` | `529`
- `FaultRate` — 0.0–1.0 share of requests faulted
- `LatencyMs` — optional pre-error delay (ms)
- `Rules` — optional ordered JSON list of `{model_pattern, fault_type, fault_rate, latency_ms}`; first match wins
- `DurationSeconds` — active window before clean disable
- `InjectorId` — scopes the SSM subtree; must equal the injector `INJECTOR_ID` env var
- `AutomationAssumeRole` — role assumed by SSM Automation to write and clean up the parameters

## Verification boundary

The core proving setup uses a gateway-specific temporary endpoint substitution:

- `agentcore`: the inference provider target changes from direct Bedrock to the proxy HTTPS endpoint.
- `llm-proxy-ec2`, `llm-proxy-ecs`, or `llm-proxy-eks`: the model deployment's upstream or `api_base` changes from direct Bedrock to the proxy.

The forward-mode proxy uses its workload role to call Bedrock Runtime with a fresh SigV4 signature. For AgentCore, the proxy exposes `/v1/models` and the configured inference operation paths. A no-fault request must return a real HTTP 200 completion; a deterministic per-model SSM rule matrix must return 503, 429, and 529 through the same selected gateway; and `active=false` must restore real Bedrock completions.

The six-model authenticated API Gateway matrix already proves the proxy contract, model discovery, real Bedrock forwarding, fault selection, and recovery. That API Gateway/VPC Link/private ALB path is an optional hardened-edge variant, not a requirement for the core AgentCore or LiteLLM experiment.

The test does not claim to modify an unchanged direct AgentCore-to-Bedrock connection. It tests the gateway's documented provider/upstream target substitution while preserving the application-facing gateway contract.

All proving resources remain available until the test owner confirms testing is complete. Resource deletion is not part of this experiment.

## Stop conditions & safety
- CloudWatch alarm stop condition on the app's own error rate / p99 latency; do not run open-loop.
- Non-prod account only unless the application/gateway owner and security review approve the endpoint change.
- The selected gateway must use an approved HTTPS/authentication edge for restricted traffic.
- Guaranteed fault rollback: the SSM doc writes `active=false` first and then deletes parameters on the happy path and on cancel/failure.
- A cancelled run leaves the proxy in clean pass-through; gateway rollback from proxy to direct Bedrock is a separate controlled configuration change.

## Observability (what the demo should show)
- Client/gateway-side: retry count, success-after-retry, fallback-model or second-profile invocations, and end-to-end latency.
- Selected gateway and proxy runtime metrics/logs without request bodies, prompts, or secrets.
- Bedrock CloudWatch metrics and CloudTrail model invocation events.
- `x-fault-injected`, `x-fault-model`, `x-fault-matched-pattern`, and `x-fault-execution-id` headers.

## Verification status
- Static: `app.py` compiles; all JSON valid; automation YAML valid.
- Functional: injector fault, fail-open, health, model-rule, and `/v1/models` discovery behavior are implemented and validated.
- Optional hardened-edge live proof: API Gateway unsigned denial (403), AgentCore signed access, private VPC Link/ALB/ECS routing, six real Bedrock completions, deterministic 503/429/529 model-family matrix, and six-model recovery after deactivation are verified.
- Core direct-target live proof: a separate AgentCore Gateway direct inference provider target pointed at the HTTPS ECS/Fargate proxy. With the proxy in forward/Converse mode, all six model families returned deterministic mapped 503/429/529 responses, and the same six requests returned real Bedrock 200 `chat.completion` responses after deactivation. The service was restored to task definition revision 8 and the original inactive SSM rules after testing.
- LiteLLM live proof: pinned LiteLLM `v1.106.2` was deployed separately on private EC2 (`t4g.small`), ECS/Fargate ARM64, and EKS 1.33 Fargate. Each runtime used the OpenAI-compatible `api_base` configuration against the private proxy. Each returned six real Bedrock 200 completions, propagated the six-model 503/429/529 matrix as LiteLLM HTTP status/body error codes, and recovered all six to real 200 completions. LiteLLM stripped the proxy's custom fault headers, so header assertions belong at the proxy boundary.
- Runtime-specific findings: ECS required explicit ARM64 task definitions and GPT-6.1 destination-region foundation-model permissions. EKS Fargate required x86_64 image variants and explicit LiteLLM memory sizing to avoid startup OOM. EC2 used a private ARM64 instance with SSM-only management and no public IP. The shared private proxy used bearer authentication over the VPC path; because the account had no ACM certificate, this is a non-production proving shape and must be replaced with an approved private HTTPS edge for restricted or production use.
- Not yet verified: starting the SSM Automation through a registered FIS experiment in this account. The live matrix used direct SSM writes to isolate and prove the request path; the repository automation and FIS templates remain separately validated as configuration files.
