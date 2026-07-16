const assert = require("node:assert/strict");
const test = require("node:test");

const diagnostics = require("../static/diagnostics.js");

test("classifies IPv4, IPv6, and mDNS addresses", () => {
  assert.equal(diagnostics.classifyAddress("192.168.1.2"), "private");
  assert.equal(diagnostics.classifyAddress("100.64.1.2"), "private");
  assert.equal(diagnostics.classifyAddress("8.8.8.8"), "public");
  assert.equal(diagnostics.classifyAddress("203.0.113.8"), "reserved");
  assert.equal(diagnostics.classifyAddress("fd12::1"), "private");
  assert.equal(diagnostics.classifyAddress("fe80::1%en0"), "link_local");
  assert.equal(diagnostics.classifyAddress("host-123.local"), "mdns");
});

test("parses ICE candidates and does not overstate safety", () => {
  const candidate = diagnostics.parseIceCandidate(
    "candidate:1 1 udp 2122260223 192.168.1.7 54321 typ host generation 0"
  );
  assert.deepEqual(candidate, {
    protocol: "udp",
    address: "192.168.1.7",
    port: 54321,
    type: "host",
    privacy: "private",
  });
  assert.equal(diagnostics.summarizeCandidates([]).status, "warn");
  assert.equal(
    diagnostics.summarizeCandidates(["candidate:1 1 udp 1 8.8.8.8 5000 typ host"]).status,
    "error"
  );
  assert.equal(
    diagnostics.summarizeCandidates(["candidate:1 1 udp 1 hidden.example 5000 typ host"]).status,
    "warn"
  );
});

test("builds an explicit backend failure result", () => {
  const result = diagnostics.failedBackendResult(new Error("backend unavailable"));
  assert.equal(result.summary.error, 1);
  assert.equal(result.results[0].status, "error");
  assert.match(result.results[0].message, /backend unavailable/);
});

test("collects browser candidates without a STUN server", async () => {
  class FakeConnection {
    constructor(configuration) {
      this.configuration = configuration;
      FakeConnection.last = this;
    }

    createDataChannel() {}

    async createOffer() {
      return { type: "offer", sdp: "" };
    }

    async setLocalDescription() {
      this.onicecandidate({
        candidate: { candidate: "candidate:1 1 udp 1 device.local 5000 typ host" },
      });
      this.onicecandidate({ candidate: null });
    }

    close() {
      this.closed = true;
    }
  }

  const result = await diagnostics.collectWebrtcCandidates({
    RTCPeerConnectionCtor: FakeConnection,
    timeoutMs: 100,
  });

  assert.deepEqual(FakeConnection.last.configuration, { iceServers: [] });
  assert.equal(FakeConnection.last.closed, true);
  assert.equal(result.status, "warn");
  assert.equal(result.details.candidate_count, 1);
});

test("reports unsupported WebRTC as skipped", async () => {
  const result = await diagnostics.collectWebrtcCandidates({ RTCPeerConnectionCtor: null });
  if (typeof globalThis.RTCPeerConnection !== "function") {
    assert.equal(result.status, "skipped");
  }
});
