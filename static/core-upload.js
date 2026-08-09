(function (root, factory) {
  const coreUpload = factory();
  if (typeof module === "object" && module.exports) module.exports = coreUpload;
  root.ProxyBridgeCoreUpload = coreUpload;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // 上传协议使用原始二进制正文，浏览器会根据 File/Blob 自动填写 Content-Length；
  // 不使用 multipart、Base64 或 JSON，避免 2 GiB 安装包在页面内产生额外内存副本。
  const uploadPath = "/api/cores/upload";
  const maxUploadBytes = 2 * 1024 * 1024 * 1024;
  const kindHeader = "X-ProxyBridge-Upload-Kind";
  const nameHeader = "X-ProxyBridge-Upload-Name";
  const sha256Header = "X-ProxyBridge-Upload-Sha256";
  const kinds = Object.freeze(["sing-box", "xray", "fingerprint-chromium"]);

  function errorWithCode(message, code = "") {
    const error = new Error(message);
    if (code) error.code = code;
    return error;
  }

  function validateKind(value) {
    const kind = String(value || "").trim().toLowerCase();
    if (!kinds.includes(kind)) {
      throw new Error("导入目标只能是 sing-box、xray 或 fingerprint-chromium");
    }
    return kind;
  }

  function normalizeSha256(value) {
    const digest = String(value || "").trim().toLowerCase();
    if (digest && !/^[0-9a-f]{64}$/.test(digest)) {
      throw new Error("SHA-256 必须是 64 位十六进制字符串");
    }
    return digest;
  }

  function validatePackageFile(kindValue, file) {
    const kind = validateKind(kindValue);
    const name = String(file?.name || "");
    const size = Number(file?.size);
    if (
      !name
      || name.length > 180
      || name === "."
      || name === ".."
      || /[\\/\u0000-\u001f\u007f]/.test(name)
    ) {
      throw new Error("请选择文件名完整且未改名的官方安装包");
    }
    if (!Number.isSafeInteger(size) || size < 1) {
      throw new Error("不能上传空文件");
    }
    if (size > maxUploadBytes) {
      throw new Error("上传文件不能超过 2 GiB");
    }

    const lowered = name.toLowerCase();
    const supported = kind === "sing-box"
      ? lowered.startsWith("sing-box") && [".tar.gz", ".tgz", ".zip"].some((suffix) => lowered.endsWith(suffix))
      : kind === "xray"
        ? lowered.startsWith("xray") && lowered.endsWith(".zip")
        : [".zip", ".tar.gz", ".tgz", ".tar.xz", ".tar", ".dmg"].some((suffix) => lowered.endsWith(suffix));
    if (!supported) {
      const format = kind === "sing-box"
        ? "sing-box 官方 .tar.gz/.tgz/.zip 包"
        : kind === "xray"
          ? "Xray 官方 .zip 包"
          : "fingerprint-chromium 的 .zip/.tar/.dmg 包（不支持 .7z）";
      throw new Error(`请选择${format}，并保留官方文件名`);
    }
    return { kind, name, size };
  }

  function platformHint(kindValue, platform = {}, remoteBrowser = false) {
    const kind = validateKind(kindValue);
    const os = String(platform.os || "当前系统");
    const hostArch = String(platform.arch || "当前架构");
    const arch = kind === "fingerprint-chromium" && remoteBrowser ? "x86_64" : hostArch;
    const requirements = {
      "sing-box": "官方 .tar.gz/.tgz/.zip 发布包",
      xray: "官方 .zip 发布包",
      "fingerprint-chromium": "完整 .zip/.tar/.dmg 发布包",
    };
    const remoteNote = kind === "fingerprint-chromium" && remoteBrowser
      ? "；Docker Worker 包内必须有可执行 chrome-wrapper"
      : "";
    return `目标 ${os}/${arch} · ${requirements[kind]} · 必须保留官方文件名${remoteNote}`;
  }

  async function uploadKernel({
    fetchImpl,
    auth,
    browser,
    token = "",
    kind,
    file,
    sha256 = "",
    signal,
  }) {
    if (typeof fetchImpl !== "function") throw new Error("当前浏览器不支持文件上传");
    if (!auth || typeof auth.validateResponseToken !== "function" || !auth.headerName) {
      throw new Error("管理 API 认证模块不可用");
    }
    const selected = validatePackageFile(kind, file);
    const expectedSha256 = normalizeSha256(sha256);
    const headers = {
      "Content-Type": "application/octet-stream",
      [kindHeader]: selected.kind,
      [nameHeader]: encodeURIComponent(selected.name),
    };
    if (token) headers[auth.headerName] = token;
    if (expectedSha256) headers[sha256Header] = expectedSha256;

    const response = await fetchImpl(uploadPath, {
      method: "POST",
      headers,
      body: file,
      credentials: "same-origin",
      cache: "no-store",
      signal,
    });
    const responseToken = response.headers?.get(auth.headerName) || "";
    if (!auth.validateResponseToken(browser, token, responseToken)) {
      throw errorWithCode("管理 API 凭证已失效，请从启动入口重新打开控制台", "AUTH_INVALID");
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.error) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    return payload;
  }

  return Object.freeze({
    uploadPath,
    maxUploadBytes,
    kindHeader,
    nameHeader,
    sha256Header,
    kinds,
    normalizeSha256,
    validatePackageFile,
    platformHint,
    uploadKernel,
  });
});
