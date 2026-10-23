import { api } from "./auth.js";

const sources = ["M1", "M2", "M3", "D"];
const text = (v) => typeof v === "string" && Boolean(v.trim());
const nullableText = (v) => v === null || text(v);
const time = (v) => text(v) && /(?:Z|[+-]\d{2}:\d{2})$/.test(v) && Number.isFinite(Date.parse(v));
const invalid = (message) => Object.assign(new Error(message), { code: "INVALID_ARGUMENT", httpStatus: null });
const malformed = () => Object.assign(new Error("服务响应不完整，请重试查询。"), { code: "UNAVAILABLE", httpStatus: null });

export function canRead(identity, permission) {
  return identity?.accountStatus === "ACTIVE" && identity.grants.some((g) => g.permissionCode === permission);
}

function identifier(kind, value, kinds) {
  if (!kinds.includes(kind) || !text(value)) throw invalid("请选择编号类型并填写编号。");
  return { [kind]: value.trim() };
}

export function notificationInput(kind, value) {
  return identifier(kind, value, ["notificationId", "requestId"]);
}

export function auditGetInput(kind, value, source) {
  const input = { operation: "GET", ...identifier(kind, value, ["auditId", "requestId"]) };
  if (source) {
    if (!sources.includes(source)) throw invalid("来源模块不正确。");
    input.sourceModule = source;
  }
  return input;
}

// The form explicitly uses Beijing time; conversion must not depend on the device timezone.
export function beijingTime(value) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(value)) throw invalid("请输入完整的北京时间。");
  const normalized = value.length === 16 ? value + ":00" : value;
  const stamp = Date.parse(normalized + "Z");
  if (!Number.isFinite(stamp) || new Date(stamp).toISOString().slice(0, 19) !== normalized) {
    throw invalid("时间日期无效，请检查年月日和时分。");
  }
  return normalized + "+08:00";
}

export function auditListInput(values) {
  const input = { operation: "LIST" };
  for (const key of ["sourceModule", "businessId", "objectType", "action", "actorUserId", "spaceId"]) {
    if (text(values[key])) input[key] = values[key].trim();
  }
  if (input.sourceModule && !sources.includes(input.sourceModule)) throw invalid("来源模块不正确。");
  for (const key of ["timeFrom", "timeTo"]) if (values[key]) input[key] = beijingTime(values[key]);
  if (input.timeFrom && input.timeTo && Date.parse(input.timeFrom) >= Date.parse(input.timeTo)) {
    throw invalid("开始时间必须早于结束时间；查询不包含结束时刻。");
  }
  const rawLimit = String(values.limit ?? "20");
  if (!/^\d+$/.test(rawLimit) || Number(rawLimit) < 1 || Number(rawLimit) > 100) throw invalid("每页条数须为 1 到 100 的整数。");
  input.limit = Number(rawLimit);
  return input;
}

export function validateNotification(v) {
  if (!v || !["notificationId", "requestId", "businessId", "title", "body", "kind"].every((key) => text(v[key])) ||
      !sources.slice(0, 3).includes(v.sourceModule) || !v.kind.startsWith(v.sourceModule + "_") ||
      v.channel !== "EMAIL" || !nullableText(v.spaceId) || !time(v.createdAt) ||
      !Array.isArray(v.deliveries) || !v.deliveries.length || !v.deliveries.every((d) => d && text(d.recipientUserId) &&
        ["PENDING", "SENDING", "SUBMITTED", "FAILED", "UNKNOWN"].includes(d.status) &&
        Number.isInteger(d.attemptCount) && d.attemptCount >= 0 && (d.lastAttemptAt === null || time(d.lastAttemptAt)) &&
        typeof d.reason === "string")) throw malformed();
  return v;
}

export function validateAudit(v) {
  if (!v || !["auditId", "requestId", "objectType", "businessId", "action", "summary"].every((key) => text(v[key])) ||
      !sources.includes(v.sourceModule) || !sources.includes(v.verifiedBy) || !nullableText(v.spaceId) ||
      !["USER", "SYSTEM", "UNKNOWN"].includes(v.actorType) || !nullableText(v.actorId) || !time(v.occurredAt) ||
      !["SUCCESS", "REJECTED", "FAILED", "UNKNOWN"].includes(v.result)) throw malformed();
  return v;
}

export function validateAuditPage(v) {
  if (!v || !Array.isArray(v.items) || !nullableText(v.nextCursor)) throw malformed();
  v.items.forEach(validateAudit);
  return v;
}

// Changing filters or revalidating identity invalidates in-flight reads as well as pagination.
// A failed next-page read retains its original cursor so the user can retry that same page.
export class QueryReader {
  constructor(functionName, validate, call = api) {
    this.functionName = functionName;
    this.validate = validate;
    this.call = call;
    this.reset();
  }
  reset() {
    this.generation = (this.generation || 0) + 1;
    this.busy = false;
    this.base = null;
    this.nextCursor = null;
    this.page = 0;
  }
  async run(input, next = false) {
    if (this.busy) return null;
    if (next && (!this.base || !this.nextCursor)) return null;
    const generation = this.generation;
    if (!next) { this.base = { ...input }; this.nextCursor = null; this.page = 0; }
    const request = next ? { ...this.base, cursor: this.nextCursor } : { ...this.base };
    this.busy = true;
    try {
      const data = this.validate(await this.call("D", this.functionName, request));
      if (this.generation !== generation) return null;
      this.nextCursor = data.nextCursor ?? null;
      this.page += 1;
      return { data, page: this.page };
    } catch (error) {
      if (this.generation !== generation) return null;
      if (request.operation === "LIST" && error.code === "INVALID_ARGUMENT") this.reset();
      return { error };
    } finally { if (this.generation === generation) this.busy = false; }
  }
}
