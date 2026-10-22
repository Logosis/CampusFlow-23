// Shared browser contract from docs/开发指导.md. No module credentials belong here.
const CREDENTIAL_KEY = "campusflow.credential";
const STATUS = {
  AUTH_FAILED: 401, UNAUTHENTICATED: 401, FORBIDDEN: 403, NOT_FOUND: 404,
  CONFLICT: 409, REQUEST_ID_REUSED: 409, INVALID_ARGUMENT: 422,
  UNAVAILABLE: 503, INTERNAL_ERROR: 500,
};

function error(code, message, httpStatus = null) {
  return Object.assign(new Error(message), { code, httpStatus });
}

function storage(action) {
  try { return action(globalThis.sessionStorage); }
  catch { throw error("UNAVAILABLE", "无法访问当前标签页的会话存储，请检查浏览器设置。"); }
}

export function getCredential() {
  return storage((store) => store.getItem(CREDENTIAL_KEY)) || null;
}

export function clearSession() {
  storage((store) => store.removeItem(CREDENTIAL_KEY));
}

export function setSession(loginData) {
  const identity = loginData?.identity;
  const id = (value) => typeof value === "string" && Boolean(value.trim());
  if (!loginData || typeof loginData.credential !== "string" ||
      !/^[\x21-\x7e]+$/.test(loginData.credential) ||
      typeof loginData.expiresAt !== "string" || !Number.isFinite(Date.parse(loginData.expiresAt)) ||
      !/(?:Z|[+-]\d{2}:\d{2})$/.test(loginData.expiresAt) ||
      !identity || identity.accountStatus !== "ACTIVE" || !id(identity.userId) ||
      typeof identity.displayName !== "string" ||
      !(identity.departmentId === null || id(identity.departmentId)) ||
      !Array.isArray(identity.roleIds) || !identity.roleIds.every(id) ||
      !Array.isArray(identity.grants) || !identity.grants.every((grant) =>
        id(grant?.permissionCode) && grant.scope &&
        ((grant.scope.type === "GLOBAL" && grant.scope.id === null) ||
         (["DEPARTMENT", "SPACE"].includes(grant.scope.type) && id(grant.scope.id))))) {
    throw error("UNAVAILABLE", "登录响应不完整，请保留原登录请求重试。");
  }
  // Never persist the password, full identity, or client-side authorization decisions.
  storage((store) => store.setItem(CREDENTIAL_KEY, loginData.credential));
}

export async function api(module, functionName, input) {
  if (typeof module !== "string" || !["d", "m1", "m2", "m3"].includes(module.toLowerCase()) ||
      typeof functionName !== "string" || !/^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/.test(functionName) ||
      !input || typeof input !== "object" || Array.isArray(input)) {
    throw error("INVALID_ARGUMENT", "模块、函数名或请求体不符合接口约定。");
  }
  const name = module.toLowerCase();
  const credential = getCredential();
  const headers = { "Content-Type": "application/json", Accept: "application/json" };
  if (!(name === "d" && functionName === "login") && credential) {
    headers.Authorization = `Bearer ${credential}`;
  }
  let body;
  try { body = JSON.stringify(input); }
  catch { throw error("INVALID_ARGUMENT", "请求内容无法编码为 JSON。"); }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  try {
    let response;
    let result;
    try {
      response = await globalThis.fetch(`/api/v1/${name}/${functionName}`, {
        method: "POST", headers, body, signal: controller.signal,
        credentials: "omit", redirect: "error", cache: "no-store",
      });
    } catch {
      throw error("UNAVAILABLE", "连接中断或超时；操作结果尚未确认，请保留原请求编号。");
    }
    // A late response must not discard a newer login in this same tab.
    if (response.status === 401 && getCredential() === credential) clearSession();
    try { result = await response.json(); }
    catch { throw error("UNAVAILABLE", "服务响应无法识别，操作结果尚未确认。"); }
    if (response.status === 200 && result?.ok === true && result.error === null &&
        result.data !== null && typeof result.data === "object") return result.data;
    if (result?.ok === false && result.data === null && result.error &&
        Object.hasOwn(STATUS, result.error.code) && STATUS[result.error.code] === response.status &&
        typeof result.error.message === "string" && result.error.message.trim()) {
      throw error(result.error.code, result.error.message, response.status);
    }
    throw error("UNAVAILABLE", "服务响应不符合约定，操作结果尚未确认。");
  } finally {
    clearTimeout(timer);
  }
}
