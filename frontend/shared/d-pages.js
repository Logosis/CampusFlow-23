import { getCredential } from "./auth.js";
import { login, logout, currentIdentity, pendingLogin } from "./d-session.js";

const $ = (id) => document.getElementById(id);
const isLoginPage = document.body.dataset.page === "login";
let busy = false;

function message(text, kind = "error") {
  $("message").textContent = text;
  $("message").dataset.kind = kind;
  $("message").hidden = !text;
}

function showError(err) {
  message(err.code === "REQUEST_ID_REUSED"
    ? "原登录请求使用了不同密码。请重新输入上次提交的密码，确认原请求结果。"
    : (err.message || "请求失败，请稍后重试。"));
}

function loginFields() {
  const attempt = pendingLogin();
  if (attempt) $("account").value = attempt.account;
  $("account").readOnly = Boolean(attempt);
  $("request-note").hidden = !attempt;
  $("request-note").textContent = attempt
    ? `登录结果待确认，请输入原密码重试。请求编号：${attempt.requestId}` : "";
  $("submit").textContent = attempt ? "重试原登录" : "登录";
}

function setBusy(value) {
  busy = value;
  document.body.setAttribute("aria-busy", String(value));
  for (const item of document.querySelectorAll("button, input")) item.disabled = value;
}

async function submitLogin(event) {
  event.preventDefault();
  if (busy) return;
  setBusy(true);
  message("正在验证账号…", "info");
  try {
    await login($("account").value, $("password").value);
    $("password").value = "";
    location.replace("/");
  } catch (err) {
    $("password").value = "";
    showError(err);
    try { loginFields(); } catch (storageError) { showError(storageError); }
  } finally {
    setBusy(false);
    $("password").focus();
  }
}

async function refreshIdentity() {
  if (busy) return;
  setBusy(true);
  message("正在读取当前身份…", "info");
  if (!isLoginPage) $("workspace").hidden = true;
  try {
    if (!getCredential()) {
      if (!isLoginPage) location.replace("/login.html");
      else { $("login-form").hidden = false; loginFields(); message(""); }
      return;
    }
    const identity = await currentIdentity();
    if (isLoginPage) { location.replace("/"); return; }
    $("display-name").textContent = identity.displayName || identity.userId;
    $("user-id").textContent = identity.userId;
    $("department").textContent = identity.departmentId || "未指定院系";
    $("roles").textContent = identity.roleIds.length ? identity.roleIds.join("、") : "暂未分配角色";
    $("workspace").hidden = false;
    message("");
  } catch (err) {
    if (err.httpStatus === 401) {
      if (isLoginPage) {
        $("login-form").hidden = false;
        loginFields();
        message("原会话已失效，请重新登录。", "info");
      } else location.replace("/login.html");
    } else {
      showError(err);
      $("retry").hidden = false;
    }
  } finally { setBusy(false); }
}

async function submitLogout() {
  if (busy) return;
  setBusy(true);
  message("正在确认退出…", "info");
  try {
    await logout();
    location.replace("/login.html");
  } catch (err) {
    if (err.httpStatus === 401) location.replace("/login.html");
    else {
      showError(err);
      $("logout").textContent = "重试退出";
      // Keep the credential until the service confirms revocation.
    }
  } finally { setBusy(false); }
}

$("retry").addEventListener("click", () => {
  $("retry").hidden = true;
  refreshIdentity();
});
if (isLoginPage) $("login-form").addEventListener("submit", submitLogin);
else $("logout").addEventListener("click", submitLogout);
// Revalidate when returning from another module or a back/forward cache entry.
window.addEventListener("pageshow", (event) => { if (event.persisted) refreshIdentity(); });
window.addEventListener("focus", () => { if (!isLoginPage) refreshIdentity(); });
document.addEventListener("visibilitychange", () => {
  if (!isLoginPage && document.visibilityState === "visible") refreshIdentity();
});
refreshIdentity();
