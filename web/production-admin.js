/* 平台管理员查看参数化 SQL 模板和执行记录。 */
let productionAdminRequest = 0;
const productionApi = "/api/admin/production-sql";
const productionPageSize = 20;
const productionPages = { routes: 0, history: 0 };

function productionTimestamp(value) {
  if (!value) return "—";
  const time = new Date(value);
  return Number.isNaN(time.getTime()) ? enterpriseEscape(value) : time.toLocaleString("zh-CN", { hour12: false });
}

function productionRouteRow(route) {
  const id = enterpriseEscape(route.id);
  const parameters = (route.parameters || []).map(enterpriseEscape).join("、");
  return `<tr>
    <td>${enterpriseEscape(route.example_question)}<details><summary>查看 SQL 模板</summary>
      <div>路由 ID <code>${id}</code></div><div>参数 ${parameters || "无"}</div><pre>${enterpriseEscape(route.sql)}</pre></details></td>
    <td>${route.enabled ? "已启用" : "已停用"}</td>
    <td>${enterpriseEscape(route.use_count ?? 0)}</td><td>${productionTimestamp(route.last_used_at)}</td>
    <td><button type="button" class="ghost" data-production-id="${id}" data-enable="${!route.enabled}">${route.enabled ? "停用" : "启用"}</button></td>
  </tr>`;
}

function productionHistoryRow(entry) {
  const outcomes = { ok: "成功", success: "成功", empty: "无匹配记录", error: "失败", disabled: "路由已停用" };
  const outcome = Object.hasOwn(outcomes, entry.outcome) ? outcomes[entry.outcome] : entry.outcome;
  return `<tr><td>${productionTimestamp(entry.executed_at)}</td><td>${enterpriseEscape(entry.username)}</td>
    <td>${enterpriseEscape(entry.question)}<details><summary>查询参数</summary><pre>${enterpriseEscape(JSON.stringify(entry.parameters || {}, null, 2))}</pre></details></td>
    <td><code>${enterpriseEscape(entry.route_id)}</code></td><td>${enterpriseEscape(outcome)}</td></tr>`;
}

async function loadProductionAdmin() {
  if (!isSuperAdmin) return;
  const request = ++productionAdminRequest;
  const status = document.getElementById("production-sql-status");
  status.textContent = "正在读取路由与查询记录…";
  const reads = Object.entries(productionPages).map(([key, page]) => {
    document.getElementById(`production-${key}-prev`).disabled = true;
    document.getElementById(`production-${key}-next`).disabled = true;
    return enterpriseJson(`${productionApi}/${key}?limit=${productionPageSize + 1}&offset=${page * productionPageSize}`);
  });
  const [routeData, historyData] = await Promise.allSettled(reads);
  if (request !== productionAdminRequest || !isSuperAdmin) return;
  const tables = [
    [routeData, "routes", "production-routes-body", productionRouteRow, "暂无生产查询路由。"],
    [historyData, "history", "production-history-body", productionHistoryRow, "暂无生产查询记录。"],
  ];
  for (const [result, key, target, render, emptyMessage] of tables) {
    const rows = result.status === "fulfilled" && result.value[key];
    const body = document.getElementById(target);
    document.getElementById(`production-${key}-prev`).disabled = productionPages[key] === 0;
    document.getElementById(`production-${key}-next`).disabled = !Array.isArray(rows) || rows.length <= productionPageSize;
    document.getElementById(`production-${key}-page`).textContent = `第 ${productionPages[key] + 1} 页`;
    if (Array.isArray(rows) && rows.length) body.innerHTML = rows.slice(0, productionPageSize).map(render).join("");
    else {
      const message = Array.isArray(rows) ? emptyMessage : "读取失败，请刷新重试。";
      body.innerHTML = `<tr><td colspan="5" class="muted">${message}</td></tr>`;
    }
  }
  status.textContent = routeData.status === "rejected" || historyData.status === "rejected" ? "部分数据未能加载。" : "";
}

async function setProductionRoute(event) {
  const button = event.target.closest("button[data-production-id]");
  if (!isSuperAdmin || !button || button.disabled) return;
  button.disabled = true;
  const enabled = button.dataset.enable === "true";
  const status = document.getElementById("production-sql-status");
  try {
    await enterpriseJson(productionApi + "/routes/" + encodeURIComponent(button.dataset.productionId), {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }),
    });
    await loadProductionAdmin();
    if (isSuperAdmin && !status.textContent) status.textContent = enabled ? "路由已启用。" : "路由已停用。";
  } catch (error) {
    status.textContent = "路由状态更新失败：" + error.message;
  } finally {
    button.disabled = false;
  }
}

document.getElementById("production-sql-refresh").addEventListener("click", loadProductionAdmin);
document.getElementById("production-routes-body").addEventListener("click", setProductionRoute);
for (const key of Object.keys(productionPages)) {
  for (const [direction, step] of [["prev", -1], ["next", 1]]) {
    const button = document.getElementById(`production-${key}-${direction}`);
    button.addEventListener("click", () => {
      if (!isSuperAdmin || button.disabled) return;
      productionPages[key] += step;
      return loadProductionAdmin();
    });
  }
}
