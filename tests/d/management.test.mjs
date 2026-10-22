import { test } from "node:test";
import assert from "node:assert/strict";
import { notificationInput, auditGetInput, auditListInput, beijingTime, canRead,
  validateNotification, validateAudit, validateAuditPage, QueryReader } from "../../frontend/shared/d-query.js";

const event = { auditId: "aud-1", requestId: "req-1", sourceModule: "M1", actorType: "USER", actorId: "u-1",
  objectType: "BOOKING", businessId: "booking-1", spaceId: null, action: "CREATE", occurredAt: "2026-10-21T10:20:30+08:00",
  result: "SUCCESS", summary: "预约成功", verifiedBy: "D" };
const notice = { notificationId: "ntf-1", requestId: "req-1", sourceModule: "M1", businessId: "booking-1", spaceId: null,
  kind: "M1_BEFORE_START", channel: "EMAIL", title: "预约提醒", body: "请按时签到", createdAt: "2026-10-21T10:20:30+08:00",
  deliveries: [{ recipientUserId: "u-1", status: "PENDING", attemptCount: 0, lastAttemptAt: null, reason: "" }] };
const failInput = (fn) => assert.throws(fn, { code: "INVALID_ARGUMENT", httpStatus: null });
const failOutput = (fn) => assert.throws(fn, { code: "UNAVAILABLE", httpStatus: null });

test("notification input selects exactly one identifier, never a source or write operation", () => {
  assert.deepEqual(notificationInput("notificationId", " ntf-1 "), { notificationId: "ntf-1" });
  assert.deepEqual(notificationInput("requestId", "req-1"), { requestId: "req-1" });
  for (const [kind, value] of [["sourceModule", "M1"], ["notificationId", " "], ["requestId", null]]) failInput(() => notificationInput(kind, value));
});
test("audit GET supports optional source and exactly one ID", () => {
  assert.deepEqual(auditGetInput("auditId", "aud-1", ""), { operation: "GET", auditId: "aud-1" });
  assert.deepEqual(auditGetInput("requestId", "req-1", "D"), { operation: "GET", requestId: "req-1", sourceModule: "D" });
  failInput(() => auditGetInput("requestId", "x", "M4"));
  failInput(() => auditGetInput("auditId", "", ""));
});
test("audit LIST omits blank/null filters and unrecognized keys", () => {
  assert.deepEqual(auditListInput({ sourceModule: "", businessId: null, objectType: " ", action: "", cursor: "stale", operation: "APPEND" }), { operation: "LIST", limit: 20 });
  assert.deepEqual(auditListInput({ sourceModule: "M1", businessId: " b1 ", objectType: "BOOKING", action: "CANCEL", actorUserId: "u1", spaceId: "s1", limit: "100", timeFrom: "2026-10-21T00:00", timeTo: "2026-10-22T00:00" }),
    { operation: "LIST", sourceModule: "M1", businessId: "b1", objectType: "BOOKING", action: "CANCEL", actorUserId: "u1", spaceId: "s1", timeFrom: "2026-10-21T00:00:00+08:00", timeTo: "2026-10-22T00:00:00+08:00", limit: 100 });
});
for (const limit of ["0", "101", "1.5", "1e1", "", "x", "-1", "Infinity"]) test(`reject invalid page limit ${JSON.stringify(limit)}`, () => failInput(() => auditListInput({ limit })));
test("time is explicit Beijing time independent of browser timezone", () => {
  assert.equal(beijingTime("2026-10-22T12:05"), "2026-10-22T12:05:00+08:00");
  assert.equal(beijingTime("2024-02-29T23:59:59"), "2024-02-29T23:59:59+08:00");
  assert.equal(Date.parse(beijingTime("2026-10-22T12:05")), Date.parse("2026-10-22T04:05:00Z"));
});
for (const date of ["2026-02-29T10:00", "2026-04-31T12:00", "2026-13-01T12:00", "2026-01-01T24:00", "2026-01-01", "2026-01-01T12:00Z"]) {
  test(`reject invalid local date ${date}`, () => failInput(() => beijingTime(date)));
}
test("reversed/equal time window and invalid source rejected", () => {
  for (const to of ["2026-10-21T00:00", "2026-10-20T00:00"]) failInput(() => auditListInput({ timeFrom: "2026-10-21T00:00", timeTo: to }));
  failInput(() => auditListInput({ sourceModule: "M4" }));
});
test("visibility follows current ACTIVE identity's grants, never role names", () => {
  const identity = { accountStatus: "ACTIVE", roleIds: ["r-admin"], grants: [] };
  assert.equal(Boolean(canRead(identity, "d.audit.read")), false);
  identity.grants.push({ permissionCode: "d.audit.read", scope: { type: "SPACE", id: "s1" } });
  assert.equal(canRead(identity, "d.audit.read"), true);
  assert.equal(canRead(identity, "d.notify.read"), false);
  identity.accountStatus = "DISABLED";
  assert.equal(canRead(identity, "d.audit.read"), false);
  assert.equal(Boolean(canRead(null, "d.audit.read")), false);
});
test("accept valid notices with all five delivery states and null timestamps", () => {
  const v = structuredClone(notice);
  v.deliveries = ["PENDING", "SENDING", "SUBMITTED", "FAILED", "UNKNOWN"].map((status) => ({ ...notice.deliveries[0], status }));
  assert.equal(validateNotification(v), v);
});
for (const key of Object.keys(notice)) test(`reject incomplete notification missing ${key}`, () => {
  const v = structuredClone(notice); delete v[key]; failOutput(() => validateNotification(v));
});
test("reject false delivery success or malformed attempts/time", () => {
  for (const patch of [{ status: "SENT" }, { attemptCount: -1 }, { attemptCount: "1" }, { lastAttemptAt: "yesterday" }, { reason: null }]) {
    failOutput(() => validateNotification({ ...notice, deliveries: [{ ...notice.deliveries[0], ...patch }] }));
  }
  failOutput(() => validateNotification({ ...notice, deliveries: [] }));
});
test("accept audit record and empty final page", () => {
  assert.equal(validateAudit(event), event);
  assert.deepEqual(validateAuditPage({ items: [], nextCursor: null }), { items: [], nextCursor: null });
});
for (const key of Object.keys(event)) test(`reject incomplete audit event missing ${key}`, () => {
  const v = { ...event }; delete v[key]; failOutput(() => validateAudit(v));
});
test("invalid page envelope/record never masquerades as empty success", () => {
  for (const v of [null, {}, { items: [], nextCursor: "" }, { items: [], nextCursor: 1 }, { items: [null], nextCursor: null }]) failOutput(() => validateAuditPage(v));
});

test("pagination preserves exact original filters and limit", async () => {
  const calls = [];
  const reader = new QueryReader("audit", validateAuditPage, async (...args) => {
    calls.push(args); return { items: [event], nextCursor: calls.length === 1 ? "bound-cursor" : null };
  });
  const first = { operation: "LIST", sourceModule: "M1", limit: 1 };
  assert.equal((await reader.run(first)).page, 1);
  first.sourceModule = "M2";
  assert.equal((await reader.run({ sourceModule: "M3" }, true)).page, 2);
  assert.deepEqual(calls, [["D", "audit", { operation: "LIST", sourceModule: "M1", limit: 1 }],
    ["D", "audit", { operation: "LIST", sourceModule: "M1", limit: 1, cursor: "bound-cursor" }]]);
  assert.equal(await reader.run({}, true), null);
});
test("failed next page retries original cursor without advancing page", async () => {
  const requests = []; let fail = true;
  const reader = new QueryReader("audit", validateAuditPage, async (_m, _f, input) => {
    requests.push(input);
    if (input.cursor && fail) throw Object.assign(new Error("Service unavailable"), { code: "UNAVAILABLE", httpStatus: 503 });
    return { items: [event], nextCursor: input.cursor ? null : "next-1" };
  });
  await reader.run({ operation: "LIST", limit: 20 });
  assert.equal((await reader.run({}, true)).error.httpStatus, 503);
  assert.equal(reader.page, 1); assert.equal(reader.nextCursor, "next-1");
  fail = false;
  assert.equal((await reader.run({}, true)).page, 2);
  assert.deepEqual(requests[1], requests[2]);
});
test("reset removes cursor and ignores late success after identity/filter change", async () => {
  let resolve;
  const reader = new QueryReader("audit", validateAuditPage, () => new Promise((r) => { resolve = r; }));
  const pending = reader.run({ operation: "LIST", limit: 20 });
  assert.equal(reader.busy, true);
  reader.reset();
  resolve({ items: [event], nextCursor: "secret-old-cursor" });
  assert.equal(await pending, null);
  assert.equal(reader.nextCursor, null); assert.equal(reader.base, null); assert.equal(reader.busy, false);
});
test("late failure cannot overwrite another query or clear its busy state", async () => {
  const pending = [];
  const reader = new QueryReader("get_notification", validateNotification, () => new Promise((resolve, reject) => pending.push({ resolve, reject })));
  const first = reader.run({ notificationId: "first" });
  reader.reset();
  const second = reader.run({ notificationId: "second" });
  pending[0].reject(new Error("old failure"));
  assert.equal(await first, null); assert.equal(reader.busy, true);
  pending[1].resolve(notice);
  assert.equal((await second).data, notice); assert.equal(reader.busy, false);
});
test("double click runs one request, failures remain errors with no invented success", async () => {
  let reject; let count = 0;
  const reader = new QueryReader("audit", validateAudit, () => { count++; return new Promise((_resolve, fail) => { reject = fail; }); });
  const first = reader.run({ operation: "GET", auditId: "1" });
  assert.equal(await reader.run({ operation: "GET", auditId: "1" }), null);
  assert.equal(count, 1);
  const forbidden = Object.assign(new Error("Forbidden"), { code: "FORBIDDEN", httpStatus: 403 });
  reject(forbidden);
  assert.deepEqual(await first, { error: forbidden }); assert.equal(reader.page, 0);
});
test("invalid service response is rejected before pagination advances", async () => {
  const reader = new QueryReader("audit", validateAuditPage, async () => ({ items: [], nextCursor: 42 }));
  assert.equal((await reader.run({ operation: "LIST", limit: 20 })).error.code, "UNAVAILABLE");
  assert.equal(reader.page, 0); assert.equal(reader.nextCursor, null);
});

// Exercise the real public auth adapter as well as our reader; mock only browser storage/fetch.
async function withBrowserResponse(reply, action) {
  const oldFetch = globalThis.fetch;
  const oldStorage = Object.getOwnPropertyDescriptor(globalThis, "sessionStorage");
  const values = new Map([["campusflow.credential", "isolated-query-token"]]);
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  } });
  const calls = [];
  globalThis.fetch = async (url, options) => {
    const input = JSON.parse(options.body);
    calls.push({ url, input, authorization: options.headers.Authorization });
    return reply(input);
  };
  try { await action(calls, values); }
  finally {
    globalThis.fetch = oldFetch;
    if (oldStorage) Object.defineProperty(globalThis, "sessionStorage", oldStorage);
    else delete globalThis.sessionStorage;
  }
}
const serviceFailure = (code, status) => Response.json({ ok: false, data: null, error: { code, message: "测试服务返回" } }, { status });
for (const [code, status] of [["CONFLICT", 409], ["NOT_FOUND", 404], ["FORBIDDEN", 403], ["INVALID_ARGUMENT", 422], ["UNAVAILABLE", 503]]) {
  test(`real auth adapter preserves ${status}/${code} for management reads`, async () => {
    await withBrowserResponse(() => serviceFailure(code, status), async (calls) => {
      const reader = new QueryReader("get_notification", validateNotification);
      const result = await reader.run({ notificationId: "ntf-1" });
      assert.equal(result.error.code, code); assert.equal(result.error.httpStatus, status);
      assert.equal(result.data, undefined);
      assert.deepEqual(calls, [{ url: "/api/v1/d/get_notification", input: { notificationId: "ntf-1" }, authorization: "Bearer isolated-query-token" }]);
    });
  });
}
test("real auth 422 invalid cursor discards pagination and next click cannot reuse it", async () => {
  await withBrowserResponse((input) => input.cursor ? serviceFailure("INVALID_ARGUMENT", 422)
    : Response.json({ ok: true, data: { items: [event], nextCursor: "expired-cursor" }, error: null }), async (calls) => {
    const reader = new QueryReader("audit", validateAuditPage);
    await reader.run({ operation: "LIST", limit: 1 });
    const result = await reader.run({}, true);
    assert.equal(result.error.code, "INVALID_ARGUMENT"); assert.equal(result.error.httpStatus, 422);
    assert.equal(reader.nextCursor, null); assert.equal(reader.base, null);
    assert.equal(reader.page, 0); assert.equal(reader.busy, false);
    assert.equal(await reader.run({}, true), null); assert.equal(calls.length, 2);
    assert.equal((await reader.run({ operation: "LIST", limit: 1 })).page, 1);
    assert.equal(calls.at(-1).input.cursor, undefined);
  });
});
test("real auth 503 keeps the cursor and retries the exact next-page input", async () => {
  let fail = true;
  await withBrowserResponse((input) => input.cursor && fail ? serviceFailure("UNAVAILABLE", 503)
    : Response.json({ ok: true, data: { items: [event], nextCursor: input.cursor ? null : "next-cursor" }, error: null }), async (calls) => {
    const reader = new QueryReader("audit", validateAuditPage);
    await reader.run({ operation: "LIST", limit: 1, sourceModule: "M1" });
    assert.equal((await reader.run({}, true)).error.httpStatus, 503);
    assert.equal(reader.nextCursor, "next-cursor"); assert.equal(reader.page, 1);
    fail = false;
    assert.equal((await reader.run({}, true)).page, 2);
    assert.deepEqual(calls[1].input, calls[2].input);
  });
});
test("unknown service error code remains unavailable, never presented as accepted validation", async () => {
  await withBrowserResponse(() => serviceFailure("UNRECOGNIZED_CODE", 422), async () => {
    const result = await new QueryReader("get_notification", validateNotification).run({ notificationId: "ntf-1" });
    assert.equal(result.error.code, "UNAVAILABLE"); assert.equal(result.error.httpStatus, null);
  });
});
