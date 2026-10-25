// Run with Node >= 20: node --test tests/d/frontend.test.mjs. No npm packages/build.
// Only browser storage/fetch are substituted; the actual auth and session code runs.
import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { api, getCredential, setSession, clearSession } from "../../frontend/shared/auth.js";
import { login, logout, currentIdentity, pendingLogin } from "../../frontend/shared/d-session.js";

const credentialKey = "campusflow.credential";
const loginKey = "campusflow.login.pending";
const logoutKey = "campusflow.logout.pending";
const identity = { userId: "u-test", displayName: "测试同学", departmentId: null,
  accountStatus: "ACTIVE", roleIds: ["r-student"], grants: [
    { permissionCode: "m1.reserve", scope: { type: "SPACE", id: "sp-test" } },
  ] };
const loginData = { credential: "opaque-test-token", expiresAt: "2026-01-01T08:00:00Z", identity };
const nativeFetch = globalThis.fetch;
const nativeTimer = globalThis.setTimeout;
let calls;
let values;
function success(data) { return { status: 200, json: async () => ({ ok: true, data, error: null }) }; }
function failure(code, status) {
  return { status, json: async () => ({ ok: false, data: null, error: { code, message: "服务明确拒绝" } }) };
}
function respond(response) {
  globalThis.fetch = async (url, options) => { calls.push({ url, ...options }); return response; };
}
function networkFailure() {
  globalThis.fetch = async (url, options) => { calls.push({ url, ...options }); throw new Error("network"); };
}
function unavailable(err) { return err.code === "UNAVAILABLE" && err.httpStatus === null; }

beforeEach(() => {
  calls = [];
  values = new Map();
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  } });
  globalThis.fetch = async () => { assert.fail("Unexpected network request"); };
});
afterEach(() => { globalThis.fetch = nativeFetch; globalThis.setTimeout = nativeTimer; });

test("storage uses only sessionStorage credential; clearing preserves unrelated data", () => {
  assert.equal(getCredential(), null);
  values.set("unrelated", "keep");
  setSession({ ...loginData, password: "never-store-this" });
  assert.equal(getCredential(), loginData.credential);
  assert.deepEqual([...values], [["unrelated", "keep"], [credentialKey, loginData.credential]]);
  clearSession();
  assert.deepEqual([...values], [["unrelated", "keep"]]);
});
for (const bad of [null, {}, { ...loginData, credential: "bad\nheader" },
  { ...loginData, expiresAt: "2026-01-01" }, { ...loginData, identity: {} },
  { ...loginData, identity: { ...identity, roleIds: null } },
  { ...loginData, identity: { ...identity, grants: [{ permissionCode: "x", scope: { type: "GLOBAL", id: "x" } }] } },
  { ...loginData, identity: { ...identity, accountStatus: "DISABLED" } }]) {
  test(`setSession rejects incomplete data ${JSON.stringify(bad)}`, () => {
    assert.throws(() => setSession(bad), unavailable);
    assert.equal(values.size, 0);
  });
}
test("browser storage refusal is explicit and sends no request", async () => {
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, get() { throw new Error("blocked"); } });
  assert.throws(getCredential, unavailable);
  assert.throws(() => setSession(loginData), unavailable);
  await assert.rejects(api("d", "get_identity", {}), unavailable);
});
test("api sends direct JSON to a lowercase same-origin path with only user bearer", async () => {
  setSession(loginData);
  respond(success({ value: 3 }));
  assert.deepEqual(await api("M2", "get_applications", { limit: 5 }), { value: 3 });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/v1/m2/get_applications");
  assert.equal(calls[0].body, '{"limit":5}');
  assert.equal(calls[0].method, "POST");
  assert.deepEqual(calls[0].headers, { "Content-Type": "application/json", Accept: "application/json", Authorization: "Bearer opaque-test-token" });
  assert.equal(calls[0].redirect, "error");
  assert.equal(calls[0].credentials, "omit");
  assert.equal(calls[0].cache, "no-store");
});
test("login never attaches an existing credential", async () => {
  setSession(loginData);
  respond(success(loginData));
  await api("D", "login", { account: "a", password: "p", requestId: "test" });
  assert.equal(calls[0].headers.Authorization, undefined);
});
for (const [module, fn, input] of [["https://bad", "login", {}], ["d", "../login", {}],
  ["d", "login?secret", {}], ["m4", "query", {}], ["d", "login", []], ["d", "login", null]]) {
  test(`invalid request target ${module}/${fn}/${JSON.stringify(input)}`, async () => {
    await assert.rejects(api(module, fn, input), { code: "INVALID_ARGUMENT", httpStatus: null });
  });
}
test("unserializable input fails before sending", async () => {
  const input = {}; input.self = input;
  await assert.rejects(api("d", "login", input), { code: "INVALID_ARGUMENT" });
});
for (const [code, status] of [["AUTH_FAILED", 401], ["UNAUTHENTICATED", 401], ["FORBIDDEN", 403],
  ["NOT_FOUND", 404], ["CONFLICT", 409], ["REQUEST_ID_REUSED", 409],
  ["INVALID_ARGUMENT", 422], ["UNAVAILABLE", 503], ["INTERNAL_ERROR", 500]]) {
  test(`valid service error ${code} retains its status and message`, async () => {
    setSession(loginData);
    respond(failure(code, status));
    await assert.rejects(api("d", "get_identity", {}), { code, httpStatus: status, message: "服务明确拒绝" });
    assert.equal(getCredential(), status === 401 ? null : loginData.credential);
    assert.equal(calls.length, 1);
  });
}
for (const [label, response] of [
  ["HTML response", { status: 502, json: async () => { throw new Error("HTML"); } }],
  ["empty success", success(null)], ["wrong status", { ...success({}), status: 201 }],
  ["wrong error status", failure("FORBIDDEN", 200)],
  ["unknown error", failure("SECRET", 500)],
  ["missing envelope", { status: 200, json: async () => ({ data: {} }) }],
]) {
  test(`malformed response: ${label}`, async () => {
    respond(response);
    await assert.rejects(api("d", "get_identity", {}), unavailable);
    assert.equal(calls.length, 1);
  });
}
test("401 clears credential even if the response body is malformed", async () => {
  setSession(loginData);
  respond({ status: 401, json: async () => { throw new Error("bad json"); } });
  await assert.rejects(api("d", "get_identity", {}), unavailable);
  assert.equal(getCredential(), null);
});
test("late 401 for an old request cannot clear a newer session", async () => {
  setSession(loginData);
  globalThis.fetch = async () => { setSession({ ...loginData, credential: "new-token" }); return failure("UNAUTHENTICATED", 401); };
  await assert.rejects(api("d", "get_identity", {}), { httpStatus: 401 });
  assert.equal(getCredential(), "new-token");
});
test("network failure never retries automatically", async () => {
  networkFailure();
  await assert.rejects(api("d", "logout", { requestId: "r" }), unavailable);
  assert.equal(calls.length, 1);
});
test("timeout aborts once and returns unknown result", async () => {
  globalThis.setTimeout = (callback) => nativeTimer(callback, 1);
  globalThis.fetch = async (url, options) => {
    calls.push({ url, ...options });
    return new Promise((resolve, reject) => options.signal.addEventListener("abort", () => reject(new Error("aborted"))));
  };
  await assert.rejects(api("d", "logout", { requestId: "r" }), unavailable);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].signal.aborted, true);
});
test("login persists only account and ID before sending; unknown results reuse both after reload", async () => {
  networkFailure();
  await assert.rejects(login("student1", "private-password"), unavailable);
  const first = JSON.parse(calls[0].body);
  assert.deepEqual(pendingLogin(), { account: "student1", requestId: first.requestId });
  assert.ok(!JSON.stringify([...values]).includes("private-password"));
  const reloaded = await import(`../../frontend/shared/d-session.js?reload=${crypto.randomUUID()}`);
  respond(success(loginData));
  await reloaded.login("student1", "private-password");
  assert.deepEqual(JSON.parse(calls[1].body), first);
  assert.equal(pendingLogin(), null);
  assert.equal(getCredential(), loginData.credential);
});
test("unknown login does not silently switch account or issue another request", async () => {
  networkFailure();
  await assert.rejects(login("student1", "p"), unavailable);
  await assert.rejects(login("student2", "p"), unavailable);
  assert.equal(calls.length, 1);
});
test("definite bad-password response permits a new user-triggered attempt", async () => {
  respond(failure("AUTH_FAILED", 401));
  await assert.rejects(login("student1", "bad"), { code: "AUTH_FAILED" });
  assert.equal(pendingLogin(), null);
  respond(success(loginData));
  await login("student1", "correct");
  assert.notEqual(JSON.parse(calls[0].body).requestId, JSON.parse(calls[1].body).requestId);
});
for (const [code, status] of [["UNAVAILABLE", 503], ["INTERNAL_ERROR", 500], ["REQUEST_ID_REUSED", 409]]) {
  test(`login preserves original request on ${code}`, async () => {
    respond(failure(code, status));
    await assert.rejects(login("student1", "p"), { code });
    assert.equal(pendingLogin().requestId, JSON.parse(calls[0].body).requestId);
  });
}
test("invalid successful identity cannot establish a session and keeps the login ID", async () => {
  respond(success({ ...loginData, identity: { ...identity, grants: null } }));
  await assert.rejects(login("student1", "p"), unavailable);
  assert.equal(getCredential(), null);
  assert.ok(pendingLogin());
});
test("failed pending-write stops login before sending", async () => {
  sessionStorage.setItem = () => { throw new Error("quota"); };
  await assert.rejects(login("student1", "p"), unavailable);
});
test("parallel login submission cannot send twice", async () => {
  let release;
  globalThis.fetch = async (url, options) => {
    calls.push({ url, ...options });
    return new Promise((resolve) => { release = () => resolve(success(loginData)); });
  };
  const first = login("student1", "p");
  await assert.rejects(login("student1", "p"), unavailable);
  release(); await first;
  assert.equal(calls.length, 1);
});
test("identity is read afresh and malformed grants are rejected", async () => {
  setSession(loginData);
  respond(success(identity));
  assert.deepEqual(await currentIdentity(), identity);
  const changed = { ...identity, roleIds: [], grants: [] };
  respond(success(changed));
  assert.deepEqual(await currentIdentity(), changed);
  respond(success({ ...identity, grants: [{}] }));
  await assert.rejects(currentIdentity(), unavailable);
  assert.equal(values.size, 1);
});
test("logout unknown result preserves credential and original ID through reload", async () => {
  setSession(loginData); networkFailure();
  await assert.rejects(logout(), unavailable);
  assert.equal(getCredential(), loginData.credential);
  const first = JSON.parse(calls[0].body);
  assert.deepEqual(JSON.parse(values.get(logoutKey)), first);
  const reloaded = await import(`../../frontend/shared/d-session.js?logout=${crypto.randomUUID()}`);
  respond(success({ loggedOut: true }));
  assert.equal(await reloaded.logout(), true);
  assert.deepEqual(JSON.parse(calls[1].body), first);
  assert.equal(getCredential(), null);
  assert.equal(values.has(logoutKey), false);
});
test("logout false is not treated as confirmation", async () => {
  setSession(loginData); respond(success({ loggedOut: false }));
  await assert.rejects(logout(), unavailable);
  assert.equal(getCredential(), loginData.credential);
  assert.ok(values.get(logoutKey));
});
test("logout without a credential sends nothing", async () => { assert.equal(await logout(), false); });
test("a fresh login clears stale logout state", async () => {
  values.set(logoutKey, JSON.stringify({ requestId: "logout-old" }));
  respond(success(loginData));
  await login("student1", "p");
  assert.equal(values.has(logoutKey), false);
});
test("malformed persisted login record is explicit instead of generating another request", async () => {
  values.set(loginKey, "invalid json");
  await assert.rejects(login("student1", "p"), unavailable);
});
