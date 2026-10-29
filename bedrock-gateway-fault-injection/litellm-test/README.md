# LiteLLM fault-proxy test setup

This directory contains the smallest repeatable LiteLLM test harness for the Bedrock gateway fault-injection experiment.

It does not change the application-facing LiteLLM URL or model alias. It tests the LiteLLM deployment by changing only the model deployment's upstream provider for a bounded test window:

```text
Normal:     application -> LiteLLM -> Bedrock
Test lane:  application -> LiteLLM -> OpenAI-compatible fault proxy -> Bedrock
```

The proxy is intentionally outside LiteLLM's Bedrock provider path. In the test lane LiteLLM uses its documented OpenAI-compatible provider route, while the proxy applies the SSM fault rule and, for non-faulted requests, signs a new Bedrock request with its own workload identity.

## Files

- `config.direct.yaml` — direct Bedrock configuration to save as the rollback baseline.
- `config.proxy.yaml` — temporary proxy configuration; the application-facing `model_name` is unchanged.
- `docker-compose.yaml` — local contract smoke setup using LiteLLM and the proxy in safe `echo` mode. It does not invoke Bedrock.
- `run-matrix.py` — runtime-independent smoke client for baseline, fault, and recovery phases.

## LiteLLM configuration change

LiteLLM's normal Bedrock deployment uses a `bedrock/...` model route and boto3 credentials. For the proxy lane, use `openai/<BEDROCK_MODEL_ID>` and set `api_base` to the proxy's OpenAI-compatible `/v1` base URL. The `api_key` is the proxy's inbound bearer secret; it is not an AWS credential.

```yaml
model_list:
  - model_name: bedrock-test
    litellm_params:
      model: openai/<BEDROCK_MODEL_ID>
      api_base: https://<PROXY HTTPS ENDPOINT>/v1
      api_key: os.environ/FAULT_PROXY_API_KEY
```

LiteLLM sends `POST /v1/chat/completions` to the proxy. The proxy strips that inbound bearer credential before forwarding and uses a fresh SigV4 signature for Bedrock. Therefore, the LiteLLM workload does not need `bedrock:InvokeModel` for the proxy lane; the proxy workload does. Keep the original LiteLLM Bedrock permissions for the direct rollback configuration.

Do not set `api_base` on a `bedrock/...` deployment and assume that boto3 Bedrock traffic will be routed through the proxy. The explicit `openai/` route is the supported seam for an OpenAI-compatible upstream.

## Runtime matrix

The LiteLLM YAML is the same on EC2, ECS, and EKS. Only these deployment concerns vary:

| Runtime | LiteLLM -> proxy network | LiteLLM secret delivery | LiteLLM AWS permissions in proxy lane | Proxy Bedrock permissions |
|---|---|---|---|---|
| EC2 | Private DNS or approved HTTPS route from the instance to the proxy | Instance environment or the approved host secret manager; do not commit the key | No new Bedrock permission; retain the existing instance-profile permission for direct rollback | Proxy workload role: SSM read plus scoped `bedrock:InvokeModel` and optional streaming action |
| ECS | Service-to-service route, security-group/NACL rules, and HTTPS | ECS task secret/environment injection | No new Bedrock permission; retain the existing task-role permission for direct rollback | Proxy ECS task role: SSM read plus scoped Bedrock invoke permissions |
| EKS | Kubernetes Service/Ingress or approved private HTTPS route; NetworkPolicy egress | Kubernetes Secret or an approved external secret provider | No new Bedrock permission; retain the existing IRSA permission for direct rollback | Proxy task/pod role: SSM read plus scoped Bedrock invoke permissions |

The runtime location does not change the fault rules, proxy API, FIS document, or Bedrock signing behavior. If LiteLLM is configured to assume a separate Bedrock role in its direct lane, its runtime identity additionally needs `sts:AssumeRole` on that role and the target role must trust it. That permission is not needed merely to call the proxy.

### Secret-delivery permissions

The proxy key is an application secret, not an IAM credential. If it is delivered from AWS Secrets Manager, the identity that injects the secret needs `secretsmanager:GetSecretValue` on that secret and `kms:Decrypt` only when a customer-managed KMS key encrypts it. If it is delivered from SSM Parameter Store, use `ssm:GetParameter` and the corresponding KMS decrypt permission. On ECS, this is normally the task execution/bootstrap identity used by the task definition's secret injection; on EKS it is the external-secrets/CSI identity or other approved secret delivery identity; on EC2 it is the instance or host bootstrap identity. The LiteLLM process itself only needs the resulting environment/file secret and HTTPS egress to the proxy.

## Test sequence

1. Save the direct model deployment from `config.direct.yaml` and record the exact rollback configuration.
2. Deploy the proxy with HTTPS, `INBOUND_AUTH_MODE=bearer`, a secret `PROXY_API_KEY`, and `RESPONSE_STYLE=openai`.
3. Configure LiteLLM with `config.proxy.yaml`, preserving the existing `model_name` alias used by the application.
4. Run `run-matrix.py --expected-status 200`. Require `object=chat.completion`; an echo response is not a real Bedrock proof.
5. Enable one deterministic SSM rule, for example `fault_rate=1.0` and `fault_type=503`, then run `run-matrix.py --expected-status 503`.
6. Repeat for 429 and 529 if the application has distinct handling for those statuses. LiteLLM preserves the HTTP status and includes the upstream error code in its JSON error body, but it may normalize or remove upstream `x-fault-*` and `Retry-After` headers. Assert the status and body code at the LiteLLM client boundary; assert fault headers directly at the proxy boundary.
7. Set `active=false`, wait at least the configured proxy cache TTL, and rerun the 200 check. Require a real Bedrock completion.
8. Restore the direct LiteLLM model entry and verify a direct Bedrock request before ending the change window.

The local Compose harness proves only LiteLLM-to-proxy contract and bearer authentication because it runs the proxy in `echo` mode. It must not be reported as a Bedrock invocation test. A real invocation requires the proxy workload identity, Bedrock model access, and the forward-mode environment described in the parent README.

Live hosted runtime validation was run in a dedicated non-production AWS account and Region with LiteLLM `v1.106.2` and the same six model aliases:

| Runtime | Baseline | Fault matrix | Recovery |
|---|---|---|---|
| EC2 `t4g.small` in private subnet | Six real HTTP 200 completions | 503/429/529 mapping passed | Six real HTTP 200 completions |
| ECS Fargate ARM64 service and matrix tasks | Six real HTTP 200 completions | 503/429/529 mapping passed | Six real HTTP 200 completions |
| EKS 1.33 Fargate pod | Six real HTTP 200 completions | 503/429/529 mapping passed | Six real HTTP 200 completions |

The runtime-specific deployment findings were: ECS required an explicit ARM64 task definition and GPT-6.1 required destination-region foundation-model permissions; EKS Fargate required x86_64 image variants and explicit LiteLLM memory sizing of 2–3 GiB to avoid startup OOM. LiteLLM preserved HTTP status and normalized error-body codes, but stripped the proxy's custom fault headers. The runtimes used private subnets and no public workload IPs. The private proxy hop used bearer authentication over the VPC path; the account had no ACM certificate, so this is a non-production proving shape and must be replaced with an approved private HTTPS edge before restricted or production use.

## Runtime-independent smoke client

Set the LiteLLM endpoint and key without putting secrets in this directory:

```bash
export LITELLM_BASE_URL=http://127.0.0.1:4000
export LITELLM_API_KEY=sk-test
python3 run-matrix.py --expected-status 200
```

For a fault phase, assert the HTTP status at the LiteLLM boundary. Do not require the proxy's custom headers after LiteLLM has processed the response:

```bash
python3 run-matrix.py --expected-status 503
```

To assert `x-fault-injected`, `x-fault-matched-pattern`, and `Retry-After`, call the proxy endpoint directly rather than going through LiteLLM.

The script sends one short, non-sensitive prompt and prints only status and response metadata. Use a non-production LiteLLM deployment and model during the test.

## Authoritative references

- [LiteLLM AWS Bedrock provider](https://docs.litellm.ai/docs/providers/bedrock) — boto3/IAM authentication and direct `bedrock/...` routes.
- [LiteLLM OpenAI-compatible endpoints](https://docs.litellm.ai/docs/providers/openai_compatible) — `openai/` model prefix, `api_base`, and upstream API key.
- [LiteLLM production deployment](https://docs.litellm.ai/docs/proxy/deploy) — ECS and EKS deployment differences.
- [Amazon Bedrock AgentCore inference targets](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-targets-inference.html) — the analogous direct-target model used by the other gateway type.
