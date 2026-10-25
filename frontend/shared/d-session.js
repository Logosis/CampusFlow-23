import { api, getCredential, setSession, clearSession } from "./auth.js";

const LOGIN_KEY = "campusflow.login.pending";
const LOGOUT_KEY = "campusflow.logout.pending";
const unavailable = (message) => Object.assign(new Error(message), { code: "UNAVAILABLE", httpStatus: null });

function read(key) {
  try {
    const raw = sessionStorage.getItem(key);
    return raw === null ? null : JSON.parse(raw);
  } catch { throw unavailable("无法读取待确认请求，请检查浏览器存储设置。"); }
}

function write(key, value) {
  try {
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, JSON.stringify(value));
  } catch { throw unavailable("无法保存请求状态，请保留当前页面和原请求编号。"); }
}

function newId(prefix) {
  try { return `${prefix}-${globalThis.crypto.randomUUID()}`; }
  catch { throw unavailable("无法生成安全请求编号，请通过 localhost 或 HTTPS 打开页面。"); }
}

export function validateIdentity(value) {
  const id = (v) => typeof v === "string" && Boolean(v.trim());
  if (!value || !id(value.userId) || typeof value.displayName !== "string" ||
      !(value.departmentId === null || id(value.departmentId)) || value.accountStatus !== "ACTIVE" ||
      !Array.isArray(value.roleIds) || !value.roleIds.every(id) || !Array.isArray(value.grants) ||
      !value.grants.every((g) => id(g?.permissionCode) && g.scope &&
        ((g.scope.type === "GLOBAL" && g.scope.id === null) ||
         (["DEPARTMENT", "SPACE"].includes(g.scope.type) && id(g.scope.id))))) {
    throw unavailable("当前身份响应不完整，请重试读取身份。");
  }
  return value;
}

export function pendingLogin() {
  const attempt = read(LOGIN_KEY);
  if (attempt && (typeof attempt.requestId !== "string" || !attempt.requestId.startsWith("login-") ||
      typeof attempt.account !== "string" || !attempt.account.trim())) {
    throw unavailable("原登录请求记录无法识别，请保留当前标签页并联系管理员。");
  }
  return attempt;
}

let loggingIn = false;
export async function login(account, password) {
  if (loggingIn) throw unavailable("登录请求正在处理中。");
  if (typeof account !== "string" || !account.trim() || typeof password !== "string" || !password) {
    throw Object.assign(new Error("请输入账号和密码。"), { code: "INVALID_ARGUMENT", httpStatus: 422 });
  }
  loggingIn = true;
  try {
    const existing = pendingLogin();
    if (existing && existing.account !== account) throw unavailable("请先用原账号确认上一次登录结果。");
    const attempt = existing || { account, requestId: newId("login") };
    write(LOGIN_KEY, attempt); // Only account and requestId, never a password or digest.
    let data;
    try { data = await api("d", "login", { ...attempt, password }); }
    catch (err) {
      // Unknown results retain the original ID, including across page reloads.
      if (err.httpStatus !== null && err.httpStatus < 500 && err.code !== "REQUEST_ID_REUSED") {
        write(LOGIN_KEY, null);
      }
      throw err;
    }
    validateIdentity(data.identity);
    setSession(data);
    write(LOGIN_KEY, null);
    write(LOGOUT_KEY, null);
    return data.identity;
  } finally { loggingIn = false; }
}

export async function currentIdentity() {
  return validateIdentity(await api("d", "get_identity", {}));
}

let loggingOut = false;
export async function logout() {
  if (loggingOut) throw unavailable("退出请求正在处理中。");
  loggingOut = true;
  try {
    const credential = getCredential();
    if (!credential) return false;
    let attempt = read(LOGOUT_KEY);
    if (attempt && (typeof attempt.requestId !== "string" || !attempt.requestId.startsWith("logout-"))) {
      throw unavailable("原退出请求记录无法识别，请保留当前标签页并联系管理员。");
    }
    attempt ||= { requestId: newId("logout") };
    write(LOGOUT_KEY, attempt);
    const data = await api("d", "logout", attempt);
    if (data?.loggedOut !== true) throw unavailable("退出尚未确认，请使用原请求重试。");
    if (getCredential() === credential) {
      clearSession();
      write(LOGOUT_KEY, null);
    }
    return true;
  } finally { loggingOut = false; }
}
