import os
import unittest

from app import FaultRule, InjectorConfig, create_app


class StaticLoader:
    def __init__(self, config):
        self.config = config

    def get(self):
        return self.config


class LiteLLMProxyContractTests(unittest.TestCase):
    def setUp(self):
        self.previous = {
            key: os.environ.get(key)
            for key in (
                "INBOUND_AUTH_MODE",
                "PROXY_API_KEY",
                "PASSTHROUGH_MODE",
                "RESPONSE_STYLE",
                "INJECTOR_ID",
            )
        }
        os.environ.update(
            {
                "INBOUND_AUTH_MODE": "bearer",
                "PROXY_API_KEY": "proxy-test-key",
                "PASSTHROUGH_MODE": "echo",
                "RESPONSE_STYLE": "openai",
                "INJECTOR_ID": "local-test",
            }
        )

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def request(self, client):
        return client.post(
            "/v1/chat/completions",
            json={
                "model": "bedrock-test",
                "messages": [{"role": "user", "content": "pong"}],
            },
            headers={"Authorization": "Bearer proxy-test-key"},
        )

    def test_health_is_public_but_provider_requires_bearer_key(self):
        client = create_app(StaticLoader(InjectorConfig.passthrough())).test_client()
        self.assertEqual(client.get("/healthz").status_code, 200)
        self.assertEqual(
            client.post(
                "/v1/chat/completions",
                json={"model": "bedrock-test", "messages": []},
            ).status_code,
            401,
        )
        response = self.request(client)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["object"], "chat.completion")

    def test_matching_rule_returns_predefined_status_and_headers(self):
        config = InjectorConfig(
            active=True,
            rules=(FaultRule("bedrock-*", "503", 1.0),),
            execution_id="local-execution",
        )
        response = self.request(create_app(StaticLoader(config)).test_client())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["x-fault-injected"], "503")
        self.assertEqual(response.headers["x-fault-execution-id"], "local-execution")


if __name__ == "__main__":
    unittest.main()
