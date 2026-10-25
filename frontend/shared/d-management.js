import { getCredential } from "./auth.js";
import { canRead, notificationInput, auditGetInput, auditListInput, validateNotification,
  validateAudit, validateAuditPage, QueryReader } from "./d-query.js";

const $ = (id) => document.getElementById(id);
const deliveryLabels = { PENDING: "等待投递", SENDING: "投递中", SUBMITTED: "邮件服务器已受理", FAILED: "投递失败", UNKNOWN: "结果未知" };
const resultLabels = { SUCCESS: "成功", REJECTED: "拒绝", FAILED: "失败", UNKNOWN: "未知" };
const clock = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", dateStyle: "medium", timeStyle: "medium", hour12: false });
const displayTime = (value) => value === null ? "—" : clock.format(new Date(value)) + "（北京时间）";

function node(tag, value, className) {
  const item = document.createElement(tag);
  if (value !== undefined) item.textContent = value;
  if (className) item.className = className;
  return item;
}

function fields(values) {
  const list = node("dl", undefined, "record-fields");
  for (const [label, value] of values) {
    const row = node("div");
    row.append(node("dt", label), node("dd", value ?? "—"));
    list.append(row);
  }
  return list;
}

function notificationView(data) {
  const article = node("article", undefined, "record-card");
  article.append(node("h3", data.title), node("p", data.body, "record-body"), fields([
    ["通知编号", data.notificationId], ["请求编号", data.requestId], ["来源模块", data.sourceModule],
    ["业务编号", data.businessId], ["空间编号", data.spaceId], ["通知类型", data.kind],
    ["渠道", data.channel], ["创建时间", displayTime(data.createdAt)],
  ]), node("h4", `收件人投递状态 · ${data.deliveries.length} 人`));
  const deliveries = node("ul", undefined, "delivery-list");
  for (const item of data.deliveries) {
    const row = node("li");
    const badge = node("span", `${deliveryLabels[item.status]} · ${item.status}`, "status-badge");
    badge.dataset.status = item.status;
    row.append(badge, fields([
      ["收件用户", item.recipientUserId], ["尝试次数", String(item.attemptCount)],
      ["最近尝试", displayTime(item.lastAttemptAt)], ["原因", item.reason || "—"],
    ]));
    deliveries.append(row);
  }
  article.append(deliveries);
  return article;
}

function auditView(data, expanded = false) {
  const detail = node("details", undefined, "audit-record");
  detail.open = expanded;
  const summary = node("summary");
  summary.append(node("span", `${data.sourceModule} / ${data.action}`, "audit-action"),
    node("span", `${resultLabels[data.result]} · ${data.result}`, "status-badge"),
    node("span", displayTime(data.occurredAt), "audit-time"));
  detail.append(summary, fields([
    ["审计编号", data.auditId], ["请求编号", data.requestId], ["来源模块", data.sourceModule],
    ["操作主体类型", data.actorType], ["操作主体", data.actorId], ["对象类型", data.objectType],
    ["业务编号", data.businessId], ["空间编号", data.spaceId], ["动作", data.action],
    ["发生时间", displayTime(data.occurredAt)], ["结果", data.result], ["摘要", data.summary], ["校验方", data.verifiedBy],
  ]));
  return detail;
}

export function attachManagement(onExpired) {
  const root = $("management");
  if (!root) return { setIdentity() {} };
  let identity = null;
  let userId = null;
  const channels = [
    { id: "notice", permission: "d.notify.read", fn: "get_notification", validate: validateNotification,
      build: (v) => notificationInput(v.kind, v.identifier), render: notificationView },
    { id: "audit-get", permission: "d.audit.read", fn: "audit", validate: validateAudit,
      build: (v) => auditGetInput(v.kind, v.identifier, v.sourceModule), render: (v) => auditView(v, true) },
    { id: "audit-list", permission: "d.audit.read", fn: "audit", validate: validateAuditPage,
      build: auditListInput, list: true },
  ];
  function message(channel, value, kind = "info") {
    const box = $(channel.id + "-message");
    box.textContent = value;
    box.dataset.kind = kind;
    box.hidden = !value;
  }
  function updateButtons(channel) {
    const allowed = canRead(identity, channel.permission);
    channel.form.querySelector('button[type="submit"]').disabled = !allowed || channel.reader.busy;
    if (channel.list) $("audit-next").disabled = !allowed || channel.reader.busy || !channel.reader.nextCursor;
    $(channel.id + "-result").setAttribute("aria-busy", String(channel.reader.busy));
  }
  function clear(channel) {
    channel.reader.reset();
    $(channel.id + "-result").replaceChildren();
    message(channel, "");
    updateButtons(channel);
  }
  async function query(channel, next = false) {
    if (!canRead(identity, channel.permission) || channel.reader.busy) return;
    let input, token;
    try { token = getCredential(); input = channel.build(Object.fromEntries(new FormData(channel.form))); }
    catch (err) { clear(channel); message(channel, err.message, "error"); return; }
    $(channel.id + "-result").replaceChildren();
    message(channel, "正在查询…");
    const pending = channel.reader.run(input, next);
    updateButtons(channel);
    const outcome = await pending;
    let currentToken;
    try { currentToken = getCredential(); }
    catch (err) { clear(channel); message(channel, err.message, "error"); return; }
    if (currentToken !== token) {
      setIdentity(null); onExpired(); return;
    }
    if (!outcome) return; // Filters/identity changed while this request was in flight.
    updateButtons(channel);
    if (outcome.error) {
      const err = outcome.error;
      if (err.httpStatus === 401) { setIdentity(null); onExpired(); return; }
      let help = "";
      if (err.code === "CONFLICT") help = channel.id === "notice"
        ? " 请求编号对应多条可见通知，请改用通知编号查询。"
        : " 请求编号存在歧义，请指定来源模块或改用审计编号。";
      if (err.code === "NOT_FOUND" && channel.id === "notice") help = " 未找到可见记录不代表原通知未受理，请勿据此重复发送。";
      if (channel.list && err.code === "INVALID_ARGUMENT") {
        help = " 请检查条件并从第一页重新查询。";
      }
      if (channel.list && next && channel.reader.nextCursor) help += " 可点击“下一页”重试同一页，或重新查询第一页。";
      message(channel, `${err.code || "UNAVAILABLE"}：${err.message || "查询失败，请重试。"}${help}`, "error");
      return;
    }
    const result = $(channel.id + "-result");
    if (channel.list) {
      for (const item of outcome.data.items) result.append(auditView(item));
      message(channel, `第 ${outcome.page} 页 · ${outcome.data.items.length} 条记录${channel.reader.nextCursor ? "，可继续查看下一页。" : "，已无下一页。"}`);
    } else { result.append(channel.render(outcome.data)); message(channel, "查询成功，以下为本次查询结果。"); }
  }
  function setIdentity(value) {
    identity = value;
    const changedUser = value && userId !== value.userId;
    if (value) userId = value.userId;
    for (const channel of channels) {
      if (changedUser) channel.form.reset();
      clear(channel);
    }
    const notice = Boolean(canRead(identity, "d.notify.read"));
    const audit = Boolean(canRead(identity, "d.audit.read"));
    root.hidden = !notice && !audit;
    $("notice-panel").hidden = !notice;
    $("audit-panel").hidden = !audit;
    $("notice-link").hidden = !notice;
    $("audit-link").hidden = !audit;
  }
  for (const channel of channels) {
    channel.reader = new QueryReader(channel.fn, channel.validate);
    channel.form = $(channel.id + "-form");
    channel.form.addEventListener("submit", (event) => { event.preventDefault(); void query(channel); });
    const changed = () => {
      const queried = channel.reader.base !== null;
      clear(channel);
      if (queried) message(channel, "条件已变更，请重新查询第一页。");
    };
    channel.form.addEventListener("input", changed);
    channel.form.addEventListener("change", changed);
  }
  $("audit-next").addEventListener("click", () => { void query(channels[2], true); });
  setIdentity(null);
  return { setIdentity };
}
