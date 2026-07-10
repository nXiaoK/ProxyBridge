import socket
import threading
import unittest

import app
from app import Socks5LogProxy, read_exact


class ProxyRegressionTests(unittest.TestCase):
    @staticmethod
    def _proxy(bridge_id: str) -> Socks5LogProxy:
        return Socks5LogProxy(
            bridge_id=bridge_id,
            node_id="node-test",
            node_name="测试节点",
            listen_port=1,
            upstream_port=2,
        )

    @staticmethod
    def _read_until_eof(sock: socket.socket) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                return b"".join(chunks), False
            if not chunk:
                return b"".join(chunks), True
            chunks.append(chunk)

    def test_relay_preserves_response_after_client_half_closes(self):
        proxy = self._proxy("bridge-half-close-response")
        client_proxy, client_peer = socket.socketpair()
        upstream_proxy, upstream_peer = socket.socketpair()
        client_peer.settimeout(2.0)
        upstream_peer.settimeout(2.0)
        result: dict[str, dict[str, int]] = {}

        def run_relay() -> None:
            result["counters"] = proxy._relay(client_proxy, upstream_proxy)

        relay_thread = threading.Thread(target=run_relay, daemon=True)
        relay_thread.start()
        request = b"request-body"
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 13\r\n\r\nresponse-body"
        response_write_error: OSError | None = None
        try:
            client_peer.sendall(request)
            client_peer.shutdown(socket.SHUT_WR)

            self.assertEqual(read_exact(upstream_peer, len(request)), request)
            self.assertEqual(upstream_peer.recv(1), b"")
            try:
                upstream_peer.sendall(response)
            except OSError as exc:
                response_write_error = exc
            try:
                upstream_peer.shutdown(socket.SHUT_WR)
            except OSError:
                pass

            received, saw_eof = self._read_until_eof(client_peer)
            self.assertEqual(received, response, f"upstream response write failed: {response_write_error!r}")
            self.assertTrue(saw_eof, "relay did not propagate upstream EOF to the client")
            relay_thread.join(timeout=2.0)
            self.assertFalse(relay_thread.is_alive())
            self.assertEqual(result["counters"], {"upload": len(request), "download": len(response)})
        finally:
            proxy._stop.set()
            for sock in (client_proxy, client_peer, upstream_proxy, upstream_peer):
                try:
                    sock.close()
                except OSError:
                    pass
            relay_thread.join(timeout=2.0)

    def test_http_forward_forces_upstream_connection_close(self):
        proxy = self._proxy("bridge-http-close")
        request = proxy._parse_http_request(
            b"GET http://example.com/a HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"Connection: keep-alive\r\n"
            b"Proxy-Connection: keep-alive\r\n"
            b"Keep-Alive: timeout=5\r\n"
            b"Proxy-Authorization: Basic secret\r\n"
            b"\r\n"
        )

        _, _, head, _ = proxy._http_forward_plan(request)
        lowered = head.lower()

        self.assertNotIn(b"connection: keep-alive", lowered)
        self.assertNotIn(b"proxy-connection:", lowered)
        self.assertNotIn(b"keep-alive:", lowered)
        self.assertNotIn(b"proxy-authorization:", lowered)
        self.assertEqual(lowered.count(b"connection:"), 1)
        self.assertIn(b"connection: close\r\n", lowered)

    def test_http_request_rejects_header_that_crosses_limit_in_final_recv(self):
        proxy = self._proxy("bridge-http-header-limit")
        proxy_side, peer_side = socket.socketpair()
        try:
            peer_side.sendall(b"B\r\n\r\n")
            with self.assertRaisesRegex(RuntimeError, "HTTP 代理请求头过大"):
                proxy._read_http_request(proxy_side, b"A" * app.HTTP_PROXY_HEADER_LIMIT)
        finally:
            proxy_side.close()
            peer_side.close()


if __name__ == "__main__":
    unittest.main()
