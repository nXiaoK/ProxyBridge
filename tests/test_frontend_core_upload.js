const assert = require("node:assert/strict");
const test = require("node:test");

const coreUpload = require("../static/core-upload.js");

const TOKEN = "01234567-89AB-CDEF-0123-456789ABCDEF";

function makeAuth({ valid = true } = {}) {
  return {
    headerName: "X-ProxyBridge-Instance",
    calls: [],
    validateResponseToken(browser, expected, received) {
      this.calls.push({ browser, expected, received });
      return valid;
    },
  };
}

function makeResponse({ status = 200, payload = { ok: true }, token = TOKEN } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => (name === "X-ProxyBridge-Instance" ? token : null) },
    async json() { return payload; },
  };
}

test("uploads the original File body with encoded metadata and optional SHA-256", async () => {
  const file = {
    name: "sing-box-1.13.15-linux-amd64-glibc.tar.gz",
    size: 1024,
  };
  const digest = "AB".repeat(32);
  const auth = makeAuth();
  const browser = { location: {} };
  const calls = [];
  const result = await coreUpload.uploadKernel({
    fetchImpl: async (...args) => {
      calls.push(args);
      return makeResponse({ payload: { ok: true, sha256: digest.toLowerCase() } });
    },
    auth,
    browser,
    token: TOKEN,
    kind: "sing-box",
    file,
    sha256: ` ${digest} `,
  });

  assert.equal(result.sha256, digest.toLowerCase());
  assert.equal(calls.length, 1);
  const [path, options] = calls[0];
  assert.equal(path, "/api/cores/upload");
  assert.equal(options.method, "POST");
  assert.equal(options.body, file);
  assert.equal(options.headers["Content-Type"], "application/octet-stream");
  assert.equal(options.headers["X-ProxyBridge-Upload-Kind"], "sing-box");
  assert.equal(
    decodeURIComponent(options.headers["X-ProxyBridge-Upload-Name"]),
    file.name,
  );
  assert.equal(options.headers["X-ProxyBridge-Upload-Sha256"], digest.toLowerCase());
  assert.equal(options.headers["X-ProxyBridge-Instance"], TOKEN);
  assert.equal(options.headers["Content-Length"], undefined);
  assert.deepEqual(auth.calls, [{ browser, expected: TOKEN, received: TOKEN }]);
});

test("clears the caller through auth validation and reports a stable auth error code", async () => {
  const auth = makeAuth({ valid: false });
  await assert.rejects(
    coreUpload.uploadKernel({
      fetchImpl: async () => makeResponse({ token: "other-instance" }),
      auth,
      browser: {},
      token: TOKEN,
      kind: "xray",
      file: { name: "Xray-linux-64.zip", size: 5 },
    }),
    (error) => error.code === "AUTH_INVALID" && /凭证已失效/.test(error.message),
  );
});

test("validates SHA-256, file size, filename and package type before fetch", async () => {
  const invalidCases = [
    { kind: "sing-box", file: { name: "sing-box.zip", size: 0 }, sha256: "" },
    { kind: "sing-box", file: { name: "folder/sing-box.zip", size: 1 }, sha256: "" },
    { kind: "sing-box", file: { name: "sing-box.exe", size: 1 }, sha256: "" },
    { kind: "xray", file: { name: "Xray-linux-64.zip", size: 1 }, sha256: "abc" },
    {
      kind: "fingerprint-chromium",
      file: { name: "fingerprint-chromium_linux_x64.7z", size: 1 },
      sha256: "",
    },
    {
      kind: "xray",
      file: { name: "Xray-linux-64.zip", size: coreUpload.maxUploadBytes + 1 },
      sha256: "",
    },
  ];
  let fetchCount = 0;
  for (const item of invalidCases) {
    await assert.rejects(coreUpload.uploadKernel({
      fetchImpl: async () => { fetchCount += 1; return makeResponse(); },
      auth: makeAuth(),
      browser: {},
      token: TOKEN,
      ...item,
    }));
  }
  assert.equal(fetchCount, 0);
});

test("propagates JSON API errors and falls back to the HTTP status", async () => {
  const base = {
    auth: makeAuth(),
    browser: {},
    token: TOKEN,
    kind: "xray",
    file: { name: "Xray-linux-64.zip", size: 5 },
  };
  await assert.rejects(
    coreUpload.uploadKernel({
      ...base,
      fetchImpl: async () => makeResponse({ status: 400, payload: { error: "请先停止 Xray" } }),
    }),
    /请先停止 Xray/,
  );
  await assert.rejects(
    coreUpload.uploadKernel({
      ...base,
      fetchImpl: async () => ({
        ...makeResponse({ status: 500 }),
        json: async () => { throw new Error("not json"); },
      }),
    }),
    /HTTP 500/,
  );
});

test("platform hints explain Docker worker architecture and wrapper requirement", () => {
  assert.match(
    coreUpload.platformHint(
      "fingerprint-chromium",
      { os: "linux", arch: "arm64" },
      true,
    ),
    /linux\/x86_64.*chrome-wrapper/,
  );
  assert.match(
    coreUpload.platformHint("sing-box", { os: "linux", arch: "arm64" }),
    /linux\/arm64.*官方文件名/,
  );
});
