// 原生模块入口按依赖顺序加载，共享模块继续兼容 Node 测试，无需构建步骤。
import "./auth.js";
import "./diagnostics.js";
import "./browser-ui.js";
import "./core-upload.js";
import "./ui-core.js";
import "./i18n.js";
import "./pages/overview.js";
import "./pages/kernels.js";
import "./pages/browsers.js";
import "./pages/nodes.js";
import "./app.js";
