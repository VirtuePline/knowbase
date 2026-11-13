/* 企业知识库：部门、文档可见范围和账号上下文。最终授权由服务端执行。 */
let enterpriseIdentity = null;
let enterpriseDepartments = [];
let enterpriseUsers = [];

function enterpriseEscape(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function enterpriseDepartmentName(id) { return enterpriseDepartments.find((d) => d.id === id)?.name || id || "未归属"; }
function enterpriseManagedDepartments() {
  return enterpriseDepartments.filter((d) => enterpriseIdentity?.role === "admin" || enterpriseIdentity?.department_roles?.[d.id] === "admin");
}
function enterpriseWritableDepartments() {
  return enterpriseDepartments.filter((d) => enterpriseIdentity?.role === "admin" || ["admin", "editor"].includes(enterpriseIdentity?.department_roles?.[d.id]));
}
function enterpriseCanWrite(doc) {
  if (!isAdmin) return false;
  if (typeof doc?.can_write === "boolean") return doc.can_write;
  if (enterpriseIdentity?.role === "admin") return true;
  return doc?.visibility !== "public" && ["admin", "editor"].includes(enterpriseIdentity?.department_roles?.[doc?.department_id || "operations"]);
}
function enterpriseCanManageAccess(doc) {
  if (!isAdmin) return false;
  if (typeof doc?.can_manage_access === "boolean") return doc.can_manage_access;
  return enterpriseIdentity?.role === "admin" || (doc?.visibility !== "public" && enterpriseIdentity?.department_roles?.[doc?.department_id || "operations"] === "admin");
}
function enterpriseHistoryKey() {
  return `kb_chat_history:${enterpriseIdentity?.account || enterpriseIdentity?.user || "unknown"}:${enterpriseIdentity?.permission_stamp || "initial"}`;
}
function enterpriseClearSession() {
  try { localStorage.removeItem(enterpriseHistoryKey()); localStorage.removeItem("kb_chat_history"); } catch {}
  try {
    if (typeof startNewChat === "function") startNewChat({ focus: false });
    else { history.length = 0; historyConversationId = ""; log.innerHTML = ""; }
  } catch {}
}
function enterpriseOptions(departments, selected = []) {
  return departments.map((d) => `<option value="${enterpriseEscape(d.id)}"${selected.includes(d.id) ? " selected" : ""}>${enterpriseEscape(d.name)}</option>`).join("");
}
function enterpriseScopeMarkup(prefix) {
  return `<div class="document-scope" data-scope="${prefix}">
    <label>所属部门<select id="${prefix}-department" required></select></label>
    <label>可见范围<select id="${prefix}-visibility"><option value="department">部门文档</option><option value="public">企业公共文档（无需登录可访问）</option></select></label>
    <label class="scope-shared">共享给其他部门<select id="${prefix}-shared" multiple aria-label="共享部门，可多选"></select><small>不选则仅本部门可见；共享仅授予阅读权限。</small></label>
  </div>`;
}
function enterpriseRenderScope(prefix, doc = null, editing = false) {
  const container = document.querySelector(`[data-scope="${prefix}"]`);
  if (!container) return;
  const writable = enterpriseWritableDepartments();
  const owner = doc?.department_id || document.getElementById(`${prefix}-department`)?.value || writable[0]?.id || "";
  const options = [...writable];
  if (owner && !options.some((d) => d.id === owner)) options.push({ id: owner, name: enterpriseDepartmentName(owner) });
  const department = document.getElementById(`${prefix}-department`);
  department.innerHTML = enterpriseOptions(options, [owner]);
  const visibility = document.getElementById(`${prefix}-visibility`);
  visibility.value = doc?.visibility || "department";
  visibility.querySelector('option[value="public"]').disabled = enterpriseIdentity?.role !== "admin";
  const shared = document.getElementById(`${prefix}-shared`);
  shared.innerHTML = enterpriseOptions(enterpriseDepartments.filter((d) => d.id !== owner), doc?.shared_department_ids || []);
  container.querySelectorAll("select").forEach((select) => { select.disabled = editing; });
  container.classList.toggle("scope-locked", editing);
  container.title = editing ? "已有文档的归属与共享范围请在文档列表中使用“管理权限”修改" : "";
  const update = () => {
    container.querySelector(".scope-shared").hidden = visibility.value === "public";
    shared.disabled = editing || (enterpriseIdentity?.role !== "admin" && enterpriseIdentity?.department_roles?.[department.value] !== "admin");
  };
  visibility.onchange = update;
  department.onchange = () => {
    const selected = [...shared.selectedOptions].map((option) => option.value);
    shared.innerHTML = enterpriseOptions(enterpriseDepartments.filter((d) => d.id !== department.value), selected);
    update();
  };
  update();
}
function enterpriseScope(prefix) {
  const department = document.getElementById(`${prefix}-department`);
  if (!department?.value) throw new Error("请先选择所属部门");
  const visibility = document.getElementById(`${prefix}-visibility`).value;
  const canShare = enterpriseIdentity?.role === "admin" || enterpriseIdentity?.department_roles?.[department.value] === "admin";
  return { department_id: department.value, visibility,
    shared_department_ids: visibility === "public" || !canShare ? [] : [...document.getElementById(`${prefix}-shared`).selectedOptions].map((option) => option.value) };
}
function enterpriseAppendScope(form, prefix) {
  const scope = enterpriseScope(prefix);
  form.append("department_id", scope.department_id);
  form.append("visibility", scope.visibility);
  form.append("shared_department_ids", JSON.stringify(scope.shared_department_ids));
}
function enterpriseDocumentLabel(doc) {
  const shared = doc.shared_department_ids || [];
  return `<div class="document-access"><span>${enterpriseEscape(doc.department_name || enterpriseDepartmentName(doc.department_id || "operations"))}</span><span>${doc.visibility === "public" ? "企业公共" : "部门文档"}</span>${shared.length ? `<span>共享：${shared.map(enterpriseDepartmentName).map(enterpriseEscape).join("、")}</span>` : ""}</div>`;
}
async function enterpriseJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "操作失败，请重试");
  return data;
}
function enterpriseDepartmentRolesMarkup(prefix, selected = [], roles = {}) {
  return enterpriseManagedDepartments().map((department) => `<label class="department-role"><input type="checkbox" data-membership="${enterpriseEscape(department.id)}"${selected.includes(department.id) ? " checked" : ""} />${enterpriseEscape(department.name)}<select data-department-role="${enterpriseEscape(department.id)}">${[["editor", "文档编辑者"], ["admin", "部门管理员"]].map(([role, label]) => `<option value="${role}"${(roles[department.id] === "admin" ? "admin" : "editor") === role ? " selected" : ""}>${label}</option>`).join("")}</select></label>`).join("") || '<span class="muted">暂无可管理的部门</span>';
}
function enterpriseMembershipValue(container, existing = null) {
  const managed = new Set(enterpriseManagedDepartments().map((department) => department.id));
  const ids = (existing?.department_ids || []).filter((id) => !managed.has(id));
  const roles = Object.fromEntries(Object.entries(existing?.department_roles || {}).filter(([id]) => !managed.has(id)));
  container.querySelectorAll("[data-membership]:checked").forEach((checkbox) => {
    const id = checkbox.dataset.membership;
    ids.push(id);
    roles[id] = [...container.querySelectorAll("[data-department-role]")].find((select) => select.dataset.departmentRole === id)?.value || "editor";
  });
  return { department_ids: ids, department_roles: roles };
}
function enterpriseUsersRows(users) {
  enterpriseUsers = users;
  return users.map((user) => {
    const account = user.account || user.username;
    const admin = user.role === "admin";
    const departments = (user.department_ids || []).map((id) => `${enterpriseEscape(enterpriseDepartmentName(id))} · ${user.department_roles?.[id] === "admin" ? "部门管理员" : ["editor", "member"].includes(user.department_roles?.[id]) ? "文档编辑者" : "角色异常"}`).join("<br />") || "未分配部门";
    return `<tr><td>${enterpriseEscape(account)}</td><td>${enterpriseEscape(user.display_name || account)}</td><td>${admin ? "平台管理员" : departments}</td><td>${enterpriseEscape(String(user.created_at || "").slice(0, 16))}</td><td>${admin ? "—" : `<button type="button" class="ghost" data-membership-user="${enterpriseEscape(account)}">部门与角色</button>${isSuperAdmin ? `<button class="ghost danger" type="button" data-del-user="${enterpriseEscape(account)}">删除</button>` : ""}`}</td></tr>`;
  }).join("");
}
function enterpriseOpenDialog(title, body, onSave, { saveLabel = "保存", destructive = false } = {}) {
  document.getElementById("enterprise-dialog")?.remove();
  const dialog = document.createElement("dialog");
  dialog.id = "enterprise-dialog";
  dialog.className = "enterprise-dialog";
  dialog.innerHTML = `<form method="dialog"><h2>${enterpriseEscape(title)}</h2><div class="enterprise-dialog-body">${body}</div><p class="enterprise-error" role="alert"></p><div class="enterprise-dialog-actions"><button value="cancel" class="ghost">取消</button><button type="button" class="primary${destructive ? " danger" : ""}" data-enterprise-save>${enterpriseEscape(saveLabel)}</button></div></form>`;
  document.body.appendChild(dialog);
  dialog.querySelector("[data-enterprise-save]").addEventListener("click", async (event) => {
    event.target.disabled = true;
    try { await onSave(dialog); dialog.close(); dialog.remove(); }
    catch (error) { dialog.querySelector(".enterprise-error").textContent = error.message; }
    finally { event.target.disabled = false; }
  });
  dialog.showModal();
  return dialog;
}
async function enterpriseEditAccess(doc) {
  if (!enterpriseCanManageAccess(doc)) return;
  const dialog = enterpriseOpenDialog(`管理权限：${doc.name}`, enterpriseScopeMarkup("access"), async () => {
    await enterpriseJson(`/api/documents/${encodeURIComponent(doc.id)}/access`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(enterpriseScope("access")) });
    enterpriseClearSession();
    window.location.reload();
  });
  enterpriseRenderScope("access", doc);
  if (enterpriseIdentity?.role !== "admin") dialog.querySelector("#access-department").disabled = true;
}
function enterpriseDepartmentDirectoryRows() {
  return enterpriseDepartments.map((department) => `<div class="department-directory-row"><span>${enterpriseEscape(department.name)} <small>${enterpriseEscape(department.id)}</small></span>${isSuperAdmin ? `<div class="department-directory-actions"><button class="ghost" type="button" data-rename-department="${enterpriseEscape(department.id)}">重命名</button><button class="ghost danger" type="button" data-delete-department="${enterpriseEscape(department.id)}"${enterpriseDepartments.length <= 1 ? ' disabled title="至少保留一个部门"' : ""}>删除</button></div>` : ""}</div>`).join("");
}
async function enterpriseDeleteDepartment(department) {
  if (!isSuperAdmin || !department) return;
  try {
    const preview = await enterpriseJson(`/api/departments/${encodeURIComponent(department.id)}/deletion`);
    const targets = (preview.departments || []).filter((item) => item.id !== department.id);
    if (!targets.length) { toast("至少保留一个部门，请先创建其他部门"); return; }
    const documents = Number(preview.documents) || 0;
    const members = Number(preview.members) || 0;
    const sharedDocuments = Number(preview.shared_documents) || 0;
    const body = `<div class="department-deletion-content"><p>将删除部门「${enterpriseEscape(preview.department?.name || department.name)}」。</p>
      <div class="department-deletion-counts"><span>所属文档 <strong>${documents}</strong></span><span>部门成员 <strong>${members}</strong></span><span>共享文档 <strong>${sharedDocuments}</strong></span></div>
      <label>文档迁移至<select data-department-transfer${documents ? " required" : ""}><option value="">${documents ? "请选择接收文档的部门" : "无需迁移文档"}</option>${enterpriseOptions(targets)}</select></label>
      <p class="muted">所属文档、正文和附件会保留；迁移后目标部门成员可访问原部门文档，公共文档保留公共范围。</p>
      <p class="muted">成员会移除该部门归属与角色，账号保留，不会自动加入目标部门；共享给该部门的授权也会移除。</p></div>`;
    return enterpriseOpenDialog(`删除部门：${preview.department?.name || department.name}`, body, async (dialog) => {
      const target = dialog.querySelector("[data-department-transfer]").value;
      if ((documents && !target) || (target && !targets.some((item) => item.id === target))) {
        throw new Error("请选择接收文档的部门");
      }
      await enterpriseJson(`/api/departments/${encodeURIComponent(department.id)}`, {
        method: "DELETE", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_department_id: target }),
      });
      enterpriseClearSession();
      window.location.reload();
    }, { saveLabel: "删除部门", destructive: true });
  } catch (error) { toast(error.message); }
}
async function enterpriseLoadDepartments() {
  const data = await enterpriseJson("/api/departments");
  enterpriseDepartments = data.departments || [];
  for (const [host, prefix] of [["doc-scope-host", "doc"], ["record-scope-host", "record"]]) {
    const container = document.getElementById(host);
    if (container && !container.children.length) container.innerHTML = enterpriseScopeMarkup(prefix);
  }
  document.querySelectorAll("[data-scope]").forEach((container) => enterpriseRenderScope(container.dataset.scope));
  const registration = document.getElementById("reg-departments");
  if (registration) registration.innerHTML = enterpriseDepartmentRolesMarkup("reg");
  const directory = document.getElementById("department-directory");
  if (directory) directory.innerHTML = enterpriseDepartmentDirectoryRows();
}
async function enterpriseInitialize(data) {
  const publicAccess = data.public_access === true || portal === "search";
  enterpriseIdentity = publicAccess ? { ...data, public_access: true, role: "guest", account: "", user: "",
    department_ids: [], department_roles: {}, can_write: false, can_manage_users: false } : data;
  try { localStorage.removeItem("kb_chat_history"); } catch {}
  document.body.classList.toggle("department-manager", !publicAccess && portal === "admin" && data.can_manage_users === true);
  const context = document.querySelector(".workspace-context");
  if (context) context.textContent = publicAccess ? "公共访问 · 仅公共文档" : "企业公共知识 · 部门知识共享";
  if (publicAccess) { enterpriseDepartments = []; enterpriseUsers = []; return; }
  await enterpriseLoadDepartments();
}

// API 会话失效时统一回到登录页；不把未授权响应当作空列表继续显示。
const enterpriseOriginalFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const response = await enterpriseOriginalFetch(...args);
  if (response.status === 401 && String(args[0]).startsWith("/api/")) {
    enterpriseClearSession();
    if (enterpriseIdentity?.public_access !== true) window.location.replace("/login");
  }
  return response;
};
document.addEventListener("click", async (event) => {
  const access = event.target.closest("[data-document-access]");
  if (access) {
    const doc = [...docDocs, ...recDocs].find((item) => item.id === access.dataset.documentAccess);
    if (doc) enterpriseEditAccess(doc);
  }
  const membership = event.target.closest("[data-membership-user]");
  if (membership && isAccountAdmin) {
    const user = enterpriseUsers.find((item) => (item.account || item.username) === membership.dataset.membershipUser);
    if (!user) return;
    enterpriseOpenDialog(`部门与角色：${user.display_name || user.username}`, enterpriseDepartmentRolesMarkup("member", user.department_ids, user.department_roles), async (dialog) => {
      await enterpriseJson(`/api/admin/users/${encodeURIComponent(membership.dataset.membershipUser)}/departments`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(enterpriseMembershipValue(dialog, user)) });
      await loadUsers();
      if ((user.account || user.username) === currentUser) window.location.reload();
    });
  }
  const rename = event.target.closest("[data-rename-department]");
  if (rename && isSuperAdmin) {
    const department = enterpriseDepartments.find((item) => item.id === rename.dataset.renameDepartment);
    const name = prompt("部门名称", department?.name || "");
    if (!name?.trim()) return;
    try { await enterpriseJson(`/api/departments/${encodeURIComponent(department.id)}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name.trim() }) }); await enterpriseLoadDepartments(); }
    catch (error) { toast(error.message); }
  }
  const remove = event.target.closest("[data-delete-department]");
  if (remove && isSuperAdmin) {
    await enterpriseDeleteDepartment(enterpriseDepartments.find((item) => item.id === remove.dataset.deleteDepartment));
  }
  if (event.target.closest("#department-create") && isSuperAdmin) {
    const id = document.getElementById("department-id").value.trim();
    const name = document.getElementById("department-name").value.trim();
    if (!id || !name) { toast("请填写部门标识和名称"); return; }
    try { await enterpriseJson("/api/departments", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id, name }) }); await enterpriseLoadDepartments(); document.getElementById("department-id").value = ""; document.getElementById("department-name").value = ""; }
    catch (error) { toast(error.message); }
  }
});
// 权限调整后刷新浏览器上下文，避免复用旧权限下的回答和历史。
setInterval(async () => {
  if (!enterpriseIdentity || document.hidden) return;
  try {
    const response = await fetch("/api/mode");
    if (!response.ok) return;
    const data = await response.json();
    if (data.permission_stamp !== enterpriseIdentity.permission_stamp) {
      enterpriseClearSession();
      window.location.reload();
    }
  } catch {}
}, 60000);
