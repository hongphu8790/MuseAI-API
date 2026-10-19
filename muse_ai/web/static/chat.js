// Independent Chat mode: keeps conversations on this browser and resumes Muse sessions.
(() => {
  "use strict";
  const el = (id) => document.getElementById(id);
  const KEY = "muse-ai-web-chat-v1";
  // Do not silently truncate stored chats. If browser storage is full,
  // persist() reports the failure rather than discarding older messages.
  const buttons = [...document.querySelectorAll(".mode-tab")];
  const chatMode = el("chatMode");
  const videoMode = el("videoMode");
  const jobsPanel = el("jobsPanel");
  const chatInput = el("chatInput");
  const sendButton = el("sendChat");
  const messageBox = el("chatMessages");
  const chatNotice = el("chatMessage");
  const fileInput = el("chatFileInput");
  const selectedList = el("chatSelectedFiles");
  let selectedFiles = [];
  const maxFiles = 10;
  const supported = /\.(png|jpg|jpeg|webp|gif|pdf|txt|md|csv|json|docx|xlsx|pptx)$/i;

  function renderSelectedFiles() {
    selectedList.replaceChildren();
    selectedFiles.forEach((file, index) => {
      const chip = document.createElement("span");
      chip.className = "chat-file-chip";
      const title = document.createElement("span");
      title.textContent = file.name + " (" + Math.ceil(file.size / 1024) + " KB)";
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "✕";
      remove.addEventListener("click", () => {
        selectedFiles.splice(index, 1);
        renderSelectedFiles();
      });
      chip.append(title, remove);
      selectedList.appendChild(chip);
    });
  }

  fileInput.addEventListener("change", () => {
    const incoming = Array.from(fileInput.files || []);
    if (selectedFiles.length + incoming.length > maxFiles ||
        incoming.some(file => !supported.test(file.name) || file.size <= 0 ||
                              file.size > 12 * 1024 * 1024) ||
        selectedFiles.reduce((n, file) => n + file.size, 0) +
          incoming.reduce((n, file) => n + file.size, 0) > 40 * 1024 * 1024) {
      chatNotice.textContent = "Tối đa 10 tệp, 12 MB/tệp, tổng 40 MB; chỉ hỗ trợ ảnh và tài liệu thông dụng.";
      chatNotice.className = "message error";
    } else {
      selectedFiles.push(...incoming);
      renderSelectedFiles();
    }
    fileInput.value = "";
  });
  const threadSelect = el("chatThreads");
  const accountSelect = el("chatAccount");
  const sessionInfo = el("chatSession");
  const syncButton = el("syncChat");
  const syncSessionInput = el("syncSessionId");
  const syncNotice = el("chatSyncNotice");
  let backendReady = false;
  let writeChain = Promise.resolve();
  let knownAccounts = [];
  const title = el("workspaceTitle");
  let threads = [];
  let activeId = null;
  let busy = false;

  function makeId() {
    return typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : String(Date.now()) + "-" + Math.random().toString(36).slice(2);
  }

  function load() {
    try {
      const saved = JSON.parse(localStorage.getItem(KEY) || "{}");
      if (Array.isArray(saved.threads)) {
        threads = saved.threads.filter((thread) =>
          thread && typeof thread.id === "string" &&
          Array.isArray(thread.messages)
        );
      }
      if (typeof saved.activeId === "string") activeId = saved.activeId;
    } catch (_) {
      threads = [];
    }
    if (!threads.some((thread) => thread.id === activeId)) {
      activeId = threads[0]?.id || null;
    }
  }

  async function saveSnapshot(thread) {
    if (!thread?.accountId) return;
    const response = await fetch("/api/chat/threads/" + encodeURIComponent(thread.id), {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        accountId: thread.accountId, sessionId: thread.sessionId || null,
        name: thread.name, messages: thread.messages
      })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "Không thể lưu SQLite");
    return data.thread;
  }

  function queueServerSave(thread) {
    if (!backendReady || !thread?.accountId) return Promise.resolve();
    const snapshot = JSON.parse(JSON.stringify(thread));
    writeChain = writeChain.catch(() => {}).then(() => saveSnapshot(snapshot));
    writeChain.catch(err => {
      syncNotice.textContent = "Lưu SQLite thất bại: " + err.message +
        ". Bản dự phòng trong trình duyệt vẫn được giữ.";
    });
    return writeChain;
  }

  function persist(thread = activeThread()) {
    try {
      localStorage.setItem(KEY, JSON.stringify({activeId, threads}));
    } catch (_) {
      chatNotice.textContent = "Không lưu được bản dự phòng trên trình duyệt; kiểm tra SQLite.";
      chatNotice.className = "message error";
    }
    void queueServerSave(thread);
  }

  async function restoreFromServer() {
    try {
      const response = await fetch("/api/chat/threads", {cache: "no-store"});
      if (!response.ok) throw new Error("HTTP " + response.status);
      const data = await response.json();
      if (!Array.isArray(data.threads)) throw new Error("Dữ liệu SQLite không hợp lệ");
      // One-time migration and crash recovery: never clear browser backup.
      for (const local of [...threads]) {
        if (!local.accountId) continue;
        try {
          await saveSnapshot(local);
        } catch (err) {
          syncNotice.textContent = "Chưa chuyển được toàn bộ hội thoại cũ: " + err.message;
        }
      }
      const refreshed = await fetch("/api/chat/threads", {cache: "no-store"});
      if (!refreshed.ok) throw new Error("HTTP " + refreshed.status);
      const result = await refreshed.json();
      if (!Array.isArray(result.threads)) throw new Error("Dữ liệu SQLite không hợp lệ");
      const seen = new Set(result.threads.map(t => t.id));
      const previousActive = activeThread();
      threads = [...result.threads, ...threads.filter(t => !seen.has(t.id))];
      // Prefer an existing saved conversation over an empty placeholder that
      // the account refresh may have created while SQLite was still loading.
      if ((!previousActive || (!previousActive.sessionId &&
           !(previousActive.messages || []).length)) && accountSelect.value) {
        const restored = result.threads.find(t => t.accountId === accountSelect.value &&
          Array.isArray(t.messages) && t.messages.length);
        if (restored) activeId = restored.id;
      }
      backendReady = true;
      // Avoid triggering an unnecessary server write when just restoring UI.
      try { localStorage.setItem(KEY, JSON.stringify({activeId, threads})); } catch (_) {}
      render();
      for (const thread of threads) {
        for (const message of thread.messages || []) {
          if (message.mediaJobId && message.mediaStatus === "watching") {
            void followMedia(thread, message);
          }
        }
      }
    } catch (err) {
      syncNotice.textContent = "Chưa kết nối được kho lịch sử SQLite: " + err.message;
      // Retain local cache, and do not claim durable storage succeeded.
    }
  }

  function activeThread() {
    return threads.find((thread) => thread.id === activeId) || null;
  }

  function createThread(accountId = accountSelect.value) {
    if (!accountId) {
      chatNotice.textContent = "Hãy chọn tài khoản Muse trước khi tạo cuộc hội thoại.";
      chatNotice.className = "message error";
      return null;
    }
    const thread = {
      id: makeId(),
      name: "Cuộc trò chuyện mới",
      sessionId: null,
      accountId,
      messages: [],
      updatedAt: Date.now()
    };
    threads.unshift(thread);
    activeId = thread.id;
    persist();
    render();
    chatInput.focus();
    return thread;
  }

  function render() {
    const accountId = accountSelect.value;
    const matching = accountId
      ? threads.filter(thread => thread.accountId === accountId)
      : [];
    if (!matching.some(thread => thread.id === activeId)) {
      activeId = matching[0]?.id || null;
    }
    threadSelect.replaceChildren();
    for (const thread of matching) {
      const option = document.createElement("option");
      option.value = thread.id;
      option.textContent = thread.name || "Cuộc trò chuyện";
      threadSelect.appendChild(option);
    }
    if (activeId) threadSelect.value = activeId;
    const thread = activeThread();
    sessionInfo.textContent = thread?.sessionId
      ? "Muse session: " + thread.sessionId
      : "Chưa có session · Lượt đầu sẽ tạo session mới";
    messageBox.replaceChildren();
    if (!thread || thread.messages.length === 0) {
      const empty = document.createElement("div");
      empty.className = "chat-empty";
      const heading = document.createElement("strong");
      heading.textContent = "Gửi tin nhắn bình thường tới Muse";
      const hint = document.createElement("span");
      hint.textContent = "Bạn có thể hỏi tiếp nhiều lượt trong cùng một session.";
      empty.append(heading, hint);
      messageBox.appendChild(empty);
    } else {
      for (const message of thread.messages) {
        const wrapper = document.createElement("div");
        wrapper.className = "chat-bubble " +
          (message.role === "assistant" ? "assistant" : "user");
        const label = document.createElement("div");
        label.className = "chat-bubble-label";
        label.textContent = message.role === "assistant" ? "Muse" : "Bạn";
        const body = document.createElement("div");
        body.className = "chat-bubble-text";
        body.textContent = message.text || "";
        wrapper.append(label, body);
        if (message.role === "assistant" && message.replyFinished === false) {
          const note = document.createElement("div");
          note.className = "chat-media-status";
          note.textContent = "Phản hồi tạm thời · đang kiểm tra tín hiệu hoàn tất từ Muse";
          wrapper.appendChild(note);
        }
        if (Array.isArray(message.uploads) && message.uploads.length) {
          const uploads = document.createElement("div");
          uploads.className = "chat-selected-files";
          for (const file of message.uploads) {
            const chip = document.createElement("span");
            chip.className = "chat-file-chip";
            if (file.url && /^\/api\/chat\/uploads\/[\w-]+\/[a-f0-9]{32}$/.test(file.url)) {
              const a = document.createElement("a");
              a.href = file.url;
              a.textContent = "📎 " + file.name;
              a.target = "_blank";
              a.rel = "noopener";
              chip.appendChild(a);
            } else {
              chip.textContent = "📎 " + file.name;
            }
            uploads.appendChild(chip);
          }
          wrapper.appendChild(uploads);
        }
        if (Array.isArray(message.files) && message.files.length) {
          const gallery = document.createElement("div");
          gallery.className = "chat-attachments";
          for (const file of message.files) {
            if (!file || typeof file.url !== "string" ||
                !/^\/api\/chat\/media\/[a-f0-9]+\/files\/[\w.\-]+$/.test(file.url)) {
              continue;
            }
            const item = document.createElement("div");
            item.className = "chat-attachment";
            if (file.kind === "image") {
              const img = document.createElement("img");
              img.src = file.url;
              img.alt = file.filename || "Ảnh từ Muse";
              img.loading = "lazy";
              item.appendChild(img);
            } else if (file.kind === "video") {
              const video = document.createElement("video");
              video.src = file.url;
              video.preload = "metadata";
              video.controls = true;
              item.appendChild(video);
            }
            const link = document.createElement("a");
            link.href = file.url + "?download=1";
            link.textContent = "↓ Tải " + (file.filename || "tệp");
            link.download = file.filename || "";
            item.appendChild(link);
            gallery.appendChild(item);
          }
          if (gallery.childNodes.length) wrapper.appendChild(gallery);
        }
        if (message.mediaStatus === "watching" && message.mediaExpected) {
          const status = document.createElement("p");
          status.className = "chat-media-status";
          status.textContent = "Đang kiểm tra tệp đính kèm từ Muse…";
          wrapper.appendChild(status);
        } else if (message.mediaStatus === "reply_unconfirmed") {
          const note = document.createElement("p");
          note.className = "chat-media-status";
          note.textContent = "Chưa xác minh được phản hồi cuối. Có thể dùng Đồng bộ lịch sử từ Muse.";
          wrapper.appendChild(note);
        } else if (message.mediaStatus === "no_media" || message.mediaStatus === "failed") {
          const warning = document.createElement("p");
          warning.className = "chat-media-warning";
          warning.textContent = (message.mediaErrors || []).join(" ") ||
            "Muse chưa trả về tệp. Kiểm tra task trước khi gửi lại.";
          wrapper.appendChild(warning);
        } else if (Array.isArray(message.mediaErrors) && message.mediaErrors.length) {
          const warning = document.createElement("p");
          warning.className = "chat-media-warning";
          warning.textContent = message.mediaErrors.join(" ");
          wrapper.appendChild(warning);
        }
        messageBox.appendChild(wrapper);
      }
    }
    if (busy && activeId === pendingThreadId) {
      const typing = document.createElement("div");
      typing.className = "chat-bubble assistant chat-typing";
      typing.textContent = "Muse đang trả lời…";
      messageBox.appendChild(typing);
    }
    messageBox.scrollTop = messageBox.scrollHeight;
  }

  let pendingThreadId = null;
  const activeMediaPolls = new Set();

  async function followMedia(thread, reply) {
    const jobId = reply.mediaJobId;
    if (!jobId || activeMediaPolls.has(jobId)) return;
    activeMediaPolls.add(jobId);
    let failures = 0;
    try {
      while (reply.mediaStatus === "watching") {
        const response = await fetch("/api/chat/media/" + encodeURIComponent(jobId), {
          cache: "no-store"
        });
        if (!response.ok) {
          failures++;
          if (failures >= 3) throw new Error("Không kiểm tra được tệp từ Muse.");
        } else {
          failures = 0;
          const data = await response.json();
          reply.files = Array.isArray(data.files) ? data.files : [];
          reply.mediaErrors = Array.isArray(data.errors) ? data.errors : [];
          reply.mediaStatus = data.status || "watching";
          reply.mediaErrorCode = data.error_code || null;
          reply.mediaMuseText = data.muse_message || null;
          if (typeof data.muse_message === "string" &&
              data.muse_message.startsWith(reply.text || "")) {
            reply.text = data.muse_message;
          }
          if (data.reply_finished === true) reply.replyFinished = true;
          if (activeId === thread.id &&
              data.error_code === "muse_finished_without_video") {
            chatNotice.className = "message error";
            chatNotice.textContent =
              "Muse đã hoàn tất trả lời nhưng không tạo video. Phản hồi Muse: " +
              (data.muse_message || "Không có nội dung");
          }
          persist(thread);
          if (activeId === thread.id) render();
        }
        if (reply.mediaStatus !== "watching") break;
        await new Promise((resolve) => setTimeout(resolve, 3000));
      }
    } catch (err) {
      reply.mediaStatus = "failed";
      reply.mediaErrors = [String(err.message || err)];
      persist(thread);
      if (activeId === thread.id) render();
    } finally {
      activeMediaPolls.delete(jobId);
    }
  }

  function setMode(mode) {
    const chat = mode === "chat";
    chatMode.classList.toggle("hidden", !chat);
    videoMode.classList.toggle("hidden", chat);
    jobsPanel.classList.toggle("hidden", chat);
    title.textContent = chat ? "Chat với Muse" : "Tạo video";
    for (const button of buttons) {
      const selected = button.dataset.mode === mode;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", selected ? "true" : "false");
    }
    if (chat) {
      render();
      chatInput.focus();
    }
  }

  async function sendMessage() {
    const message = chatInput.value.trim() ||
      (selectedFiles.length ? "Hãy xem các tệp đính kèm." : "");
    if (!message || busy) return;
    const sendingFiles = selectedFiles.slice();
    if (!activeThread() || activeThread().accountId !== accountSelect.value) {
      createThread(accountSelect.value);
    }
    const thread = activeThread();
    if (!thread || thread.accountId !== accountSelect.value) {
      chatNotice.textContent = "Hãy chọn tài khoản Muse để gửi tin nhắn.";
      chatNotice.className = "message error";
      return;
    }
    if (!thread.accountId && thread.sessionId) {
      chatNotice.textContent = "Hội thoại cũ không gắn tài khoản. Hãy tạo Chat mới và chọn tài khoản Muse.";
      chatNotice.className = "message error";
      return;
    }
    if (!thread.accountId) thread.accountId = accountSelect.value || null;
    if (!thread.accountId ||
        !knownAccounts.some(a => a.id === thread.accountId && a.enabled && a.status === "ready")) {
      chatNotice.textContent = "Vui lòng chọn tài khoản Muse đang sẵn sàng để chat.";
      chatNotice.className = "message error";
      return;
    }
    const requestedId = thread.id;
    busy = true;
    pendingThreadId = requestedId;
    sendButton.disabled = true;
    chatNotice.textContent = "";
    chatNotice.className = "message";
    const userTurn = {
      role: "user", text: message,
      uploads: sendingFiles.map(file => ({
        name: file.name, size: file.size, kind: /^image\//.test(file.type || "") ? "image" : "document"
      }))
    };
    thread.messages.push(userTurn);
    if (thread.messages.length === 1) {
      thread.name = message.length > 54
        ? message.slice(0, 54) + "…"
        : message;
    }
    thread.updatedAt = Date.now();
    chatInput.value = "";
    persist();
    render();

    try {
      // Durable save must precede network submission. The backend also records
      // both turns so closing the tab cannot silently discard Muse's reply.
      await writeChain.catch(() => {});
      await saveSnapshot(JSON.parse(JSON.stringify(thread)));
      let response;
      if (sendingFiles.length) {
        const payload = new FormData();
        payload.append("message", message);
        payload.append("account_id", thread.accountId);
        payload.append("thread_id", thread.id);
        payload.append("timeout", "120");
        if (thread.sessionId) payload.append("session_id", thread.sessionId);
        sendingFiles.forEach(file => payload.append("files", file, file.name));
        response = await fetch("/api/chat/send-files", {
          method: "POST", body: payload
        });
      } else {
        response = await fetch("/api/chat/send", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            message,
            account_id: thread.accountId,
            session_id: thread.sessionId || null,
            thread_id: thread.id,
            timeout: 120
          })
        });
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(typeof data.detail === "string"
          ? data.detail
          : "Không gửi được tin nhắn (HTTP " + response.status + ")");
      }
      if (typeof data.text !== "string" || !data.text.trim()) {
        throw new Error("Muse chưa trả về nội dung text.");
      }
      thread.sessionId = data.session_id || thread.sessionId;
      if (Array.isArray(data.uploaded_files)) {
        userTurn.uploads = data.uploaded_files;
        selectedFiles = selectedFiles.filter(file => !sendingFiles.includes(file));
        renderSelectedFiles();
      }
      if (data.warning) {
        chatNotice.textContent = data.warning;
        chatNotice.className = "message";
      }
      const reply = {
        role: "assistant",
        text: typeof data.text === "string" ? data.text : "",
        files: Array.isArray(data.attachments) ? data.attachments : [],
        mediaJobId: data.media_job_id || null,
        mediaStatus: data.media_job_id ? "watching" : "completed",
        mediaExpected: Boolean(data.media_expected),
        mediaErrors: [],
        replyFinished: data.reply_finished === true,
        completionSource: data.completion_source || "unknown"
      };
      thread.messages.push(reply);
      thread.updatedAt = Date.now();
      persist(thread);
      if (reply.mediaJobId) void followMedia(thread, reply);
    } catch (err) {
      chatNotice.textContent = err.message +
        " Có thể Muse đã nhận yêu cầu; hãy kiểm tra trước khi gửi lại để tránh lặp.";
      chatNotice.className = "message error";
    } finally {
      busy = false;
      pendingThreadId = null;
      sendButton.disabled = false;
      if (activeId === requestedId) render();
      chatInput.focus();
    }
  }

  async function syncThread(thread, silent = false) {
    if (!thread?.accountId || !thread.sessionId) {
      if (!silent) syncNotice.textContent = "Cần có tài khoản Muse và session_id.";
      return;
    }
    try {
      await writeChain.catch(() => {});
      await saveSnapshot(JSON.parse(JSON.stringify(thread)));
      const response = await fetch(
        "/api/chat/threads/" + encodeURIComponent(thread.id) + "/sync",
        {method: "POST"}
      );
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "Không đồng bộ được từ Muse");
      if (!data.thread || !Array.isArray(data.thread.messages)) {
        throw new Error("Muse không trả về hội thoại hợp lệ");
      }
      Object.assign(thread, data.thread);
      persist(thread);
      if (activeId === thread.id) render();
      if (!silent) syncNotice.textContent =
        "Đã đồng bộ " + data.remote_count + " tin nhắn từ Muse và lưu trong SQLite.";
    } catch (err) {
      if (!silent) syncNotice.textContent = "Đồng bộ thất bại: " + err.message;
    }
  }

  syncButton.addEventListener("click", async () => {
    if (busy || syncButton.disabled) return;
    const accountId = accountSelect.value;
    if (!accountId) {
      syncNotice.textContent = "Hãy chọn tài khoản Muse trước.";
      return;
    }
    let thread = activeThread();
    if (!thread || thread.accountId !== accountId) thread = createThread(accountId);
    if (!thread) return;
    const suppliedId = syncSessionInput.value.trim();
    if (suppliedId && thread.sessionId && suppliedId !== thread.sessionId) {
      syncNotice.textContent =
        "Session ID khác hội thoại hiện tại. Hãy tạo Chat mới trước khi khôi phục.";
      return;
    }
    const sessionId = suppliedId || thread.sessionId;
    if (!sessionId) {
      syncNotice.textContent = "Nhập session_id để khôi phục, hoặc chọn hội thoại đã có.";
      return;
    }
    thread.sessionId = sessionId;
    syncButton.disabled = true;
    syncNotice.textContent = "Đang lấy lịch sử từ Muse…";
    try {
      persist(thread);
      await syncThread(thread);
      syncSessionInput.value = "";
    } finally {
      syncButton.disabled = false;
    }
  });

  buttons.forEach((button) => {
    button.addEventListener("click", () => setMode(button.dataset.mode));
  });
  el("newChat").addEventListener("click", () => createThread(accountSelect.value));
  threadSelect.addEventListener("change", () => {
    activeId = threadSelect.value;
    persist();
    render();
  });
  accountSelect.addEventListener("change", () => {
    const accountId = accountSelect.value;
    if (!accountId) {
      activeId = null;
      persist();
      render();
      return;
    }
    const recent = threads.find(thread => thread.accountId === accountId);
    if (recent) {
      activeId = recent.id;
      persist();
      render();
    } else {
      createThread(accountId);
    }
  });
  window.addEventListener("muse-accounts-updated", (event) => {
    knownAccounts = event.detail || [];
    const previous = accountSelect.value;
    accountSelect.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Chọn tài khoản Muse";
    accountSelect.appendChild(placeholder);
    for (const account of knownAccounts) {
      if (account.enabled && account.status === "ready") {
        const option = document.createElement("option");
        option.value = account.id;
        option.textContent = account.label || account.email || account.id;
        accountSelect.appendChild(option);
      }
    }
    const readyIds = knownAccounts
      .filter(account => account.enabled && account.status === "ready")
      .map(account => account.id);
    const lastThreadAccount = activeThread()?.accountId;
    const selectedId = readyIds.includes(previous)
      ? previous
      : readyIds.includes(lastThreadAccount)
        ? lastThreadAccount
        : readyIds[0] || "";
    accountSelect.value = selectedId;
    if (!selectedId) {
      activeId = null;
      render();
    } else {
      const current = activeThread();
      if (!current || current.accountId !== selectedId) {
        const recent = threads.find(thread => thread.accountId === selectedId);
        if (recent) {
          activeId = recent.id;
          render();
        } else {
          createThread(selectedId);
        }
      } else {
        render();
      }
    }
    persist();
  });
  sendButton.addEventListener("click", sendMessage);
  chatInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendMessage();
    }
  });
  load();
  if (!activeThread()) createThread();
  void restoreFromServer();
  setMode("video");
  for (const thread of threads) {
    for (const message of thread.messages) {
      if (message.mediaJobId && message.mediaStatus === "watching") {
        void followMedia(thread, message);
      }
    }
  }
})();
