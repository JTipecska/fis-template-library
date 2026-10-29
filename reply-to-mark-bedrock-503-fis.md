Draft reply to Mark — "any easy way to simulate Bedrock 503 errors with FIS?"

---

Short answer: there's no easy/managed way today. FIS has no Bedrock target, and the generic
API-error actions (inject-api-unavailable-error for 503, inject-api-throttle-error for 429) only
support the ec2 and kinesis namespaces — not bedrock. I couldn't find a managed FIS Bedrock fault
announced either; the only forward signal is the Bedrock Gateway at re:Invent (retries, fallbacks,
region/model spread), which is a mitigation, not a fault-injection feature. No ready-made SSM/FIS
template for Bedrock 503s exists in aws-samples, awslabs, or internal code search.

So it's a build. Two workable patterns:
- Client-side: FIS drives an SSM doc that installs a boto3 fault layer on the host running your
  agent, raising ServiceUnavailable/Throttling for a set % and duration, then cleans up. Faithful
  to what your retry code actually sees. This mirrors the SSM pattern already in the FIS template
  library (incl. the agentcore-strands-agent-faults experiment).
- Proxy-side: if you call Bedrock through a gateway, FIS + ALB weighted routing to a Lambda that
  returns 503 — the AWS "simulating partial failures" blog pattern, no app change. This is the
  better target now, since it proves the gateway's fallback actually fires.

Worth exercising both 503 and 529 (the official Bedrock wording lists both as the transient errors
to retry with backoff) plus 429 for the quota/pinning case.

I've scaffolded an experiment plan (approaches, parameters, stop conditions, observability, demo
narrative) — no build yet. Happy to turn it into a real template for the library once you tell me
whether your demo app calls Bedrock directly or via a gateway, and what it runs on.

---
Note: the Harbinger reactive wording backs the framing — CRIS is "primarily a capacity mechanism…
not, by itself, a failover or disaster recovery mechanism," and won't move you off an impaired
Region. That's the exact behaviour a 503 experiment would let a customer rehearse.
