"use strict";

const accountPage = document.body.dataset.accountPage;
const isRegisterPage = accountPage === "register";
const isChangePasswordPage = accountPage === "change-password";
const accountForm = document.querySelector("#account-form");
const accountFields = document.querySelector("#account-fields");
const usernameField = document.querySelector("#account-username");
const accountIdentifierField = document.querySelector("#account-identifier");
const passwordField = document.querySelector("#account-password");
const currentPasswordField = document.querySelector("#account-current-password");
const newPasswordField = document.querySelector("#account-new-password");
const confirmField = document.querySelector("#account-confirm");
const submitButton = document.querySelector("#account-submit");
const errorBox = document.querySelector("#account-error");
const successBox = document.querySelector("#account-success");
const statusBox = document.querySelector("#account-status");
let accountReady = false;
let submittingAccount = false;
let mustSetPassword = false;
let accountDepartments = [];
let accountIdentity = null;
function accountEscape(value) { return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "\'": "&#39;" }[character] || character)); }

function accountError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
}

document.querySelectorAll("[data-password-target]").forEach((button) => {
  button.addEventListener("click", () => {
    const input = document.getElementById(button.dataset.passwordTarget);
    if (!input) return;
    const showing = input.type === "password";
    input.type = showing ? "text" : "password";
    button.textContent = showing ? "隐藏" : "显示";
    button.setAttribute("aria-pressed", String(showing));
    button.setAttribute("aria-label", showing ? "隐藏密码" : "显示密码");
  });
});

accountForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!accountReady || submittingAccount) return;
  errorBox.hidden = true;
  successBox.hidden = true;
  let endpoint;
  let payload;
  let username = "";
  let account = "";
  if (isChangePasswordPage) {
    const newPassword = newPasswordField.value;
    const currentPassword = mustSetPassword ? "" : currentPasswordField.value;
    if (!mustSetPassword && !currentPassword) {
      accountError("请输入当前密码"); currentPasswordField.focus(); return;
    }
    const passwordLength = Array.from(newPassword).length;
    if (passwordLength < 8 || passwordLength > 128) {
      accountError("新密码长度应为 8–128 位"); newPasswordField.focus(); return;
    }
    if (newPassword !== confirmField.value) {
      accountError("两次输入的新密码不一致"); confirmField.focus(); return;
    }
    endpoint = "/api/admin/change-password";
    payload = { current_password: currentPassword, new_password: newPassword, confirm_password: confirmField.value };
  } else {
    username = usernameField.value.trim();
    if (isRegisterPage) username = username.normalize("NFKC");
    if (!username) { accountError(isRegisterPage ? "请输入用户名" : "请输入账号或用户名"); usernameField.focus(); return; }
    if (isRegisterPage) {
      account = accountIdentifierField.value.trim().toLowerCase();
      if (!/^[a-z][a-z0-9_.-]{0,31}$/.test(account)) {
        accountError("账号应为 1–32 位，以英文字母开头，可含字母、数字、下划线、点或短横线");
        accountIdentifierField.focus(); return;
      }
      if (!/^[\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9][\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9_.-]{0,31}$/.test(username)) {
        accountError("用户名应为 1–32 位中文、字母、数字、下划线、点或短横线，首位为中文、字母或数字");
        usernameField.focus(); return;
      }
      endpoint = "/api/admin/register";
      const department_ids = [...document.querySelectorAll('[data-account-department]:checked')].map((checkbox) => checkbox.dataset.accountDepartment);
      if (!department_ids.length) { accountError("请选择至少一个所属部门"); return; }
      const department_roles = {};
      document.querySelectorAll('[data-account-role]').forEach((select) => { if (department_ids.includes(select.dataset.accountRole)) department_roles[select.dataset.accountRole] = select.value; });
      payload = { account, username, department_ids, department_roles };
    } else {
      endpoint = "/api/admin/login";
      payload = { username, password: passwordField.value };
    }
  }
  submittingAccount = true;
  submitButton.disabled = true;
  statusBox.textContent = isChangePasswordPage ? "正在保存新密码…" : isRegisterPage ? "正在创建账号…" : "正在验证账号…";
  try {
    const response = await fetch(endpoint, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if ((isRegisterPage || isChangePasswordPage) && response.status === 401) { window.location.replace("/login"); return; }
      const detail = typeof data.detail === "string" ? data.detail : "操作失败，请检查输入后重试";
      throw new Error(detail);
    }
    if (isChangePasswordPage) { window.location.assign("/"); return; }
    if (!isRegisterPage) { window.location.assign(data.must_change_password === true ? "/change-password" : "/"); return; }
    accountForm.reset();
    successBox.textContent = `成员账号「${username}（${account}）」已创建。初始密码为空，首次登录需设置密码。您仍保持管理员登录。`;
    successBox.hidden = false;
    statusBox.textContent = "账号创建成功";
    accountIdentifierField.focus();
  } catch (error) {
    accountError(String(error.message || "连接失败，请稍后重试"));
    statusBox.textContent = "";
  } finally {
    submittingAccount = false;
    submitButton.disabled = false;
  }
});

function accountDisplay(mode) {
  const account = String(mode.account || mode.user || "");
  const name = String(mode.display_name || mode.username || account);
  return name && account && name !== account ? `${name}（${account}）` : name || account || "当前账号";
}

function preparePasswordPage(mode) {
  mustSetPassword = mode.must_change_password === true;
  document.querySelector("#password-user").textContent = accountDisplay(mode);
  document.querySelector("#current-password-row").hidden = mustSetPassword;
  currentPasswordField.disabled = mustSetPassword;
  currentPasswordField.required = !mustSetPassword;
  document.querySelector("#password-back").hidden = mustSetPassword;
  document.querySelector("#password-section-label").textContent = mustSetPassword ? "首次登录" : "账号设置";
  document.querySelector("#account-title").textContent = mustSetPassword ? "设置您的密码" : "修改密码";
  document.querySelector("#password-description").textContent = mustSetPassword
    ? "首次登录需要先设置自己的密码，再进入企业知识工作台。"
    : "验证当前密码，为您自己的账号设置新密码。";
  document.querySelector("#password-context-note").textContent = mustSetPassword ? "设置完成前暂不能使用工作台" : "密码仅用于您自己的账号";
  document.querySelector("#password-note").textContent = mustSetPassword
    ? "新账号初始密码为空，请设置您自己的密码。保存成功后将进入工作台。"
    : "修改成功后将使用新密码登录，当前会话会同步更新。";
  document.querySelector("#password-submit-label").textContent = mustSetPassword ? "设置密码并进入工作台" : "保存新密码并进入工作台";
  document.title = `${mustSetPassword ? "设置您的密码" : "修改密码"} · 企业知识库`;
}

async function initializeAccountPage() {
  try {
    const response = await fetch("/api/mode");
    if (!response.ok) throw new Error("无法读取登录状态，请刷新重试");
    const mode = await response.json();
    if (isRegisterPage && mode.portal !== "admin") throw new Error("请从管理端地址创建账号");
    const authenticated = mode.authenticated === true || mode.admin === true;
    if (isChangePasswordPage) {
      if (!authenticated) { window.location.replace("/login"); return; }
      preparePasswordPage(mode);
    } else {
      if (authenticated && mode.must_change_password === true) { window.location.replace("/change-password"); return; }
      if (!isRegisterPage && authenticated) { window.location.replace("/"); return; }
      if (isRegisterPage) {
        if (!authenticated) { window.location.replace("/login"); return; }
        if (mode.can_manage_users !== true) throw new Error("只有平台管理员或部门管理员可以创建成员账号");
        accountIdentity = mode;
        const response = await fetch("/api/departments");
        if (!response.ok) throw new Error("无法读取部门目录");
        const data = await response.json();
        accountDepartments = (data.departments || []).filter((department) => mode.role === "admin" || mode.department_roles?.[department.id] === "admin");
        document.querySelector("#account-departments").innerHTML = accountDepartments.map((department) => `<label class="account-department"><input type="checkbox" data-account-department="${accountEscape(department.id)}" aria-label="加入${accountEscape(department.name)}" /><span class="department-name">${accountEscape(department.name)}</span><select data-account-role="${accountEscape(department.id)}" aria-label="${accountEscape(department.name)}角色"><option value="editor">文档编辑者</option><option value="admin">部门管理员</option></select></label>`).join("");
        document.querySelector("#register-actor").textContent = accountDisplay(mode);
      }
    }
    accountReady = true;
    accountFields.disabled = false;
    statusBox.textContent = isChangePasswordPage
      ? (mustSetPassword ? "首次登录 · 请先设置密码" : "当前账号 · 修改密码")
      : isRegisterPage ? "创建账号与用户名 · 初始密码为空" : "企业知识库 · 新账号首次登录请将密码留空";
    if (isChangePasswordPage) (mustSetPassword ? newPasswordField : currentPasswordField).focus();
    else (isRegisterPage ? accountIdentifierField : usernameField).focus();
  } catch (error) {
    statusBox.textContent = "";
    accountError(String(error.message || "连接失败，请刷新重试"));
    accountFields.disabled = true;
  }
}

initializeAccountPage();
