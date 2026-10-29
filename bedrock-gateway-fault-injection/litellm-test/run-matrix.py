#!/usr/bin/env python3
"""Small runtime-independent LiteLLM fault-proxy smoke client."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-status", type=int, required=True)
    parser.add_argument("--require-fault-header", action="store_true")
    args = parser.parse_args()

    base_url = os.environ.get("LITELLM_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("LITELLM_API_KEY", "")
    model = os.environ.get("LITELLM_MODEL", "bedrock-test")
    if not base_url or not api_key:
        print("LITELLM_BASE_URL and LITELLM_API_KEY are required", file=sys.stderr)
        return 2

    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "Reply with pong."}],
        "max_tokens": 16,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    status = None
    headers = {}
    body = b""
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status = response.status
            headers = dict(response.headers)
            body = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        headers = dict(error.headers)
        body = error.read()
    except urllib.error.URLError as error:
        print(f"request failed before receiving HTTP response: {error}", file=sys.stderr)
        return 1

    if status != args.expected_status:
        print(f"expected HTTP {args.expected_status}, received HTTP {status}", file=sys.stderr)
        return 1

    try:
        document = json.loads(body or b"{}")
    except json.JSONDecodeError:
        print("response was not JSON", file=sys.stderr)
        return 1

    if status == 200 and document.get("object") != "chat.completion":
        print("HTTP 200 did not contain an OpenAI chat.completion response", file=sys.stderr)
        return 1
    if args.require_fault_header and not headers.get("x-fault-injected"):
        print("expected x-fault-injected response header", file=sys.stderr)
        return 1

    print(json.dumps({
        "status": status,
        "object": document.get("object"),
        "fault_injected": headers.get("x-fault-injected"),
        "retry_after": headers.get("Retry-After"),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
