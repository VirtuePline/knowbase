/* 随记录保存的附件队列。正文图片仍由编辑器处理，这里只管理用户选择的附件。 */
(function (global) {
  "use strict";
  const MAX_COUNT = 20;
  const MAX_BYTES = 20 * 1024 * 1024;
  const BLOCKED = new Set(["exe", "bat", "cmd", "sh", "ps1", "msi", "dll", "js", "vbs"]);
  const MIME_EXT = { "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp", "application/pdf": "pdf", "application/zip": "zip", "application/octet-stream": "bin", "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx" };

  function clipboardFiles(data) {
    const files = Array.from(data?.files || []);
    if (files.length) return files;
    return Array.from(data?.items || []).filter((item) => item.kind === "file")
      .map((item) => item.getAsFile()).filter(Boolean);
  }
  function batchId() {
    return global.crypto?.randomUUID?.() || `attachments_${Date.now()}_${Math.random().toString(36).slice(2, 14)}`;
  }
  function formatSize(size) {
    return size >= 1024 * 1024 ? `${(size / (1024 * 1024)).toFixed(1)} MB` : `${Math.max(1, Math.ceil(size / 1024))} KB`;
  }
  function escape(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  class Queue {
    constructor(options = {}) {
      this.entries = [];
      this.target = null;
      this.existingCount = 0;
      this.busy = false;
      this.saving = false;
      this.error = "";
      this.lastBatch = null;
      this.serial = 0;
      this.contextVersion = 0;
      this.fetch = options.fetch || ((...args) => global.fetch(...args));
      this.canAdd = options.canAdd || (() => true);
      this.onChange = options.onChange || (() => {});
      this.notify = options.notify || (() => {});
      this.confirm = options.confirm || ((message) => global.confirm(message));
      this.makeForm = options.makeForm || (() => new global.FormData());
    }
    changed() { this.onChange(this); }
    add(files) {
      if (!this.canAdd()) { this.notify("当前账号没有添加附件的权限"); return 0; }
      let added = 0;
      const errors = [];
      for (const file of Array.from(files || [])) {
        const name = String(file.name || "附件");
        const extension = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
        if (BLOCKED.has(extension)) { errors.push(`${name}：不支持可执行文件`); continue; }
        if (!file.size) { errors.push(`${name}：文件为空`); continue; }
        if (file.size > MAX_BYTES) { errors.push(`${name}：超过 20 MB`); continue; }
        if (this.existingCount + this.entries.length >= MAX_COUNT) { errors.push("每条记录最多 20 个附件"); break; }
        this.entries.push({ id: ++this.serial, file });
        added += 1;
      }
      if (errors.length) this.notify(errors.join("；"));
      if (added && !this.lastBatch) this.error = "";
      this.changed();
      return added;
    }
    remove(id) {
      if (this.busy) return false;
      const removedId = Number(id);
      this.entries = this.entries.filter((entry) => entry.id !== removedId);
      // 后加文件尚未参与失败批次；移除它不能改变重试批次，否则响应丢失时会重复上传。
      if (this.lastBatch?.entries.some((entry) => entry.id === removedId)) this.lastBatch = null;
      this.error = "";
      this.changed();
      return true;
    }
    clear() {
      if (this.busy) { this.notify("附件正在上传，请完成后再清空"); return false; }
      this.entries = [];
      this.lastBatch = null;
      this.error = "";
      this.changed();
      return true;
    }
    prepareContextChange({ force = false } = {}) {
      if (this.busy || (this.saving && !force)) { this.notify("记录或附件正在保存，请稍后切换记录"); return false; }
      if (!force && this.entries.length && !this.confirm("还有未上传的附件，切换记录将清空附件队列。是否继续？")) return false;
      this.clear();
      this.target = null;
      this.existingCount = 0;
      this.contextVersion += 1;
      this.changed();
      return true;
    }
    bind(doc) {
      if (!doc?.id) throw new Error("请先保存记录，再上传附件");
      if (this.target && this.target.id !== doc.id && this.entries.length) throw new Error("附件已关联另一条记录，请先完成上传或清空附件队列");
      const changedTarget = this.target?.id !== doc.id;
      this.target = { id: doc.id, name: doc.name || "已保存记录" };
      if (Array.isArray(doc.attachments)) this.existingCount = doc.attachments.length;
      else if (changedTarget) this.existingCount = 0;
      this.changed();
    }
    async upload() {
      if (this.busy) return { busy: true, count: 0 };
      if (!this.entries.length) return { count: 0 };
      if (!this.target?.id) throw new Error("请先保存记录，再上传附件");
      const ids = new Set(this.entries.map((entry) => entry.id));
      const snapshot = this.lastBatch && this.lastBatch.entries.every((entry) => ids.has(entry.id))
        ? this.lastBatch : { id: batchId(), entries: this.entries.slice() };
      this.lastBatch = snapshot;
      this.busy = true;
      this.error = "";
      this.changed();
      try {
        const form = this.makeForm();
        snapshot.entries.forEach((entry) => form.append("files", entry.file));
        form.append("batch_id", snapshot.id);
        const response = await this.fetch(`/api/documents/${encodeURIComponent(this.target.id)}/attachments`, { method: "POST", body: form });
        const data = await response.json();
        if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "附件上传失败");
        const submitted = new Set(snapshot.entries.map((entry) => entry.id));
        this.entries = this.entries.filter((entry) => !submitted.has(entry.id));
        this.existingCount = Array.isArray(data.attachments) ? data.attachments.length : this.existingCount + snapshot.entries.length;
        this.lastBatch = null;
        return { count: snapshot.entries.length, attachments: data.attachments || [] };
      } catch (error) {
        this.error = `记录已保存，附件尚未确认上传：${error.message || "网络异常"}。可直接重试，无需重新创建记录。`;
        throw error;
      } finally {
        this.busy = false;
        this.changed();
      }
    }
    async readClipboard(clipboard = global.navigator?.clipboard) {
      if (!this.canAdd()) { this.notify("当前账号没有添加附件的权限"); return 0; }
      if (!clipboard?.read) {
        this.notify("当前浏览器不支持直接读取剪贴板，请点击附件区域后按 Ctrl+V，或选择附件");
        return 0;
      }
      const contextVersion = this.contextVersion;
      try {
        const items = await clipboard.read();
        const files = [];
        for (const item of items) {
          const type = item.types.find((value) => !value.startsWith("text/") && value !== "application/x-web-custom-data");
          if (!type) continue;
          const blob = await item.getType(type);
          files.push(new global.File([blob], `剪贴板附件-${Date.now()}-${files.length + 1}.${MIME_EXT[type] || "bin"}`, { type, lastModified: Date.now() }));
        }
        if (contextVersion !== this.contextVersion) {
          this.notify("当前记录已切换，未添加刚才读取的剪贴板附件，请在当前记录中重新粘贴");
          return 0;
        }
        if (!files.length) this.notify("剪贴板中没有可读取的文件。复制文件后在附件区域按 Ctrl+V，或点击“选择附件”");
        return this.add(files);
      } catch {
        this.notify("当前无法读取剪贴板，可点击附件区域后按 Ctrl+V，或使用“选择附件”添加文件");
        return 0;
      }
    }
  }

  function mount(options) {
    const root = options.root;
    const input = root.querySelector("#rec-attach");
    const drop = root.querySelector("[data-attachment-drop]");
    const list = root.querySelector("[data-attachment-list]");
    const status = root.querySelector("[data-attachment-status]");
    const count = root.querySelector("[data-attachment-count]");
    const retry = root.querySelector("[data-attachment-retry]");
    const clear = root.querySelector("[data-attachment-clear]");
    const target = root.querySelector("[data-attachment-target]");
    const queue = new Queue({ ...options, onChange: render });
    function render() {
      root.setAttribute("aria-busy", String(queue.busy));
      count.textContent = `${queue.entries.length} 个待上传${queue.existingCount ? ` · 已有 ${queue.existingCount} 个` : ""}`;
      target.textContent = queue.target ? `附件归属：《${queue.target.name}》` : "在线保存后随当前记录上传；上传多份记录文件时，仅附加到第一条成功入库的记录。";
      status.textContent = queue.error || (queue.busy ? "正在上传附件，请稍候…" : queue.entries.length ? "附件已就绪，保存记录时一并上传。" : "支持照片、PDF、文档、表格与压缩包；每个不超过 20 MB，每条记录最多 20 个。" );
      status.classList.toggle("error", !!queue.error);
      retry.hidden = !queue.target || !queue.entries.length || queue.busy;
      retry.textContent = queue.error ? "重试附件上传" : "上传待添加附件";
      clear.disabled = queue.busy || !queue.entries.length;
      list.hidden = !queue.entries.length;
      list.innerHTML = queue.entries.map(({ id, file }) => {
        const ext = String(file.name || "附件").split(".").pop().slice(0, 5).toUpperCase();
        return `<li class="record-attachment-item"><span class="record-attachment-type" aria-hidden="true">${escape(ext)}</span><span class="record-attachment-file"><strong>${escape(file.name)}</strong><small>${formatSize(file.size)}</small></span><button type="button" class="record-attachment-remove" data-attachment-remove="${id}" aria-label="移除附件 ${escape(file.name)}"${queue.busy ? " disabled" : ""}>×</button></li>`;
      }).join("");
      options.onChange?.(queue);
    }
    input.addEventListener("change", () => { queue.add(input.files); input.value = ""; });
    root.querySelector("[data-attachment-choose]").addEventListener("click", () => input.click());
    root.querySelector("[data-attachment-clipboard]").addEventListener("click", () => { drop.focus(); queue.readClipboard(); });
    clear.addEventListener("click", () => queue.clear());
    retry.addEventListener("click", async () => {
      try { const result = await queue.upload(); options.onUploaded?.(result, queue.target); }
      catch (error) { queue.notify(error.message || "附件上传失败，请重试"); }
    });
    list.addEventListener("click", (event) => { const button = event.target.closest("[data-attachment-remove]"); if (button) queue.remove(button.dataset.attachmentRemove); });
    drop.addEventListener("paste", (event) => {
      const files = clipboardFiles(event.clipboardData);
      if (!files.length) return;
      event.preventDefault();
      event.stopPropagation();
      queue.add(files);
    });
    ["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((name) => drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", (event) => { event.stopPropagation(); queue.add(event.dataTransfer.files); });
    render();
    return queue;
  }
  global.RecordAttachments = { Queue, mount, clipboardFiles, MAX_BYTES, MAX_COUNT };
  if (typeof module !== "undefined" && module.exports) module.exports = global.RecordAttachments;
})(typeof window !== "undefined" ? window : globalThis);
