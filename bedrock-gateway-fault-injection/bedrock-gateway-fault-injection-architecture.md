# Bedrock Gateway Fault Injection: Direct Provider-Target Architecture

## Decision

Host the provider-agnostic fault-injection proxy on a reachable container runtime. ECS with Fargate is the reference AWS runtime, but the experiment is not coupled to ECS: the same container can run behind LiteLLM on EC2, ECS, or EKS.

The normal production path is the gateway's direct Bedrock integration. During an experiment, the gateway's upstream/provider endpoint is temporarily changed to the proxy. The proxy implements the same provider contract, returns a deterministic fault when a rule matches, and forwards non-faulted requests to the real Bedrock Runtime API.

## Gateway parameterization

`GatewayType` identifies the gateway integration being tested:

| GatewayType | Normal production path | Experiment change |
|---|---|---|
| `agentcore` | AgentCore inference provider target -> Bedrock | Change the provider target endpoint to the proxy HTTPS endpoint |
| `llm-proxy-ec2` | LiteLLM on EC2 -> Bedrock | Change the LiteLLM model deployment's upstream/API base to the proxy |
| `llm-proxy-ecs` | LiteLLM on ECS -> Bedrock | Change the LiteLLM model deployment's upstream/API base to the proxy |
| `llm-proxy-eks` | LiteLLM on EKS -> Bedrock | Change the LiteLLM model deployment's upstream/API base to the proxy |

`GatewayType` documents the setup and test context. FIS and SSM do not change the gateway configuration automatically. The gateway owner performs the temporary endpoint change and restores the direct Bedrock endpoint after the test.

## LiteLLM connection and insertion design

LiteLLM's direct Bedrock provider uses boto3 and its runtime workload identity. The normal model entry is `bedrock/<model-id>` or an explicit `bedrock/converse/<model-id>` route. Adding `api_base` to that entry does not turn the boto3 Bedrock call into an HTTP proxy call.

The minimal fault lane uses LiteLLM's documented OpenAI-compatible provider route instead:

```text
Application -> LiteLLM /chat/completions
           -> model alias -> openai/<model-id>
           -> HTTPS fault proxy /v1/chat/completions
           -> fresh SigV4 request from proxy workload role
           -> Bedrock Runtime
```

The application-facing LiteLLM URL and model alias remain unchanged. The temporary deployment entry changes only `model` from `bedrock/...` to `openai/...`, adds `api_base=https://<proxy>/v1`, and supplies a proxy bearer key. The proxy validates that key, applies the SSM rule, and signs its own Bedrock request. The proxy key and LiteLLM control-plane key are separate secrets.

EC2, ECS, and EKS do not change this request contract. They change how LiteLLM reaches the proxy and how its proxy key is delivered: instance secret/network controls on EC2, task secret and service networking on ECS, and Kubernetes/external Secret plus Service/NetworkPolicy on EKS. The LiteLLM workload retains Bedrock IAM for the direct rollback lane; the proxy workload owns Bedrock IAM in the temporary lane. See `litellm-test/` for the shared configuration and smoke client.

## Request path

```text
Normal production:
Application -> gateway -> direct Bedrock integration -> model

Experiment window:
Application -> same gateway -> proxy provider/upstream -> Bedrock Runtime -> model

FIS -> SSM Automation -> SSM Parameter Store -> proxy configuration
```

For AgentCore, the proxy is a direct inference-provider target endpoint. For LiteLLM, the proxy is the configured model deployment upstream. The optional hardened edge variant may add API Gateway IAM, VPC Link, and a private ALB in front of the proxy, but those components are not required by the core experiment.

The FIS control path is separate from the request path:

```text
FIS experiment
    -> SSM Automation
    -> SSM Parameter Store: /bedrock-fis/{InjectorId}/*
    -> ECS/Fargate proxy reads a short-lived cached configuration
```

## Runtime and endpoint requirements

ECS/Fargate is the recommended AWS proving runtime because it supplies task roles and a managed container lifecycle. LiteLLM customers may run the same image alongside LiteLLM on EC2, ECS, or EKS. In every case, the proxy must have:

- An HTTPS endpoint reachable by the gateway.
- The gateway's provider/upstream request contract.
- A task or workload role that can read the selected SSM namespace and invoke approved Bedrock resources.
- A safe authentication design for any restricted or customer-facing traffic.

An unauthenticated public endpoint is limited to synthetic non-production proving. API Gateway IAM, VPC Link, and a private ALB are optional edge-hardening components, not a requirement of AgentCore inference targets or LiteLLM.

## Target configuration

The normal production target remains direct:

```text
AgentCore inference provider target -> Bedrock Runtime
LiteLLM model deployment -> Bedrock Runtime
```

During a controlled experiment window, the gateway owner changes only the gateway's provider/upstream endpoint:

```text
AgentCore inference provider target -> HTTPS proxy -> Bedrock Runtime
LiteLLM model deployment -> HTTPS/private proxy -> Bedrock Runtime
```

The application-facing gateway endpoint and request contract remain unchanged. The FIS experiment changes only SSM fault state; it does not modify AgentCore, LiteLLM, EC2, ECS, or EKS configuration. Rollback restores the original direct Bedrock endpoint.

## Proxy behavior

For every request, the proxy extracts the model or inference-profile identifier and evaluates the ordered SSM rules:

1. If `active=false`, no rule is applied.
2. If a model rule matches and its sampled rate fires, the proxy returns the configured Bedrock-shaped 503, 429, or 529 response without calling Bedrock.
3. Otherwise, the proxy creates a fresh SigV4 request with the ECS task role and calls Bedrock Runtime.
4. The proxy translates the Bedrock response to the response contract expected by the AgentCore inference target.

The proxy never forwards the AgentCore signature to Bedrock. The two signatures authenticate different hops and cover different URLs.

## Authentication and authorization

| Hop | Authentication | Authorization boundary |
|---|---|---|
| Application -> gateway | Existing AgentCore or LiteLLM application authentication | The gateway's existing caller policy |
| AgentCore -> proxy | The configured AgentCore outbound credential provider, such as `GATEWAY_IAM_ROLE` | The proxy's approved HTTPS authentication edge; validate the caller for restricted traffic |
| LiteLLM -> proxy | The existing service-to-service mechanism for the EC2, ECS, or EKS deployment | The proxy edge and network policy |
| Proxy -> Bedrock Runtime | Fresh SigV4 using the ECS task role or the equivalent workload role | `bedrock:InvokeModel` and, if streaming, `bedrock:InvokeModelWithResponseStream` scoped to approved model/profile ARNs |
| FIS/SSM -> SSM parameters | IAM role assumption | Parameter-path-scoped SSM permissions under `/bedrock-fis/{InjectorId}/*` |

The core experiment must not assume that an ALB or a network boundary authenticates the caller. For restricted or production traffic, use the approved service-to-service authentication edge. API Gateway IAM plus VPC Link is one optional implementation; it is not the default AgentCore architecture.

## Fault controls

The experiment uses one namespace per proxy:

```text
/bedrock-fis/{InjectorId}/active
/bedrock-fis/{InjectorId}/fault_type
/bedrock-fis/{InjectorId}/fault_rate
/bedrock-fis/{InjectorId}/latency_ms
/bedrock-fis/{InjectorId}/rules
/bedrock-fis/{InjectorId}/execution_id
```

SSM Automation writes all values first and `active=true` last. On completion, cancellation, or failure it writes `active=false` before cleanup. The proxy caches configuration for a bounded TTL and fails open to real Bedrock pass-through if SSM is unavailable.

For independent concurrent campaigns, use a separate proxy and `InjectorId` per campaign. Multiple writers must not update one shared `rules` parameter because the last writer would replace the complete rule set.

## Observability

The design should emit and correlate:

- Gateway request and target metrics for the selected `GatewayType`.
- The proxy runtime's access logs and target health.
- ECS, EC2, or EKS workload logs with model, selected rule, execution ID, and upstream status.
- Bedrock CloudWatch metrics and CloudTrail model invocation events.
- `x-fault-injected`, `x-fault-model`, `x-fault-matched-pattern`, and `x-fault-execution-id` on injected responses.

## Rollback

Rollback order is:

1. Set the proxy's `active` parameter to `false` or stop the FIS experiment.
2. Wait at least one proxy configuration-cache TTL.
3. Confirm real Bedrock completions through the selected gateway.
4. Restore the selected gateway's direct Bedrock provider/upstream endpoint if the proxy is no longer required.
5. Keep the proving resources available until the test owner confirms testing is complete.

No cleanup or resource deletion is part of the experiment runbook. Resource teardown requires a separate explicit operator decision.

## Live non-production proof

The core direct-target implementation was validated in a dedicated non-production AWS account and Region using a separate AgentCore Gateway whose inference provider endpoint was the HTTPS ECS/Fargate proxy endpoint. No API Gateway, VPC Link, or private ALB was used in this target path. A no-fault request returned a real Bedrock completion; SSM-selected rules returned 503, 429, and 529 through the direct provider target. The public proving service was restored to echo mode after testing, and no resources were deleted.

An optional hardened-edge variant was also validated with an API Gateway HTTP API, VPC Link, private ALB, and ECS service. That variant proved IAM edge denial, private routing, real completions, the fault matrix, and recovery. It is not required for the core experiment.

## Non-goals and limitations

This design does not transparently intercept an unchanged managed AgentCore-to-Bedrock connection. AgentCore does not expose a user-controlled egress hook for arbitrary HTTP fault responses. The test lane is therefore a controlled AgentCore target substitution that preserves the application-facing Gateway endpoint and exercises the same request, retry, fallback, and recovery behavior.

The proxy must not be deployed in forward mode behind an unauthenticated public edge for customer or production traffic. A public edge may be used for synthetic non-production proving only, or replace it with the optional authenticated edge variant.
