(function (root, factory) {
  const diagnostics = factory();
  if (typeof module === "object" && module.exports) module.exports = diagnostics;
  root.ProxyBridgeDiagnostics = diagnostics;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function ipv4Parts(address) {
    const parts = String(address || "").split(".");
    if (parts.length !== 4) return null;
    const values = parts.map((part) => Number(part));
    if (values.some((part, index) => !/^\d+$/.test(parts[index]) || part < 0 || part > 255)) return null;
    return values;
  }

  function classifyAddress(rawAddress) {
    const address = String(rawAddress || "").trim().replace(/^\[|\]$/g, "").split("%")[0].toLowerCase();
    if (!address) return "unknown";
    if (address.endsWith(".local")) return "mdns";
    const ipv4 = ipv4Parts(address.replace(/^::ffff:/, ""));
    if (ipv4) {
      const [first, second] = ipv4;
      if (first === 127) return "loopback";
      if (first === 169 && second === 254) return "link_local";
      if (
        first === 10
        || (first === 172 && second >= 16 && second <= 31)
        || (first === 192 && second === 168)
        || (first === 100 && second >= 64 && second <= 127)
      ) return "private";
      if (
        first === 0
        || first >= 224
        || (first === 192 && second === 0)
        || (first === 192 && second === 88)
        || (first === 198 && [18, 19, 51].includes(second))
        || (first === 203 && second === 0)
      ) return "reserved";
      return "public";
    }
    if (!address.includes(":")) return "hostname";
    if (address === "::1") return "loopback";
    if (address === "::") return "reserved";
    if (address.startsWith("fc") || address.startsWith("fd")) return "private";
    if (/^fe[89ab]/.test(address)) return "link_local";
    if (
      address.startsWith("ff")
      || address.startsWith("2001:db8:")
      || address.startsWith("2001:2:")
    ) return "reserved";
    return "public";
  }

  function parseIceCandidate(candidateLine) {
    const line = String(candidateLine || "").trim().replace(/^a=/, "");
    if (!line.startsWith("candidate:")) return null;
    const parts = line.split(/\s+/);
    if (parts.length < 8) return null;
    const typeIndex = parts.indexOf("typ");
    const port = Number(parts[5]);
    if (typeIndex < 0 || !Number.isInteger(port) || port < 1 || port > 65535) return null;
    const address = parts[4];
    return {
      protocol: String(parts[2] || "").toLowerCase(),
      address,
      port,
      type: String(parts[typeIndex + 1] || "unknown").toLowerCase(),
      privacy: classifyAddress(address),
    };
  }

  function summarizeCandidates(candidateLines) {
    const unique = new Map();
    for (const line of candidateLines || []) {
      const candidate = parseIceCandidate(line);
      if (!candidate) continue;
      unique.set(`${candidate.protocol}|${candidate.address}|${candidate.port}|${candidate.type}`, candidate);
    }
    const candidates = [...unique.values()];
    const details = { candidate_count: candidates.length, candidates };
    if (!candidates.length) {
      return {
        status: "warn",
        message: "未发现候选；可能被 mDNS 或浏览器策略隐藏，不能据此判定安全",
        details,
      };
    }
    if (candidates.some((candidate) => candidate.privacy === "public")) {
      return { status: "error", message: "发现 WebRTC 公网地址候选", details };
    }
    if (candidates.some((candidate) => ["private", "link_local"].includes(candidate.privacy))) {
      return { status: "warn", message: "发现可见的本地网络地址候选", details };
    }
    if (candidates.some((candidate) => candidate.privacy === "mdns")) {
      return { status: "warn", message: "本地候选已由 mDNS 隐藏；未使用外部 STUN，风险仍需结合浏览器策略判断", details };
    }
    if (candidates.some((candidate) => candidate.privacy === "hostname" || candidate.privacy === "unknown")) {
      return { status: "warn", message: "存在无法分类的 WebRTC 候选，不能据此判定安全", details };
    }
    return { status: "ok", message: "仅发现回环或保留地址候选", details };
  }

  async function collectWebrtcCandidates({ RTCPeerConnectionCtor, timeoutMs = 2500 } = {}) {
    const Connection = RTCPeerConnectionCtor || globalThis.RTCPeerConnection;
    if (typeof Connection !== "function") {
      return { status: "skipped", message: "当前浏览器不支持 WebRTC 候选检查", details: {} };
    }
    const connection = new Connection({ iceServers: [] });
    const candidateLines = [];
    try {
      await new Promise(async (resolve, reject) => {
        let settled = false;
        const finish = () => {
          if (settled) return;
          settled = true;
          resolve();
        };
        const timer = globalThis.setTimeout(finish, Math.max(250, Number(timeoutMs) || 2500));
        connection.onicecandidate = (event) => {
          const line = event?.candidate?.candidate;
          if (line) candidateLines.push(line);
          else {
            globalThis.clearTimeout(timer);
            finish();
          }
        };
        try {
          connection.createDataChannel("proxybridge-diagnostic");
          const offer = await connection.createOffer();
          await connection.setLocalDescription(offer);
        } catch (error) {
          globalThis.clearTimeout(timer);
          if (!settled) {
            settled = true;
            reject(error);
          }
        }
      });
      return summarizeCandidates(candidateLines);
    } finally {
      connection.close();
    }
  }

  function failedBackendResult(error) {
    return {
      generated_at: Math.floor(Date.now() / 1000),
      network_requested: false,
      selected_bridge_id: "",
      results: [{
        id: "backend",
        label: "后端诊断",
        category: "local",
        status: "error",
        latency_ms: 0,
        message: String(error?.message || "诊断请求失败").slice(0, 500),
        details: {},
      }],
      summary: { ok: 0, warn: 0, error: 1, skipped: 0 },
    };
  }

  return { classifyAddress, parseIceCandidate, summarizeCandidates, collectWebrtcCandidates, failedBackendResult };
});
