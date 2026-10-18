from __future__ import annotations

import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import zipfile
from pathlib import Path
from unittest import mock

import app
import browser_broker
import remote_browser
import webdav_sync


class FakeHTTPResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self.payload


class RemoteBrowserBrokerClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.token_file = Path(self.temp.name) / "broker-token"
        self.token = "t" * 48
        self.token_file.write_text(self.token, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def client(self) -> remote_browser.RemoteBrowserBrokerClient:
        return remote_browser.RemoteBrowserBrokerClient(
            "http://browser-broker:8765",
            self.token_file,
            timeout=3.0,
        )

    def test_client_rejects_non_internal_http_url_shapes(self) -> None:
        invalid_urls = (
            "https://browser-broker:8765",
            "http://alice:secret@browser-broker:8765",
            "http://browser-broker:8765/v1",
            "http://browser-broker:8765/?debug=1",
            "http://browser-broker:8765/#token=x",
            "browser-broker:8765",
        )
        for value in invalid_urls:
            with self.subTest(value=value), self.assertRaises(ValueError):
                remote_browser.RemoteBrowserBrokerClient(value, self.token_file)

    def test_client_sends_bearer_token_and_json_to_fixed_endpoint(self) -> None:
        response = FakeHTTPResponse(
            json.dumps({"ok": True, "url": "https://127.0.0.1:23000/"}).encode("utf-8")
        )
        with mock.patch.object(
            remote_browser.RemoteBrowserBrokerClient,
            "_open",
            return_value=response,
        ) as opener:
            result = self.client().start({"instance_id": "0123456789abcdef", "password": "not-returned"})

        request = opener.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.full_url, "http://browser-broker:8765/v1/sessions/start")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {self.token}")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {"instance_id": "0123456789abcdef", "password": "not-returned"},
        )
        self.assertEqual(result, {"ok": True, "url": "https://127.0.0.1:23000/"})
        self.assertGreaterEqual(opener.call_args.kwargs["timeout"], 600.0)

    def test_client_reads_credentials_from_fixed_endpoint_and_validates_response(self) -> None:
        instance_id = "0123456789abcdef"
        response = FakeHTTPResponse(
            json.dumps(
                {
                    "ok": True,
                    "instance_id": instance_id,
                    "url": "https://127.0.0.1:23000/",
                    "username": "alice",
                    "password": "correct-horse-battery-staple",
                }
            ).encode("utf-8")
        )
        with mock.patch.object(
            remote_browser.RemoteBrowserBrokerClient,
            "_open",
            return_value=response,
        ) as opener:
            result = self.client().credentials(instance_id)

        request = opener.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            request.full_url,
            "http://browser-broker:8765/v1/sessions/credentials",
        )
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"instance_id": instance_id})
        self.assertEqual(result["password"], "correct-horse-battery-staple")

        invalid_payloads = (
            {"ok": True, "instance_id": "f" * 16, "username": "alice", "password": "valid-password"},
            {"ok": True, "instance_id": instance_id, "username": "bad:user", "password": "valid-password"},
            {"ok": True, "instance_id": instance_id, "username": "alice", "password": "short"},
        )
        for payload in invalid_payloads:
            with (
                self.subTest(payload=payload),
                mock.patch.object(
                    remote_browser.RemoteBrowserBrokerClient,
                    "_open",
                    return_value=FakeHTTPResponse(json.dumps(payload).encode("utf-8")),
                ),
                self.assertRaisesRegex(remote_browser.RemoteBrowserError, "凭证响应格式无效"),
            ):
                self.client().credentials(instance_id)

    def test_client_forces_direct_connections_and_rejects_redirects(self) -> None:
        with mock.patch.object(
            remote_browser.urllib.request,
            "getproxies",
            return_value={"http": "http://external-proxy.example:8080"},
        ):
            client = self.client()
        proxy_handlers = [
            handler
            for handler in client._opener.handlers
            if isinstance(handler, urllib.request.ProxyHandler)
        ]
        # 显式空 ProxyHandler 会抑制默认环境代理；urllib 在代理表为空时不会
        # 把该 handler 保留在最终列表中。
        self.assertEqual(proxy_handlers, [])
        self.assertTrue(
            any(
                isinstance(handler, remote_browser._NoRedirectHandler)
                for handler in client._opener.handlers
            )
        )

    def test_client_rejects_missing_or_malformed_token_file(self) -> None:
        self.token_file.write_text("short", encoding="utf-8")
        with self.assertRaisesRegex(remote_browser.RemoteBrowserError, "凭证无效"):
            self.client().status()

        self.token_file.unlink()
        with self.assertRaisesRegex(remote_browser.RemoteBrowserError, "尚未就绪"):
            self.client().status()

    def test_client_translates_broker_disconnect_and_http_error(self) -> None:
        with mock.patch.object(
            remote_browser.RemoteBrowserBrokerClient,
            "_open",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with self.assertRaisesRegex(remote_browser.RemoteBrowserError, "无法连接浏览器 Broker"):
                self.client().sessions()

        with mock.patch.object(
            remote_browser.RemoteBrowserBrokerClient,
            "_open",
            side_effect=TimeoutError("timed out"),
        ) as opener:
            with self.assertRaisesRegex(remote_browser.RemoteBrowserError, "准备远程浏览器 Worker 镜像超时"):
                self.client().prepare()
        self.assertGreaterEqual(
            opener.call_args.kwargs["timeout"],
            remote_browser.BROKER_IMAGE_PREPARE_TIMEOUT,
        )

        error = urllib.error.HTTPError(
            "http://browser-broker:8765/v1/sessions/start",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(json.dumps({"error": "Docker worker 端口已耗尽"}).encode("utf-8")),
        )
        with mock.patch.object(
            remote_browser.RemoteBrowserBrokerClient,
            "_open",
            side_effect=error,
        ):
            with self.assertRaisesRegex(remote_browser.RemoteBrowserError, "端口已耗尽"):
                self.client().start({"instance_id": "0123456789abcdef"})

    def test_client_rejects_oversized_and_invalid_responses(self) -> None:
        with mock.patch.object(
            remote_browser.RemoteBrowserBrokerClient,
            "_open",
            return_value=FakeHTTPResponse(b"x" * (remote_browser.BROKER_RESPONSE_LIMIT + 1)),
        ):
            with self.assertRaisesRegex(remote_browser.RemoteBrowserError, "响应过大"):
                self.client().status()

        with mock.patch.object(
            remote_browser.RemoteBrowserBrokerClient,
            "_open",
            return_value=FakeHTTPResponse(b"not-json"),
        ):
            with self.assertRaisesRegex(remote_browser.RemoteBrowserError, "无效响应"):
                self.client().status()

    def test_client_rejects_non_authoritative_sessions_shape(self) -> None:
        response = FakeHTTPResponse(json.dumps({"ok": True, "sessions": None}).encode("utf-8"))
        with mock.patch.object(
            remote_browser.RemoteBrowserBrokerClient,
            "_open",
            return_value=response,
        ):
            with self.assertRaisesRegex(remote_browser.RemoteBrowserError, "会话响应格式无效"):
                self.client().sessions()

        duplicate = {
            "ok": True,
            "sessions": [
                {"instance_id": "0123456789abcdef"},
                {"instance_id": "0123456789abcdef"},
            ],
        }
        response = FakeHTTPResponse(json.dumps(duplicate).encode("utf-8"))
        with mock.patch.object(
            remote_browser.RemoteBrowserBrokerClient,
            "_open",
            return_value=response,
        ):
            with self.assertRaisesRegex(remote_browser.RemoteBrowserError, "会话响应格式无效"):
                self.client().sessions()

        response = FakeHTTPResponse(json.dumps({"sessions": []}).encode("utf-8"))
        with mock.patch.object(
            remote_browser.RemoteBrowserBrokerClient,
            "_open",
            return_value=response,
        ):
            with self.assertRaisesRegex(remote_browser.RemoteBrowserError, "Broker 操作失败"):
                self.client().sessions()


def broker_config(
    root: Path,
    *,
    port_start: int = 23000,
    port_end: int = 23001,
    max_sessions: int = 10,
    proxy_host: str = "proxybridge",
) -> browser_broker.BrokerConfig:
    return browser_broker.BrokerConfig(
        docker_socket="/var/run/docker.sock",
        owner_container="proxybridge",
        broker_container="proxybridge-browser-broker",
        worker_network="proxybridge-browser-workers",
        worker_image="lscr.io/linuxserver/chromium:latest",
        bind_host="127.0.0.1",
        public_host="127.0.0.1",
        port_start=port_start,
        port_end=port_end,
        max_sessions=max_sessions,
        memory_bytes=2 * 1024**3,
        nano_cpus=2_000_000_000,
        pids_limit=512,
        shm_bytes=1024**3,
        proxy_host=proxy_host,
        control_dir=root,
        token_file=root / "token",
    )


def valid_start_payload(**overrides: object) -> dict[str, object]:
    return {
        "instance_id": "0123456789abcdef",
        "operation_id": "1" * 32,
        "profile_rel": "browser_profiles/node-record/0123456789abcdef",
        "fingerprint_rel": "fingerprint-chromium/chromium",
        "username": "proxybridge",
        "password": "correct-horse-battery-staple",
        "proxy_port": 21080,
        "fingerprint_seed": 123456,
        "language": "zh-CN",
        "accept_language": "zh-CN,zh,en-US,en",
        "timezone": "Asia/Shanghai",
        **overrides,
    }


def running_worker_inspect(
    container_id: str = "c" * 64,
    *,
    instance_id: str = "0123456789abcdef",
    host_port: int = 23000,
    proxy_port: int = 21080,
) -> dict[str, object]:
    return {
        "Id": container_id,
        "Name": f"/proxybridge-browser-{instance_id}",
        "Config": {
            "Labels": {
                browser_broker.LABEL_WORKER: "1",
                browser_broker.LABEL_INSTANCE: instance_id,
                browser_broker.LABEL_OWNER: "proxybridge",
                browser_broker.LABEL_PROXY_PORT: str(proxy_port),
                browser_broker.LABEL_PUBLIC_PORT: str(host_port),
            }
        },
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "NetworkSettings": {
            # internal bridge 上 Docker 不会生成可用的 HostPort；公开端口来自标签
            # 和 Broker TCP 中继，这是本次部署回归需要覆盖的真实 inspect 形状。
            "Ports": {"3001/tcp": None},
            "Networks": {
                "proxybridge-browser-workers": {"IPAddress": "172.31.254.10"}
            },
        },
    }


def owner_container_inspect(
    *,
    network_id: str = "d" * 64,
    owner_id: str = "a" * 64,
    owner_ip: str = "172.31.254.2",
    aliases: list[str] | None = None,
    dns_names: list[str] | None = None,
) -> dict[str, object]:
    return {
        "Id": owner_id,
        "Name": "/proxybridge",
        "Mounts": [
            {
                "Destination": "/data",
                "Source": "/var/lib/docker/volumes/proxybridge-data/_data",
                "Type": "volume",
                "Name": "proxybridge-data",
            }
        ],
        "NetworkSettings": {
            "Networks": {
                "proxybridge-browser-workers": {
                    "NetworkID": network_id,
                    "IPAddress": owner_ip,
                    "Aliases": aliases if aliases is not None else ["proxybridge"],
                    "DNSNames": (
                        dns_names if dns_names is not None else ["proxybridge", owner_id[:12]]
                    ),
                }
            }
        },
    }


def broker_container_inspect(
    *,
    network_id: str = "d" * 64,
    broker_ip: str = "172.31.254.3",
    bind_host: str = "127.0.0.1",
    port_start: int = 23000,
    port_end: int = 23001,
) -> dict[str, object]:
    return {
        "Id": "b" * 64,
        "Name": "/proxybridge-browser-broker",
        "NetworkSettings": {
            "Networks": {
                "proxybridge-browser-workers": {
                    "NetworkID": network_id,
                    "IPAddress": broker_ip,
                }
            },
            "Ports": {
                f"{port}/tcp": [{"HostIp": bind_host, "HostPort": str(port)}]
                for port in range(port_start, port_end + 1)
            },
        },
    }


def worker_network_inspect(
    *,
    network_id: str = "d" * 64,
    owner_id: str = "a" * 64,
    owner_ip: str = "172.31.254.2",
    subnet: str = "172.31.254.0/24",
) -> dict[str, object]:
    return {
        "Id": network_id,
        "Name": "proxybridge-browser-workers",
        "Driver": "bridge",
        "Scope": "local",
        "Internal": True,
        "IPAM": {"Config": [{"Subnet": subnet, "Gateway": "172.31.254.1"}]},
        "Containers": {
            owner_id: {
                "Name": "proxybridge",
                "IPv4Address": f"{owner_ip}/24",
            }
        },
    }


class BrowserBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = mock.Mock(spec=browser_broker.DockerEngineClient)
        self.engine.api_version.return_value = "1.45"
        self.relay = mock.Mock(spec=browser_broker.WorkerTCPRelay)
        self.relay_factory = mock.Mock(return_value=self.relay)
        self.broker = browser_broker.BrowserWorkerBroker(
            broker_config(self.root),
            self.engine,
            relay_factory=self.relay_factory,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_public_relay_range_cannot_publish_broker_control_port(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {
                    "PROXY_BRIDGE_BROWSER_PORT_START": "8700",
                    "PROXY_BRIDGE_BROWSER_PORT_END": "8800",
                },
                clear=True,
            ),
            self.assertRaisesRegex(browser_broker.BrokerError, "端口范围无效"),
        ):
            browser_broker.BrokerConfig.from_environment()

    def test_start_payload_rejects_path_traversal_and_invalid_login(self) -> None:
        normalized = browser_broker._validate_start_payload(valid_start_payload())
        self.assertEqual(normalized["profile_rel"], "browser_profiles/node-record/0123456789abcdef")
        self.assertEqual(normalized["fingerprint_rel"], "fingerprint-chromium/chromium")

        invalid_cases = (
            {"profile_rel": "../../etc/passwd"},
            {"profile_rel": "browser_profiles/node-record/../escape"},
            {"profile_rel": "browser_profiles/node-record/%2e%2e"},
            {"fingerprint_rel": "../fingerprint-chromium/chromium"},
            {"fingerprint_rel": "fingerprint-chromium/../../etc"},
            {"instance_id": "../../docker"},
            {"operation_id": "not-an-operation-id"},
            {"username": "user:name"},
            {"password": "too-short"},
        )
        for overrides in invalid_cases:
            with self.subTest(overrides=overrides), self.assertRaises(browser_broker.BrokerError):
                browser_broker._validate_start_payload(valid_start_payload(**overrides))

        owner = {
            "Mounts": [
                {
                    "Destination": "/data",
                    "Source": "/var/lib/docker/volumes/proxybridge-data/_data",
                    "Type": "volume",
                    "Name": "proxybridge-data",
                }
            ]
        }
        with mock.patch.object(self.broker, "_owner", return_value=owner):
            self.assertEqual(self.broker._data_volume(), "proxybridge-data")
        bind_owner = {"Mounts": [{"Destination": "/data", "Source": "/srv/data", "Type": "bind"}]}
        with mock.patch.object(self.broker, "_owner", return_value=bind_owner):
            with self.assertRaisesRegex(browser_broker.BrokerError, "命名卷"):
                self.broker._data_volume()

    def test_control_directory_requires_named_volume(self) -> None:
        control_mount = {
            "Mounts": [
                {
                    "Destination": str(self.root),
                    "Source": "/var/lib/docker/volumes/proxybridge-browser-control/_data",
                    "Type": "volume",
                    "Name": "proxybridge-browser-control",
                }
            ]
        }
        with mock.patch.object(self.broker, "_broker", return_value=control_mount):
            self.assertEqual(
                self.broker._control_volume_source(),
                "/var/lib/docker/volumes/proxybridge-browser-control/_data",
            )

        bind_control = {
            "Mounts": [
                {
                    "Destination": str(self.root),
                    "Source": "/srv/proxybridge-control",
                    "Type": "bind",
                }
            ]
        }
        with mock.patch.object(self.broker, "_broker", return_value=bind_control):
            with self.assertRaisesRegex(browser_broker.BrokerError, "命名卷"):
                self.broker._control_volume_source()
            with self.assertRaisesRegex(browser_broker.BrokerError, "命名卷"):
                self.broker.ensure_token()
        self.assertFalse(self.broker.config.token_file.exists())

    def test_worker_network_requires_private_internal_local_bridge_and_owner_alias(self) -> None:
        owner = owner_container_inspect(aliases=["proxybridge", "proxy-alias"])
        network = worker_network_inspect()
        alias_broker = browser_broker.BrowserWorkerBroker(
            broker_config(self.root, proxy_host="proxy-alias"),
            self.engine,
        )
        self.engine.json_request.return_value = network
        with (
            mock.patch.object(alias_broker, "_inspect_container", return_value=owner),
            mock.patch.object(alias_broker, "_broker", return_value=broker_container_inspect()),
        ):
            self.assertEqual(alias_broker._validated_worker_network(), network)

        invalid_cases: list[tuple[str, dict[str, object], dict[str, object], str]] = []

        external_network = worker_network_inspect()
        external_network["Internal"] = False
        invalid_cases.append(("external", external_network, owner_container_inspect(), "internal=true"))

        overlay_network = worker_network_inspect()
        overlay_network["Driver"] = "overlay"
        invalid_cases.append(("overlay", overlay_network, owner_container_inspect(), "本地 bridge"))

        swarm_network = worker_network_inspect()
        swarm_network["Scope"] = "swarm"
        invalid_cases.append(("swarm", swarm_network, owner_container_inspect(), "本地 bridge"))

        public_network = worker_network_inspect(subnet="8.8.8.0/24", owner_ip="8.8.8.2")
        public_owner = owner_container_inspect(owner_ip="8.8.8.2")
        invalid_cases.append(("public", public_network, public_owner, "IPAM"))

        tailscale_network = worker_network_inspect(
            subnet="100.64.0.0/24",
            owner_ip="100.64.0.2",
        )
        tailscale_owner = owner_container_inspect(owner_ip="100.64.0.2")
        invalid_cases.append(("tailscale", tailscale_network, tailscale_owner, "RFC1918"))

        ipv6_network = worker_network_inspect(subnet="fd00::/64")
        invalid_cases.append(("ipv6", ipv6_network, owner_container_inspect(), "私有 IPv4"))

        detached_owner = owner_container_inspect()
        detached_owner["NetworkSettings"] = {"Networks": {}}
        invalid_cases.append(("detached", worker_network_inspect(), detached_owner, "未加入"))

        missing_endpoint = worker_network_inspect()
        missing_endpoint["Containers"] = {}
        invalid_cases.append(("endpoint", missing_endpoint, owner_container_inspect(), "未记录"))

        for label, candidate_network, candidate_owner, error in invalid_cases:
            with self.subTest(label=label):
                self.engine.json_request.return_value = candidate_network
                with (
                    mock.patch.object(
                        self.broker,
                        "_inspect_container",
                        return_value=candidate_owner,
                    ),
                    self.assertRaisesRegex(browser_broker.BrokerError, error),
                ):
                    self.broker._validated_worker_network()

        wrong_proxy_broker = browser_broker.BrowserWorkerBroker(
            broker_config(self.root, proxy_host="not-owner"),
            self.engine,
        )
        self.engine.json_request.return_value = worker_network_inspect()
        with (
            mock.patch.object(
                wrong_proxy_broker,
                "_inspect_container",
                return_value=owner_container_inspect(),
            ),
            self.assertRaisesRegex(browser_broker.BrokerError, "名称或别名"),
        ):
            wrong_proxy_broker._validated_worker_network()

        self.engine.json_request.return_value = worker_network_inspect()
        detached_broker = broker_container_inspect()
        detached_broker["NetworkSettings"] = {"Networks": {}, "Ports": {}}
        with (
            mock.patch.object(
                self.broker,
                "_inspect_container",
                return_value=owner_container_inspect(),
            ),
            mock.patch.object(self.broker, "_broker", return_value=detached_broker),
            self.assertRaisesRegex(browser_broker.BrokerError, "Broker 容器未加入"),
        ):
            self.broker._validated_worker_network()

        unpublished_broker = broker_container_inspect()
        unpublished_broker["NetworkSettings"]["Ports"] = {"23000/tcp": None}
        with (
            mock.patch.object(
                self.broker,
                "_inspect_container",
                return_value=owner_container_inspect(),
            ),
            mock.patch.object(self.broker, "_broker", return_value=unpublished_broker),
            self.assertRaisesRegex(browser_broker.BrokerError, "未实际发布.*最新 compose"),
        ):
            self.broker._validated_worker_network()

    def test_publish_host_rejects_wildcard_and_public_ip_but_accepts_lan_and_tailscale(self) -> None:
        self.assertEqual(browser_broker._private_or_loopback_ipv4("127.0.0.1"), "127.0.0.1")
        self.assertEqual(browser_broker._private_or_loopback_ipv4("192.168.1.20"), "192.168.1.20")
        self.assertEqual(browser_broker._private_or_loopback_ipv4("100.64.0.8"), "100.64.0.8")
        for value in ("0.0.0.0", "8.8.8.8", "example.com"):
            with self.subTest(value=value), self.assertRaises(browser_broker.BrokerError):
                browser_broker._private_or_loopback_ipv4(value)

    def test_worker_payload_uses_fixed_labels_and_never_embeds_password(self) -> None:
        values = browser_broker._validate_start_payload(valid_start_payload())
        secret = str(values["password"])
        with (
            mock.patch.object(self.broker, "_data_volume", return_value="proxybridge-data"),
            mock.patch.object(self.broker, "_worker_init_script_path", return_value="/control/init-script"),
        ):
            payload = self.broker._worker_payload(values, 23000, "/control/sessions/password-file")

        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertEqual(
            payload["Labels"],
            {
                browser_broker.LABEL_WORKER: "1",
                browser_broker.LABEL_INSTANCE: "0123456789abcdef",
                browser_broker.LABEL_OWNER: "proxybridge",
                browser_broker.LABEL_PROXY_PORT: "21080",
                browser_broker.LABEL_PUBLIC_PORT: "23000",
            },
        )
        self.assertIn("FILE__PASSWORD=/run/secrets/proxybridge-browser-password", payload["Env"])
        self.assertNotIn(f"PASSWORD={secret}", payload["Env"])
        self.assertIn("PIXELFLUX_WAYLAND=false", payload["Env"])
        self.assertNotIn("PIXELFLUX_WAYLAND=true", payload["Env"])
        self.assertEqual(payload["HostConfig"]["NetworkMode"], "proxybridge-browser-workers")
        self.assertNotIn("PortBindings", payload["HostConfig"])
        self.assertFalse(payload["HostConfig"]["Privileged"])
        self.assertNotIn("Init", payload["HostConfig"])
        self.assertEqual(payload["HostConfig"]["SecurityOpt"], ["no-new-privileges:true"])
        self.assertEqual(payload["Volumes"], {"/config": {}})
        mounts = payload["HostConfig"]["Mounts"]
        self.assertEqual(
            mounts,
            [
                {
                    "Type": "volume",
                    "Source": "proxybridge-data",
                    "Target": "/config/browser-profile",
                    "ReadOnly": False,
                    "VolumeOptions": {
                        "NoCopy": True,
                        "Subpath": "browser_profiles/node-record/0123456789abcdef",
                    },
                },
                {
                    "Type": "volume",
                    "Source": "proxybridge-data",
                    "Target": "/opt/fingerprint-chromium",
                    "ReadOnly": True,
                    "VolumeOptions": {
                        "NoCopy": True,
                        "Subpath": "fingerprint-chromium/chromium",
                    },
                },
            ],
        )
        serialized_binds = json.dumps(payload["HostConfig"]["Binds"], ensure_ascii=False)
        self.assertNotIn(str(values["profile_rel"]), serialized_binds)
        self.assertNotIn(str(values["fingerprint_rel"]), serialized_binds)
        self.assertNotIn("/data/browser-profile", serialized)
        self.assertIn("Healthcheck", payload)
        health_command = " ".join(payload["Healthcheck"]["Test"])
        self.assertIn("https://127.0.0.1:3001/", health_command)
        self.assertIn("%{http_code}", health_command)
        self.assertIn('== \"401\"', health_command)
        self.assertIn("$CUSTOM_USER", health_command)
        self.assertIn("/run/secrets/proxybridge-browser-password", health_command)

    def test_worker_init_launcher_preserves_fingerprint_wrapper_resource_path(self) -> None:
        self.assertIn(
            'exec /opt/fingerprint-chromium/chrome-wrapper "$@"',
            browser_broker.WORKER_INIT_SCRIPT,
        )
        self.assertNotIn(
            "ln -sfn /opt/fingerprint-chromium/chrome-wrapper",
            browser_broker.WORKER_INIT_SCRIPT,
        )

    def test_internal_worker_session_uses_relay_port_label_without_docker_host_binding(self) -> None:
        session = self.broker._session_from_inspect(running_worker_inspect())

        self.assertEqual(session["status"], "running")
        self.assertEqual(session["host_port"], 23000)
        self.assertEqual(session["url"], "https://127.0.0.1:23000/")
        self.assertEqual(session["worker_ip"], "172.31.254.10")

    def test_tcp_relay_reports_port_conflict_without_starting_a_thread(self) -> None:
        listener = mock.Mock(spec=socket.socket)
        listener.bind.side_effect = OSError(browser_broker.errno.EADDRINUSE, "in use")
        with (
            mock.patch.object(browser_broker.socket, "socket", return_value=listener),
            self.assertRaisesRegex(browser_broker.RelayPortInUseError, "已被占用"),
        ):
            browser_broker.WorkerTCPRelay(23000, "172.31.254.10")
        listener.close.assert_called_once_with()

    def test_tcp_relay_pump_forwards_bytes_and_half_closes_destination(self) -> None:
        source = mock.Mock(spec=socket.socket)
        destination = mock.Mock(spec=socket.socket)
        source.recv.side_effect = [b"tls-record", b""]
        finished = threading.Event()
        failed = threading.Event()

        browser_broker.WorkerTCPRelay._pump(source, destination, finished, failed)

        destination.sendall.assert_called_once_with(b"tls-record")
        destination.shutdown.assert_called_once_with(socket.SHUT_WR)
        self.assertTrue(finished.is_set())
        self.assertFalse(failed.is_set())

    def test_tcp_relay_preserves_response_after_client_half_closes(self) -> None:
        client, relay_client = socket.socketpair()
        relay_upstream, worker = socket.socketpair()
        relay = object.__new__(browser_broker.WorkerTCPRelay)
        relay.listen_port = 23000
        relay.target_host = "172.31.254.10"
        relay.target_port = browser_broker.WORKER_HTTPS_PORT
        relay.stopping = threading.Event()
        relay._connection_lock = threading.Lock()
        relay._connections = set()
        relay._connection_slots = mock.Mock()
        received_by_worker: list[bytes] = []
        worker_errors: list[BaseException] = []

        def run_worker() -> None:
            try:
                request = b""
                while True:
                    chunk = worker.recv(1024)
                    if not chunk:
                        break
                    request += chunk
                received_by_worker.append(request)
                worker.sendall(b"response-after-request-eof")
                worker.shutdown(socket.SHUT_WR)
            except BaseException as error:
                worker_errors.append(error)
            finally:
                worker.close()

        worker_thread = threading.Thread(target=run_worker)
        relay_thread = threading.Thread(target=relay._handle_client, args=(relay_client,))
        try:
            worker_thread.start()
            with mock.patch.object(
                browser_broker.socket,
                "create_connection",
                return_value=relay_upstream,
            ):
                relay_thread.start()
                client.sendall(b"request")
                client.shutdown(socket.SHUT_WR)
                response = b""
                while True:
                    chunk = client.recv(1024)
                    if not chunk:
                        break
                    response += chunk
            relay_thread.join(timeout=2)
            worker_thread.join(timeout=2)
        finally:
            client.close()
            relay_client.close()
            relay_upstream.close()
            worker.close()

        self.assertEqual(worker_errors, [])
        self.assertEqual(received_by_worker, [b"request"])
        self.assertEqual(response, b"response-after-request-eof")
        self.assertFalse(relay_thread.is_alive())
        self.assertFalse(worker_thread.is_alive())
        relay._connection_slots.release.assert_called_once_with()

    def test_tcp_relay_preflights_internal_worker_before_publishing_link(self) -> None:
        listener = mock.Mock(spec=socket.socket)
        upstream = mock.Mock(spec=socket.socket)
        relay_thread = mock.Mock()
        with (
            mock.patch.object(browser_broker.socket, "socket", return_value=listener),
            mock.patch.object(browser_broker.socket, "create_connection", return_value=upstream) as connect,
            mock.patch.object(browser_broker.threading, "Thread", return_value=relay_thread),
        ):
            relay = browser_broker.WorkerTCPRelay(23000, "172.31.254.10")
            relay.start()

        connect.assert_called_once_with(("172.31.254.10", 3001), timeout=5.0)
        upstream.close.assert_called_once_with()
        relay_thread.start.assert_called_once_with()

    def test_stop_without_worker_closes_stale_public_relay(self) -> None:
        self.broker._relays["0123456789abcdef"] = self.relay
        with (
            mock.patch.object(self.broker, "_existing_session", return_value=None),
            mock.patch.object(self.broker, "_remove_secret") as remove_secret,
        ):
            result = self.broker.stop("0123456789abcdef")

        self.assertEqual(result["stopped"], 0)
        self.relay.stop.assert_called_once_with()
        remove_secret.assert_called_once_with("0123456789abcdef")

    def test_session_reconciliation_closes_relay_after_worker_disappears(self) -> None:
        self.broker._relays["0123456789abcdef"] = self.relay
        with mock.patch.object(self.broker, "_list_raw", return_value=[]):
            self.assertEqual(self.broker.sessions(), [])

        self.relay.stop.assert_called_once_with()
        self.assertEqual(self.broker._relays, {})

    def test_worker_listing_is_limited_by_owner_and_worker_labels(self) -> None:
        self.engine.request.return_value = b"[]"
        self.assertEqual(self.broker._list_raw(), [])

        _method, path = self.engine.request.call_args.args[:2]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        filters = json.loads(query["filters"][0])
        self.assertCountEqual(
            filters["label"],
            [
                f"{browser_broker.LABEL_WORKER}=1",
                f"{browser_broker.LABEL_OWNER}=proxybridge",
            ],
        )

    def test_sessions_and_stop_fail_closed_when_worker_inspect_fails(self) -> None:
        with (
            mock.patch.object(self.broker, "_list_raw", return_value=[{"Id": "c" * 64}]),
            mock.patch.object(
                self.broker,
                "_inspect_container",
                side_effect=browser_broker.EngineError(500, "inspect failed"),
            ),
            mock.patch.object(self.broker, "_remove_secret") as remove_secret,
        ):
            with self.assertRaisesRegex(browser_broker.EngineError, "inspect failed"):
                self.broker.sessions()
            with self.assertRaisesRegex(browser_broker.EngineError, "inspect failed"):
                self.broker.stop("0123456789abcdef")
        remove_secret.assert_not_called()

    def test_name_conflict_never_deletes_unrelated_container(self) -> None:
        session_dir = self.root / "sessions"
        session_dir.mkdir()
        secret_file = session_dir / "0123456789abcdef.password"
        self.engine.json_request.side_effect = browser_broker.EngineError(409, "name already in use")
        with (
            mock.patch.object(self.broker, "_existing_session", return_value=None),
            mock.patch.object(self.broker, "sessions", return_value=[]),
            mock.patch.object(self.broker, "_ensure_image"),
            mock.patch.object(self.broker, "_validated_worker_network"),
            mock.patch.object(self.broker, "_data_volume", return_value="proxybridge-data"),
            mock.patch.object(
                self.broker,
                "_worker_init_script_path",
                return_value="/control/init-script",
            ),
            mock.patch.object(self.broker, "_worker_payload", return_value={"Image": "worker"}),
            mock.patch.object(
                self.broker,
                "_secret_paths",
                return_value=(secret_file, "/control/sessions/0123456789abcdef.password"),
            ),
            mock.patch.object(self.broker, "_remove_container") as remove_container,
        ):
            with self.assertRaisesRegex(browser_broker.EngineError, "name already in use"):
                self.broker.start(valid_start_payload())
        remove_container.assert_not_called()
        self.assertFalse(secret_file.exists())

    def test_stop_tombstone_rejects_start_that_arrives_late(self) -> None:
        operation_id = "2" * 32
        with (
            mock.patch.object(self.broker, "_existing_session", return_value=None),
            mock.patch.object(self.broker, "_remove_secret"),
        ):
            stopped = self.broker.stop("0123456789abcdef", operation_id)
        self.assertEqual(stopped["stopped"], 0)

        with self.assertRaisesRegex(browser_broker.BrokerError, "已被停止操作取消"):
            self.broker.start(valid_start_payload(operation_id=operation_id))
        # 同一一次性操作 ID 的重复/重试请求也必须持续被拒绝，不能消费 tombstone
        # 后让稍晚到达的副本重新创建 worker。
        with self.assertRaisesRegex(browser_broker.BrokerError, "已被停止操作取消"):
            self.broker.start(valid_start_payload(operation_id=operation_id))
        self.engine.json_request.assert_not_called()

    def test_image_pull_and_create_use_linux_amd64_platform(self) -> None:
        self.engine.json_request.side_effect = [
            {"Os": "linux", "Architecture": "arm64"},
            {"Os": "linux", "Architecture": "amd64"},
        ]
        self.engine.request.return_value = b'{"status":"pulled"}\n'

        self.broker._ensure_image()

        pull_path = self.engine.request.call_args.args[1]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(pull_path).query)
        self.assertEqual(query["platform"], ["linux/amd64"])
        self.assertEqual(query["fromImage"], ["lscr.io/linuxserver/chromium"])
        self.assertEqual(query["tag"], ["latest"])

    def test_image_pull_timeout_reports_registry_context_and_status_exposes_cache_state(self) -> None:
        self.engine.json_request.side_effect = browser_broker.EngineError(404, "not found")
        self.engine.request.side_effect = browser_broker.EngineTimeoutError("Docker Engine 请求超时")

        with self.assertRaisesRegex(browser_broker.BrokerError, "超过 15 分钟.*pull/load"):
            self.broker._ensure_image()

        self.engine.json_request.side_effect = [
            browser_broker.EngineError(404, "not found"),
        ]
        with (
            mock.patch.object(self.broker, "_require_volume_subpath_api"),
            mock.patch.object(self.broker, "_validated_worker_network"),
            mock.patch.object(self.broker, "_data_volume", return_value="proxybridge-data"),
            mock.patch.object(self.broker, "_control_volume_source", return_value="/control"),
            mock.patch.object(self.broker, "sessions", return_value=[]),
        ):
            status = self.broker.status()
        self.assertFalse(status["worker_image_ready"])

    def test_port_exhaustion_removes_session_password_file(self) -> None:
        occupied = [
            {"instance_id": "aaaaaaaaaaaaaaaa", "status": "running", "host_port": 23000},
            {"instance_id": "bbbbbbbbbbbbbbbb", "status": "running", "host_port": 23001},
        ]
        session_dir = self.root / "sessions"
        session_dir.mkdir()
        secret_file = session_dir / "0123456789abcdef.password"
        with (
            mock.patch.object(self.broker, "_existing_session", return_value=None),
            mock.patch.object(self.broker, "sessions", return_value=occupied),
            mock.patch.object(self.broker, "_ensure_image"),
            mock.patch.object(self.broker, "_validated_worker_network"),
            mock.patch.object(self.broker, "_data_volume", return_value="proxybridge-data"),
            mock.patch.object(
                self.broker,
                "_worker_init_script_path",
                return_value="/control/init-script",
            ),
            mock.patch.object(
                self.broker,
                "_secret_paths",
                return_value=(secret_file, "/control/sessions/0123456789abcdef.password"),
            ),
        ):
            with self.assertRaisesRegex(browser_broker.BrokerError, "没有可用的远程浏览器端口"):
                self.broker.start(valid_start_payload())

        self.assertFalse(secret_file.exists())
        self.engine.json_request.assert_not_called()

    def test_unhealthy_running_workers_count_toward_limits_and_active_sessions(self) -> None:
        limited_broker = browser_broker.BrowserWorkerBroker(
            broker_config(self.root, max_sessions=1),
            self.engine,
        )
        failed_session = {
            "instance_id": "aaaaaaaaaaaaaaaa",
            "status": "failed",
            "host_port": 23000,
        }
        with (
            mock.patch.object(limited_broker, "_validated_worker_network"),
            mock.patch.object(limited_broker, "_data_volume", return_value="proxybridge-data"),
            mock.patch.object(limited_broker, "_worker_init_script_path", return_value="/control/init"),
            mock.patch.object(limited_broker, "_ensure_image"),
            mock.patch.object(limited_broker, "_existing_session", return_value=None),
            mock.patch.object(limited_broker, "sessions", return_value=[failed_session]),
            mock.patch.object(limited_broker, "_write_secret") as write_secret,
        ):
            with self.assertRaisesRegex(browser_broker.BrokerError, "并发已达到上限"):
                limited_broker.start(valid_start_payload())
        write_secret.assert_not_called()

        sessions = [
            {"instance_id": "aaaaaaaaaaaaaaaa", "status": "running"},
            {"instance_id": "bbbbbbbbbbbbbbbb", "status": "starting"},
            {"instance_id": "cccccccccccccccc", "status": "failed"},
            {"instance_id": "dddddddddddddddd", "status": "stopped"},
        ]
        with (
            mock.patch.object(limited_broker, "_validated_worker_network"),
            mock.patch.object(limited_broker, "_data_volume", return_value="proxybridge-data"),
            mock.patch.object(limited_broker, "_control_volume_source", return_value="/control"),
            mock.patch.object(limited_broker, "sessions", return_value=sessions),
        ):
            status = limited_broker.status()
        self.assertEqual(status["active_sessions"], 3)

    def test_start_and_stop_results_never_return_password_and_stop_cleans_secret(self) -> None:
        container_id = "c" * 64
        inspect = running_worker_inspect(container_id)
        self.engine.json_request.return_value = {"Id": container_id}
        self.engine.request.return_value = b""
        with (
            mock.patch.object(self.broker, "_existing_session", return_value=None),
            mock.patch.object(self.broker, "sessions", return_value=[]),
            mock.patch.object(self.broker, "_ensure_image"),
            mock.patch.object(self.broker, "_validated_worker_network"),
            mock.patch.object(self.broker, "_data_volume", return_value="proxybridge-data"),
            mock.patch.object(
                self.broker,
                "_worker_init_script_path",
                return_value="/control/init-script",
            ),
            mock.patch.object(self.broker, "_write_secret", return_value="/control/password"),
            mock.patch.object(self.broker, "_worker_payload", return_value={"Image": "worker"}),
            mock.patch.object(self.broker, "_inspect_container", return_value=inspect),
        ):
            result = self.broker.start(valid_start_payload())

        result_text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("correct-horse-battery-staple", result_text)
        self.assertNotIn("password", result)
        self.assertEqual(result["url"], "https://127.0.0.1:23000/")
        self.relay_factory.assert_called_once_with(23000, "172.31.254.10")
        self.relay.start.assert_called_once_with()

        with (
            mock.patch.object(self.broker, "_existing_session", return_value=result),
            mock.patch.object(self.broker, "_inspect_container", return_value=inspect),
            mock.patch.object(self.broker, "_stop_and_remove_owned") as stop_and_remove,
            mock.patch.object(self.broker, "_remove_secret") as remove_secret,
        ):
            stopped = self.broker.stop("0123456789abcdef")
        self.assertEqual(stopped, {"ok": True, "stopped": 1, "instance_id": "0123456789abcdef"})
        self.assertNotIn("password", stopped)
        stop_and_remove.assert_called_once_with(
            inspect,
            instance_id="0123456789abcdef",
            remove_timeout=20.0,
        )
        remove_secret.assert_called_once_with("0123456789abcdef")
        self.relay.stop.assert_called_once_with()

    def test_credentials_only_returns_owned_running_worker_secret(self) -> None:
        instance_id = "0123456789abcdef"
        inspect = running_worker_inspect(instance_id=instance_id)
        inspect["Config"]["Env"] = ["CUSTOM_USER=alice", "FILE__PASSWORD=/run/secret"]
        session = {
            "instance_id": instance_id,
            "container_id": "c" * 64,
            "status": "running",
            "url": "https://127.0.0.1:23000/",
        }
        with (
            mock.patch.object(self.broker, "_existing_session", return_value=session),
            mock.patch.object(self.broker, "_inspect_container", return_value=inspect),
            mock.patch.object(
                self.broker,
                "_read_secret",
                return_value="correct-horse-battery-staple",
            ) as read_secret,
        ):
            result = self.broker.credentials(instance_id)

        self.assertEqual(result["username"], "alice")
        self.assertEqual(result["password"], "correct-horse-battery-staple")
        read_secret.assert_called_once_with(instance_id)

        with (
            mock.patch.object(
                self.broker,
                "_existing_session",
                return_value={**session, "status": "stopped", "url": ""},
            ),
            mock.patch.object(self.broker, "_read_secret") as stopped_secret,
            self.assertRaisesRegex(browser_broker.BrokerError, "仅运行中"),
        ):
            self.broker.credentials(instance_id)
        stopped_secret.assert_not_called()

        foreign = running_worker_inspect(instance_id=instance_id)
        foreign["Config"]["Labels"][browser_broker.LABEL_OWNER] = "another-service"
        foreign["Config"]["Env"] = ["CUSTOM_USER=alice"]
        with (
            mock.patch.object(self.broker, "_existing_session", return_value=session),
            mock.patch.object(self.broker, "_inspect_container", return_value=foreign),
            self.assertRaisesRegex(browser_broker.BrokerError, "不属于当前"),
        ):
            self.broker.credentials(instance_id)

    def test_read_secret_rejects_missing_oversized_and_control_character_content(self) -> None:
        secret = self.root / "session.password"
        with mock.patch.object(
            self.broker,
            "_secret_paths",
            return_value=(secret, "/control/session.password"),
        ):
            with self.assertRaisesRegex(browser_broker.BrokerError, "已不存在"):
                self.broker._read_secret("0123456789abcdef")

            for value in ("short", "valid-password\n", "x" * 129):
                secret.write_text(value, encoding="utf-8")
                with self.subTest(value_length=len(value)), self.assertRaisesRegex(
                    browser_broker.BrokerError,
                    "格式无效",
                ):
                    self.broker._read_secret("0123456789abcdef")

            secret.write_text("correct-horse-battery-staple", encoding="utf-8")
            self.assertEqual(
                self.broker._read_secret("0123456789abcdef"),
                "correct-horse-battery-staple",
            )

    def test_start_recreates_existing_worker_so_new_credentials_take_effect(self) -> None:
        old_container_id = "a" * 64
        new_container_id = "b" * 64
        inspect = running_worker_inspect(new_container_id)
        self.engine.json_request.return_value = {"Id": new_container_id}
        self.engine.request.return_value = b""
        with (
            mock.patch.object(
                self.broker,
                "_existing_session",
                return_value={"container_id": old_container_id, "status": "running"},
            ),
            mock.patch.object(self.broker, "_stop_and_remove_owned") as stop_and_remove,
            mock.patch.object(self.broker, "_remove_secret") as remove_secret,
            mock.patch.object(self.broker, "sessions", return_value=[]),
            mock.patch.object(self.broker, "_ensure_image"),
            mock.patch.object(self.broker, "_validated_worker_network"),
            mock.patch.object(self.broker, "_data_volume", return_value="proxybridge-data"),
            mock.patch.object(
                self.broker,
                "_worker_init_script_path",
                return_value="/control/init-script",
            ),
            mock.patch.object(self.broker, "_write_secret", return_value="/control/password"),
            mock.patch.object(self.broker, "_worker_payload", return_value={"Image": "worker"}),
            mock.patch.object(self.broker, "_inspect_container", return_value=inspect),
        ):
            result = self.broker.start(valid_start_payload(password="a-new-strong-password"))

        stop_and_remove.assert_any_call(
            inspect,
            instance_id="0123456789abcdef",
            force_on_failure=True,
            remove_timeout=15.0,
        )
        remove_secret.assert_called_once_with("0123456789abcdef")
        self.assertNotIn("reused", result)
        self.assertEqual(result["container_id"], new_container_id)

    def test_shutdown_cleanup_only_removes_labeled_worker_ids_and_matching_secrets(self) -> None:
        sessions = self.root / "sessions"
        sessions.mkdir()
        valid_secret = sessions / "0123456789abcdef.password"
        unrelated_secret = sessions / "not-a-worker.password"
        valid_secret.write_text("secret", encoding="utf-8")
        unrelated_secret.write_text("keep", encoding="utf-8")
        self.broker._relays["0123456789abcdef"] = self.relay
        with (
            mock.patch.object(self.broker, "_list_raw", return_value=[{"Id": "c" * 64}]),
            mock.patch.object(
                self.broker,
                "_inspect_container",
                return_value=running_worker_inspect("c" * 64),
            ),
            mock.patch.object(self.broker, "_stop_and_remove_owned") as stop_and_remove,
        ):
            removed = self.broker.cleanup_workers()

        self.assertEqual(removed, 1)
        stop_and_remove.assert_called_once()
        self.relay.stop.assert_called_once_with()
        self.assertFalse(valid_secret.exists())
        self.assertTrue(unrelated_secret.exists())

    def test_image_pull_does_not_block_shutdown_worker_cleanup(self) -> None:
        pull_entered = threading.Event()
        release_pull = threading.Event()
        cleanup_finished = threading.Event()
        prepare_errors: list[BaseException] = []
        cleanup_errors: list[BaseException] = []

        def blocking_pull() -> None:
            pull_entered.set()
            release_pull.wait(timeout=2)

        def run_prepare() -> None:
            try:
                self.broker.prepare()
            except BaseException as error:
                prepare_errors.append(error)

        def run_cleanup() -> None:
            try:
                self.broker.cleanup_workers()
            except BaseException as error:
                cleanup_errors.append(error)
            finally:
                cleanup_finished.set()

        with (
            mock.patch.object(self.broker, "_require_volume_subpath_api"),
            mock.patch.object(self.broker, "_validated_worker_network"),
            mock.patch.object(self.broker, "_data_volume", return_value="proxybridge-data"),
            mock.patch.object(self.broker, "_ensure_image", side_effect=blocking_pull),
            mock.patch.object(self.broker, "_list_raw", return_value=[]),
        ):
            prepare_thread = threading.Thread(target=run_prepare)
            cleanup_thread = threading.Thread(target=run_cleanup)
            prepare_thread.start()
            self.assertTrue(pull_entered.wait(timeout=1))
            cleanup_thread.start()
            try:
                self.assertTrue(
                    cleanup_finished.wait(timeout=0.5),
                    "worker 清理不应等待仍在进行的镜像 pull",
                )
            finally:
                release_pull.set()
            prepare_thread.join(timeout=2)
            cleanup_thread.join(timeout=2)

        self.assertFalse(prepare_thread.is_alive())
        self.assertFalse(cleanup_thread.is_alive())
        self.assertEqual(cleanup_errors, [])
        self.assertEqual(len(prepare_errors), 1)
        self.assertIn("正在退出", str(prepare_errors[0]))

    def test_docker_engine_client_versions_mocked_requests_and_rejects_engine_error(self) -> None:
        client = browser_broker.DockerEngineClient("/var/run/docker.sock")
        with mock.patch.object(
            client,
            "_raw_request",
            side_effect=[
                (200, b'{"ApiVersion":"1.45"}'),
                (201, b'{"Id":"worker-id"}'),
                (500, b'{"message":"daemon failed"}'),
            ],
        ) as raw_request:
            created = client.json_request(
                "POST",
                "/containers/create",
                {"Image": "worker"},
                expected=(201,),
            )
            self.assertEqual(created, {"Id": "worker-id"})
            with self.assertRaisesRegex(browser_broker.EngineError, "daemon failed"):
                client.request("GET", "/containers/bad/json")

        self.assertEqual(raw_request.call_args_list[0].args[:2], ("GET", "/version"))
        self.assertEqual(raw_request.call_args_list[1].args[:2], ("POST", "/v1.45/containers/create"))
        self.assertEqual(raw_request.call_args_list[2].args[:2], ("GET", "/v1.45/containers/bad/json"))

    def test_docker_engine_socket_timeout_is_not_reported_as_disconnect(self) -> None:
        connection = mock.Mock()
        connection.request.side_effect = TimeoutError("timed out")
        with mock.patch.object(browser_broker, "UnixHTTPConnection", return_value=connection):
            client = browser_broker.DockerEngineClient("/var/run/docker.sock")
            with self.assertRaisesRegex(browser_broker.EngineTimeoutError, "请求超时"):
                client._raw_request("POST", "/images/create", timeout=1.0)
        connection.close.assert_called_once_with()


class AppRemoteBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_work = app.WORK_ROOT
        self.old_sync_engine = app.SYNC_ENGINE
        self.old_client = app.REMOTE_BROWSER_CLIENT
        self.old_client_key = app.REMOTE_BROWSER_CLIENT_KEY
        self.old_cache = dict(app.REMOTE_BROWSER_SESSION_CACHE)
        with app.REMOTE_BROWSER_LIFECYCLE_CONDITION:
            self.old_generations = dict(app.REMOTE_BROWSER_GENERATIONS)
            self.old_pending_starts = dict(app.REMOTE_BROWSER_PENDING_STARTS)
            self.old_pending_start_tokens = dict(app.REMOTE_BROWSER_PENDING_START_TOKENS)
            self.old_operations = set(app.REMOTE_BROWSER_OPERATIONS)
            app.REMOTE_BROWSER_GENERATIONS.clear()
            app.REMOTE_BROWSER_PENDING_STARTS.clear()
            app.REMOTE_BROWSER_PENDING_START_TOKENS.clear()
            app.REMOTE_BROWSER_OPERATIONS.clear()
        self.temp = tempfile.TemporaryDirectory()
        app.configure_work_root(Path(self.temp.name) / "work")
        app.SYNC_ENGINE = None
        app.REMOTE_BROWSER_CLIENT = None
        app.REMOTE_BROWSER_CLIENT_KEY = None
        app.invalidate_remote_browser_session_cache()

    def tearDown(self) -> None:
        app.configure_work_root(self.old_work)
        app.SYNC_ENGINE = self.old_sync_engine
        app.REMOTE_BROWSER_CLIENT = self.old_client
        app.REMOTE_BROWSER_CLIENT_KEY = self.old_client_key
        app.REMOTE_BROWSER_SESSION_CACHE.update(self.old_cache)
        with app.REMOTE_BROWSER_LIFECYCLE_CONDITION:
            app.REMOTE_BROWSER_GENERATIONS.clear()
            app.REMOTE_BROWSER_GENERATIONS.update(self.old_generations)
            app.REMOTE_BROWSER_PENDING_STARTS.clear()
            app.REMOTE_BROWSER_PENDING_STARTS.update(self.old_pending_starts)
            app.REMOTE_BROWSER_PENDING_START_TOKENS.clear()
            app.REMOTE_BROWSER_PENDING_START_TOKENS.update(self.old_pending_start_tokens)
            app.REMOTE_BROWSER_OPERATIONS.clear()
            app.REMOTE_BROWSER_OPERATIONS.update(self.old_operations)
        self.temp.cleanup()

    def docker_browser_env(self, **extra: str):
        return mock.patch.dict(
            os.environ,
            {
                "PROXY_BRIDGE_DOCKER": "1",
                "PROXY_BRIDGE_DISABLE_BROWSER": "",
                "PROXY_BRIDGE_BROWSER_BROKER_URL": "http://browser-broker:8765",
                "PROXY_BRIDGE_BROWSER_BROKER_TOKEN_FILE": "/run/proxybridge-browser/token",
                "PROXY_BRIDGE_BROWSER_PROXY_HOST": "proxybridge",
                **extra,
            },
            clear=False,
        )

    def test_launch_does_not_persist_or_return_remote_browser_password(self) -> None:
        secret = "do-not-persist-this-password"

        class FakeBroker:
            def __init__(self) -> None:
                self.payload: dict[str, object] | None = None

            def prepare(self) -> dict[str, object]:
                return {"ok": True}

            def sessions(self) -> list[dict[str, object]]:
                return []

            def start(self, payload: dict[str, object]) -> dict[str, object]:
                self.payload = dict(payload)
                return {
                    "ok": True,
                    "url": "https://127.0.0.1:23000/",
                    "container_id": "c" * 64,
                    "host_port": 23000,
                    "status": "running",
                    "health": "healthy",
                    "worker_ip": "172.31.254.10",
                    "proxy_port": 21080,
                }

        broker = FakeBroker()
        with self.docker_browser_env():
            app.ensure_dirs()
            state = app.empty_state()
            state["nodes"] = [{"id": "node-1", "name": "Test node", "region": "CN"}]
            app.save_state(state)
            with (
                mock.patch.object(app, "synchronize_process_state"),
                mock.patch.object(app, "sync_browser_instance_status", return_value=False),
                mock.patch.object(
                    app,
                    "active_bridge_for_node",
                    return_value={"id": "bridge-1", "node_id": "node-1", "port": 21080, "status": "running"},
                ),
                mock.patch.object(app, "configure_browser_profile_defaults"),
                mock.patch.object(app, "remote_browser_fingerprint_relative", return_value="fingerprint-chromium/chromium"),
                mock.patch.object(app, "remote_browser_broker_client", return_value=broker),
                mock.patch.object(
                    app,
                    "remote_browser_broker_status",
                    return_value={"worker_image": "lscr.io/linuxserver/chromium:latest"},
                ),
                mock.patch.object(app, "mark_sync_local_change"),
            ):
                result = app.launch_browser(
                    {"node_id": "node-1", "username": "alice", "password": secret}
                )

            saved = app.load_state()

        self.assertIsNotNone(broker.payload)
        self.assertEqual(broker.payload["password"], secret)
        self.assertRegex(
            str(broker.payload["profile_rel"]),
            r"^browser_profiles/[a-f0-9]{16}/[a-f0-9]{16}$",
        )
        self.assertNotIn(secret, json.dumps(saved, ensure_ascii=False))
        self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))
        self.assertNotIn("password", result)
        self.assertNotIn("password", saved["browser_instances"][0])
        self.assertEqual(saved["browser_instances"][0]["remote_username"], "alice")
        self.assertEqual(saved["browser_instances"][0]["profile_layout"], "native-v1")

    def test_credentials_are_read_outside_state_locks_without_persisting_password(self) -> None:
        password = "correct-horse-battery-staple"
        lock_state: list[tuple[bool, bool]] = []

        class FakeBroker:
            def credentials(self, instance_id: str) -> dict[str, object]:
                state_free = app.STATE_LOCK.acquire(blocking=False)
                if state_free:
                    app.STATE_LOCK.release()
                shutdown_free = app.SHUTDOWN_LOCK.acquire(blocking=False)
                if shutdown_free:
                    app.SHUTDOWN_LOCK.release()
                lock_state.append((state_free, shutdown_free))
                return {
                    "ok": True,
                    "instance_id": instance_id,
                    "url": "https://127.0.0.1:23000/",
                    "username": "alice",
                    "password": password,
                }

        with self.docker_browser_env():
            app.ensure_dirs()
            state = app.empty_state()
            state["nodes"] = [{"id": "node-1", "name": "Test node"}]
            instance = app.create_browser_instance_record(state, "node-1")
            instance.update(
                {
                    "status": "running",
                    "remote_worker": True,
                    "remote_url": "https://127.0.0.1:23000/",
                    "remote_username": "alice",
                }
            )
            state["browser_instances"] = [instance]
            app.save_state(state)
            state_before = app.STATE_PATH.read_bytes()
            with mock.patch.object(
                app,
                "remote_browser_broker_client",
                return_value=FakeBroker(),
            ):
                result = app.remote_browser_credentials({"instance_id": instance["id"]})
            state_after = app.STATE_PATH.read_bytes()

        self.assertEqual(result["password"], password)
        self.assertEqual(result["username"], "alice")
        self.assertEqual(lock_state, [(True, True)])
        self.assertEqual(state_after, state_before)
        self.assertNotIn(password.encode("utf-8"), state_after)

    def test_invalid_worker_result_is_reclaimed_and_persisted_as_failed(self) -> None:
        class FakeBroker:
            def __init__(self) -> None:
                self.stopped: list[str] = []

            def prepare(self) -> dict[str, object]:
                return {"ok": True}

            def sessions(self) -> list[dict[str, object]]:
                return []

            def start(self, _payload: dict[str, object]) -> dict[str, object]:
                return {
                    "ok": True,
                    "url": "https://evil.example:23000/",
                    "container_id": "c" * 64,
                    "host_port": 23000,
                    "status": "running",
                    "health": "healthy",
                    "worker_ip": "172.31.254.10",
                    "proxy_port": 21080,
                }

            def stop(
                self,
                instance_id: str,
                _cancelled_operation_id: str = "",
            ) -> dict[str, object]:
                self.stopped.append(instance_id)
                return {"ok": True, "stopped": 1}

        broker = FakeBroker()
        with self.docker_browser_env():
            app.ensure_dirs()
            state = app.empty_state()
            state["nodes"] = [{"id": "node-1", "name": "Test node", "region": "CN"}]
            app.save_state(state)
            with (
                mock.patch.object(app, "synchronize_process_state"),
                mock.patch.object(app, "sync_browser_instance_status", return_value=False),
                mock.patch.object(
                    app,
                    "active_bridge_for_node",
                    return_value={"id": "bridge-1", "node_id": "node-1", "port": 21080},
                ),
                mock.patch.object(app, "configure_browser_profile_defaults"),
                mock.patch.object(
                    app,
                    "remote_browser_fingerprint_relative",
                    return_value="fingerprint-chromium/chromium",
                ),
                mock.patch.object(app, "remote_browser_broker_client", return_value=broker),
                mock.patch.object(app, "mark_sync_local_change"),
            ):
                with self.assertRaisesRegex(RuntimeError, "有效的私有 HTTPS"):
                    app.launch_browser(
                        {"node_id": "node-1", "username": "alice", "password": "strong-password"}
                    )
            saved = app.load_state()

        instance = saved["browser_instances"][0]
        self.assertEqual(instance["status"], "failed")
        self.assertEqual(instance["remote_url"], "")
        self.assertEqual(broker.stopped, [instance["id"]])

    def test_stop_cancels_inflight_start_and_never_restores_running_state(self) -> None:
        start_entered = threading.Event()
        release_start = threading.Event()

        class FakeBroker:
            def __init__(self) -> None:
                self.stopped: list[str] = []

            def prepare(self) -> dict[str, object]:
                return {"ok": True}

            def start(self, payload: dict[str, object]) -> dict[str, object]:
                start_entered.set()
                self.instance_id = str(payload["instance_id"])
                release_start.wait(timeout=3)
                return {
                    "ok": True,
                    "url": "https://127.0.0.1:23000/",
                    "container_id": "c" * 64,
                    "host_port": 23000,
                    "status": "running",
                    "health": "healthy",
                    "worker_ip": "172.31.254.10",
                    "proxy_port": 21080,
                }

            def stop(
                self,
                instance_id: str,
                _cancelled_operation_id: str = "",
            ) -> dict[str, object]:
                self.stopped.append(instance_id)
                return {"ok": True, "stopped": 1, "instance_id": instance_id}

        broker = FakeBroker()
        errors: list[BaseException] = []
        with self.docker_browser_env():
            app.ensure_dirs()
            state = app.empty_state()
            state["nodes"] = [{"id": "node-1", "name": "Test node", "region": "CN"}]
            app.save_state(state)
            patches = (
                mock.patch.object(app, "synchronize_process_state"),
                mock.patch.object(app, "remote_browser_sessions", return_value=({}, "")),
                mock.patch.object(
                    app,
                    "active_bridge_for_node",
                    return_value={"id": "bridge-1", "node_id": "node-1", "port": 21080},
                ),
                mock.patch.object(app, "configure_browser_profile_defaults"),
                mock.patch.object(
                    app,
                    "remote_browser_fingerprint_relative",
                    return_value="fingerprint-chromium/chromium",
                ),
                mock.patch.object(app, "remote_browser_broker_client", return_value=broker),
                mock.patch.object(app, "mark_sync_local_change"),
                mock.patch.object(app, "maybe_auto_upload_browser_instances"),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
                thread = threading.Thread(
                    target=lambda: self._capture_error(
                        errors,
                        lambda: app.launch_browser(
                            {"node_id": "node-1", "username": "alice", "password": "strong-password"}
                        ),
                    )
                )
                thread.start()
                self.assertTrue(start_entered.wait(timeout=2))
                starting = app.load_state()["browser_instances"][0]
                stopped = app.stop_browser({"instance_id": starting["id"]})
                self.assertEqual(stopped["killed_browsers"], 1)
                release_start.set()
                thread.join(timeout=3)

            saved = app.load_state()["browser_instances"][0]

        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertIn("失效", str(errors[0]))
        self.assertEqual(saved["status"], "stopped")
        self.assertEqual(saved["remote_url"], "")
        self.assertGreaterEqual(broker.stopped.count(saved["id"]), 2)

    @staticmethod
    def _capture_error(errors: list[BaseException], callback) -> None:
        try:
            callback()
        except BaseException as error:
            errors.append(error)

    def test_remote_stop_calls_broker_without_state_or_shutdown_lock(self) -> None:
        lock_state: list[tuple[bool, bool]] = []

        class FakeBroker:
            def stop(
                self,
                instance_id: str,
                _cancelled_operation_id: str = "",
            ) -> dict[str, object]:
                state_free = app.STATE_LOCK.acquire(blocking=False)
                if state_free:
                    app.STATE_LOCK.release()
                shutdown_free = app.SHUTDOWN_LOCK.acquire(blocking=False)
                if shutdown_free:
                    app.SHUTDOWN_LOCK.release()
                lock_state.append((state_free, shutdown_free))
                return {"ok": True, "stopped": 1, "instance_id": instance_id}

        with self.docker_browser_env():
            app.ensure_dirs()
            state = app.empty_state()
            state["nodes"] = [{"id": "node-1", "name": "Test node"}]
            instance = app.create_browser_instance_record(state, "node-1")
            instance.update({"status": "running", "remote_worker": True})
            state["browser_instances"] = [instance]
            app.save_state(state)
            with (
                mock.patch.object(app, "remote_browser_broker_client", return_value=FakeBroker()),
                mock.patch.object(app, "maybe_auto_upload_browser_instances"),
            ):
                app.stop_browser({"instance_id": instance["id"]})

        self.assertEqual(lock_state, [(True, True)])

    def test_shutdown_stops_remote_workers_concurrently_without_shutdown_lock(self) -> None:
        instance_ids = ["a" * 16, "b" * 16]
        stop_barrier = threading.Barrier(len(instance_ids))
        observations: list[tuple[str, bool, float]] = []
        observation_lock = threading.Lock()

        class FakeBroker:
            def stop(
                self,
                instance_id: str,
                _cancelled_operation_id: str = "",
                *,
                timeout: float | None = None,
            ) -> dict[str, object]:
                shutdown_free = app.SHUTDOWN_LOCK.acquire(blocking=False)
                if shutdown_free:
                    app.SHUTDOWN_LOCK.release()
                with observation_lock:
                    observations.append((instance_id, shutdown_free, float(timeout or 0.0)))
                stop_barrier.wait(timeout=1)
                return {"ok": True, "stopped": 1, "instance_id": instance_id}

        state = app.empty_state()
        state["browser_instances"] = [
            {"id": instance_id, "status": "running"}
            for instance_id in instance_ids
        ]
        sessions = {
            instance_id: {"instance_id": instance_id, "status": "running"}
            for instance_id in instance_ids
        }
        with self.docker_browser_env():
            with (
                mock.patch.object(app, "load_state", return_value=state),
                mock.patch.object(
                    app,
                    "remote_browser_sessions",
                    return_value=(sessions, ""),
                ) as session_call,
                mock.patch.object(app, "remote_browser_broker_client", return_value=FakeBroker()),
            ):
                remote_ids, stopped_ids, failures = app._shutdown_remote_browser_workers()

        self.assertEqual(failures, [])
        self.assertCountEqual(remote_ids, instance_ids)
        self.assertCountEqual(stopped_ids, instance_ids)
        self.assertCountEqual([item[0] for item in observations], instance_ids)
        self.assertTrue(all(item[1] for item in observations))
        self.assertTrue(all(item[2] == 45.0 for item in observations))
        session_call.assert_called_once_with(force=True, timeout=10.0)

    def test_broker_outage_is_reported_and_running_state_fails_closed(self) -> None:
        client = mock.Mock(spec=remote_browser.RemoteBrowserBrokerClient)
        client.sessions.side_effect = remote_browser.RemoteBrowserError("broker offline")
        client.status.side_effect = remote_browser.RemoteBrowserError("broker offline")
        with self.docker_browser_env(), mock.patch.object(
            app, "remote_browser_broker_client", return_value=client
        ):
            app.invalidate_remote_browser_session_cache()
            sessions, error = app.remote_browser_sessions(force=True)
            self.assertIsNone(sessions)
            self.assertIn("broker offline", error)
            self.assertTrue(app.browser_process_is_active("0123456789abcdef"))
            status = app.remote_browser_broker_status()

        self.assertTrue(status["configured"])
        self.assertFalse(status["available"])
        self.assertIn("broker offline", status["error"])

    def test_session_cache_invalidation_discards_inflight_stale_snapshot(self) -> None:
        first_entered = threading.Event()
        release_first = threading.Event()

        class FakeBroker:
            def __init__(self) -> None:
                self.calls = 0

            def sessions(self) -> list[dict[str, object]]:
                self.calls += 1
                if self.calls == 1:
                    first_entered.set()
                    release_first.wait(timeout=3)
                    return [{"instance_id": "aaaaaaaaaaaaaaaa", "status": "running"}]
                return [{"instance_id": "bbbbbbbbbbbbbbbb", "status": "running"}]

        broker = FakeBroker()
        results: list[tuple[dict[str, dict[str, object]] | None, str]] = []
        with self.docker_browser_env(), mock.patch.object(
            app,
            "remote_browser_broker_client",
            return_value=broker,
        ):
            thread = threading.Thread(
                target=lambda: results.append(app.remote_browser_sessions(force=True))
            )
            thread.start()
            self.assertTrue(first_entered.wait(timeout=2))
            app.invalidate_remote_browser_session_cache()
            release_first.set()
            thread.join(timeout=3)

        self.assertFalse(thread.is_alive())
        self.assertEqual(broker.calls, 2)
        self.assertEqual(set((results[0][0] or {}).keys()), {"bbbbbbbbbbbbbbbb"})
        self.assertEqual(
            set((app.REMOTE_BROWSER_SESSION_CACHE.get("sessions") or {}).keys()),
            {"bbbbbbbbbbbbbbbb"},
        )

    def test_invalidated_empty_session_cache_is_not_treated_as_fresh(self) -> None:
        broker = mock.Mock()
        broker.sessions.return_value = [
            {"instance_id": "aaaaaaaaaaaaaaaa", "status": "running"}
        ]
        with (
            self.docker_browser_env(),
            mock.patch.object(app, "remote_browser_broker_client", return_value=broker),
            mock.patch.object(app.time, "monotonic", return_value=0.5),
        ):
            app.invalidate_remote_browser_session_cache()
            sessions, error = app.remote_browser_sessions()

        self.assertEqual(error, "")
        self.assertEqual(set((sessions or {}).keys()), {"aaaaaaaaaaaaaaaa"})
        broker.sessions.assert_called_once_with()

    def test_remote_url_normalization_rejects_non_private_or_state_injected_links(self) -> None:
        self.assertEqual(
            app.normalize_remote_browser_url("https://192.168.1.20:23000/"),
            "https://192.168.1.20:23000/",
        )
        self.assertEqual(
            app.normalize_remote_browser_url("https://100.64.0.8:23000/"),
            "https://100.64.0.8:23000/",
        )
        for value in (
            "http://192.168.1.20:23000/",
            "https://8.8.8.8:23000/",
            "https://192.168.1.20:23000/?token=bad",
            "javascript:alert(1)",
        ):
            with self.subTest(value=value):
                self.assertEqual(app.normalize_remote_browser_url(value), "")

    def test_restarted_worker_refreshes_stale_empty_session_cache_for_proxy_access(self) -> None:
        instance_id = "0123456789abcdef"
        broker = mock.Mock()
        broker.sessions.return_value = [
            {
                "instance_id": instance_id,
                "status": "starting",
                "worker_ip": "172.31.254.42",
                "proxy_port": 21080,
            }
        ]
        with self.docker_browser_env(), mock.patch.object(
            app,
            "remote_browser_broker_client",
            return_value=broker,
        ):
            generation, _operation_id = app.begin_remote_browser_start(instance_id)
            with app.REMOTE_BROWSER_SESSION_CONDITION:
                app.REMOTE_BROWSER_SESSION_CACHE.update(
                    {
                        "checked_at": time.monotonic(),
                        "sessions": {},
                        "error": "",
                        "fetching": False,
                    }
                )

            self.assertTrue(app.remote_browser_worker_client("172.31.254.42", 21080))
            app.finish_remote_browser_start(instance_id, generation)
            self.assertFalse(app.remote_browser_worker_client("172.31.254.43", 21080))
            self.assertFalse(app.remote_browser_worker_client("172.31.254.42", 21081))

        broker.sessions.assert_called_once_with()

    def test_worker_proxy_access_without_pending_start_uses_cached_sessions(self) -> None:
        broker = mock.Mock()
        broker.sessions.return_value = [
            {
                "instance_id": "0123456789abcdef",
                "status": "running",
                "worker_ip": "172.31.254.42",
                "proxy_port": 21080,
            }
        ]
        with self.docker_browser_env(), mock.patch.object(
            app,
            "remote_browser_broker_client",
            return_value=broker,
        ):
            with app.REMOTE_BROWSER_SESSION_CONDITION:
                app.REMOTE_BROWSER_SESSION_CACHE.update(
                    {
                        "checked_at": time.monotonic(),
                        "sessions": {},
                        "error": "",
                        "fetching": False,
                    }
                )
            self.assertFalse(app.remote_browser_worker_client("172.31.254.42", 21080))

        broker.sessions.assert_not_called()

    def test_only_exact_worker_ip_and_assigned_proxy_port_bypass_authentication(self) -> None:
        security = {
            "auth_mode": "basic",
            "username": "alice",
            "password": "secret",
            "allowed_cidrs": ["192.168.1.0/24"],
            "max_connections": 5,
        }
        proxy = app.Socks5LogProxy(
            bridge_id="bridge-1",
            node_id="node-1",
            node_name="node",
            listen_port=21080,
            upstream_port=22080,
            listen_host=app.LAN_PROXY_LISTEN_HOST,
            security=security,
        )
        sessions = {
            "0123456789abcdef": {
                "instance_id": "0123456789abcdef",
                "status": "running",
                "worker_ip": "172.31.254.42",
                "proxy_port": 21080,
            }
        }
        with self.docker_browser_env(), mock.patch.object(
            app,
            "remote_browser_sessions",
            return_value=(sessions, ""),
        ):
            self.assertTrue(app.remote_browser_worker_client("172.31.254.42", 21080))
            self.assertFalse(app.remote_browser_worker_client("172.31.254.42", 21081))
            self.assertFalse(app.remote_browser_worker_client("172.31.254.43", 21080))
            self.assertFalse(app.remote_browser_worker_client("172.31.254.42"))
            self.assertFalse(proxy._client_requires_auth(("172.31.254.42", 50000)))
            self.assertTrue(proxy._client_requires_auth(("172.31.254.43", 50000)))

            sessions["0123456789abcdef"]["status"] = "starting"
            self.assertTrue(app.remote_browser_worker_client("172.31.254.42", 21080))

        with self.docker_browser_env(
            PROXY_BRIDGE_BROWSER_BROKER_URL="",
        ):
            self.assertFalse(app.remote_browser_worker_client("172.31.254.42", 21080))
            self.assertTrue(proxy._client_requires_auth(("172.31.254.42", 50000)))

    def test_stopping_instance_stays_active_while_worker_is_initializing(self) -> None:
        state = app.empty_state()
        state["browser_instances"] = [
            {
                "id": "0123456789abcdef",
                "node_id": "node-1",
                "status": "stopping",
                "remote_worker": True,
            }
        ]
        sessions = {
            "0123456789abcdef": {
                "instance_id": "0123456789abcdef",
                "status": "starting",
                "container_id": "c" * 64,
                "host_port": 23000,
                "url": "https://127.0.0.1:23000/",
            }
        }
        with self.docker_browser_env(), mock.patch.object(
            app,
            "maybe_auto_upload_browser_instances",
        ) as auto_upload:
            changed = app.sync_browser_instance_status(
                state,
                remote_sessions_snapshot=sessions,
            )

        self.assertTrue(changed)
        self.assertEqual(state["browser_instances"][0]["status"], "stopping")
        auto_upload.assert_not_called()


class RemoteBrowserDeploymentContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]

    def test_main_and_broker_dockerfiles_copy_only_required_remote_modules(self) -> None:
        main = (self.root / "Dockerfile").read_text(encoding="utf-8")
        broker = (self.root / "docker" / "Dockerfile.broker").read_text(encoding="utf-8")

        self.assertIn(
            "COPY --chown=proxybridge:proxybridge app.py webdav_sync.py remote_browser.py browser_fingerprint.py ./",
            main,
        )
        self.assertIn("USER proxybridge", main)
        self.assertIn("COPY browser_broker.py ./", broker)
        self.assertIn("EXPOSE 8765", broker)
        self.assertIn("http://127.0.0.1:8765/healthz", broker)
        self.assertIn('CMD ["python", "browser_broker.py"]', broker)

    def test_frontend_exposes_delete_and_runtime_credential_controls(self) -> None:
        html = (self.root / "static" / "index.html").read_text(encoding="utf-8")
        frontend = (self.root / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="remoteBrowserCredentialsModal"', html)
        self.assertIn('id="remoteBrowserCredentialsPassword"', html)
        self.assertIn('data-action="copy-viewed-remote-browser-credentials"', html)
        self.assertIn('data-action="delete-browser-instance"', frontend)
        self.assertIn('data-action="view-remote-browser-credentials"', frontend)
        self.assertIn('api("/api/browser/delete"', frontend)
        self.assertIn('api("/api/browser/credentials"', frontend)
        self.assertIn("browserUi.copyText(credentials.password, window)", frontend)

    def test_compose_keeps_docker_socket_out_of_web_service_and_isolates_workers(self) -> None:
        compose = (self.root / "compose.yaml").read_text(encoding="utf-8")
        proxybridge_section, remainder = compose.split("\n  browser-broker:", 1)
        broker_section, deployment_tail = remainder.split("\nvolumes:", 1)

        self.assertNotIn("PROXY_BRIDGE_BROWSER_WORKER_CIDRS", compose)
        self.assertNotIn("/var/run/docker.sock", proxybridge_section)
        self.assertNotIn("browser-ingress", proxybridge_section)
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock:ro", broker_section)
        self.assertRegex(broker_section, r"(?m)^    ports:")
        self.assertIn(
            '"${PROXY_BRIDGE_BROWSER_HOST:-127.0.0.1}:'
            '${PROXY_BRIDGE_BROWSER_PORT_START:-23000}-'
            '${PROXY_BRIDGE_BROWSER_PORT_END:-23099}:'
            '${PROXY_BRIDGE_BROWSER_PORT_START:-23000}-'
            '${PROXY_BRIDGE_BROWSER_PORT_END:-23099}"',
            broker_section,
        )
        data_mounts = [
            line.strip().removeprefix("- ")
            for line in proxybridge_section.splitlines()
            if ":/data" in line
        ]
        self.assertEqual(data_mounts, ["proxybridge-data:/data"])
        self.assertIn("proxybridge-data:\n    name: proxybridge-data", deployment_tail)
        self.assertIn("proxybridge-browser-control:/run/proxybridge-browser:ro", proxybridge_section)
        self.assertIn("proxybridge-browser-control:/control", broker_section)
        self.assertIn('PROXY_BRIDGE_BROWSER_BROKER_URL: "http://browser-broker:8765"', proxybridge_section)
        self.assertIn('PROXY_BRIDGE_BROWSER_HOST: "${PROXY_BRIDGE_BROWSER_HOST:-127.0.0.1}"', broker_section)
        self.assertIn('PROXY_BRIDGE_BROWSER_PUBLIC_HOST: "${PROXY_BRIDGE_BROWSER_PUBLIC_HOST:-127.0.0.1}"', broker_section)
        self.assertIn('PROXY_BRIDGE_BROWSER_PROXY_HOST: "proxybridge"', broker_section)
        self.assertIn("      - browser-workers", broker_section)
        self.assertIn("      - browser-ingress", broker_section)
        self.assertGreaterEqual(deployment_tail.count("internal: true"), 2)
        self.assertIn("proxybridge-browser-ingress", deployment_tail)
        self.assertIn("internal: false", deployment_tail)
        self.assertIn('subnet: "${PROXY_BRIDGE_BROWSER_NETWORK_SUBNET:-172.31.254.0/24}"', deployment_tail)
        self.assertIn("cap_drop:\n      - ALL", broker_section)
        self.assertIn("no-new-privileges:true", broker_section)
        self.assertEqual(proxybridge_section.count("stop_grace_period: 3m"), 1)
        self.assertEqual(broker_section.count("stop_grace_period: 3m"), 1)

    def test_linux_zip_install_restores_wrapper_executable_and_sync_drops_runtime_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = root / "fingerprint-linux.zip"
            extract_path = root / "extract"
            extract_path.mkdir()
            info = zipfile.ZipInfo("chromium/chrome-wrapper")
            info.external_attr = 0
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(info, "#!/bin/sh\n")
            app._safe_extract_zip(archive_path, extract_path)
            self.assertTrue(os.access(extract_path / "chromium" / "chrome-wrapper", os.X_OK))

        sanitized = webdav_sync.sanitize_browser_instance_for_sync(
            {
                "id": "0123456789abcdef",
                "node_id": "fedcba9876543210",
                "status": "running",
                "remote_worker": True,
                "remote_url": "https://192.168.1.20:23000/",
                "remote_container_id": "c" * 64,
                "remote_host_port": 23000,
                "remote_username": "alice",
                "profile_layout": "remote-worker-v1",
            }
        )
        self.assertEqual(sanitized["status"], "stopped")
        self.assertFalse(sanitized["remote_worker"])
        self.assertEqual(sanitized["remote_url"], "")
        self.assertEqual(sanitized["remote_container_id"], "")
        self.assertIsNone(sanitized["remote_host_port"])
        self.assertEqual(sanitized["remote_username"], "")
        self.assertEqual(sanitized["profile_layout"], "remote-worker-v1")

    def test_remote_worker_selects_amd64_fingerprint_asset_on_arm64_main(self) -> None:
        releases = [
            {
                "tag_name": "v1",
                "assets": [
                    {
                        "name": "fingerprint-chromium_linux_arm64.zip",
                        "browser_download_url": "https://example/arm64.zip",
                    },
                    {
                        "name": "fingerprint-chromium_linux_x64.zip",
                        "browser_download_url": "https://example/amd64.zip",
                    },
                ],
            }
        ]
        selected = app._pick_fingerprint_asset(
            releases,
            host_os="linux",
            host_arch="x86_64",
            strict_arch=True,
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["url"], "https://example/amd64.zip")


if __name__ == "__main__":
    unittest.main()
