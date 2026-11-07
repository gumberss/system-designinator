const authScreen = document.querySelector("#auth-screen");
const appScreen = document.querySelector("#app-screen");
const authForm = document.querySelector("#auth-form");
const authError = document.querySelector("#auth-error");
const authTitle = document.querySelector("#auth-title");
const authSubtitle = document.querySelector("#auth-subtitle");
const authSubmit = document.querySelector("#auth-submit");
const authSwitchPrompt = document.querySelector("#auth-switch-prompt");
const authSwitch = document.querySelector("#auth-switch");
const conversationList = document.querySelector("#conversation-list");
const messageList = document.querySelector("#message-list");
const messageInput = document.querySelector("#message-input");
const sendButton = document.querySelector("#send-button");
const welcome = document.querySelector("#welcome");
const chatFooter = document.querySelector("#chat-footer");
const shareButton = document.querySelector("#share-chat");
const toast = document.querySelector("#toast");
const fileInput = document.querySelector("#file-input");
const attachmentList = document.querySelector("#attachment-list");

let isRegister = false;
let currentUser = null;
let currentConversationId = null;
let isSharedView = false;
let shareEnabledForCurrentConversation = false;
let attachedFiles = [];
let toastTimer;

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers
    }
  });

  if (response.status === 204) return null;
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = body.detail || body.error || body.title || "Something went wrong.";
    throw new Error(message);
  }
  return body;
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("visible"), 2800);
}

function setAuthMode(register) {
  isRegister = register;
  authTitle.textContent = register ? "Create your account" : "Welcome back";
  authSubtitle.textContent = register
    ? "Your local workspace is ready when you are."
    : "Sign in to pick up where you left off.";
  authSubmit.textContent = register ? "Create account" : "Sign in";
  authSwitchPrompt.textContent = register ? "Already have an account?" : "New here?";
  authSwitch.textContent = register ? "Sign in" : "Create an account";
  document.querySelector("#password").autocomplete = register ? "new-password" : "current-password";
  authError.textContent = "";
}

function showAuth() {
  authScreen.hidden = false;
  appScreen.hidden = true;
}

function showApp() {
  authScreen.hidden = true;
  appScreen.hidden = false;
}

function renderConversations(conversations) {
  conversationList.replaceChildren();
  for (const conversation of conversations) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `conversation-item${conversation.id === currentConversationId ? " active" : ""}`;
    button.addEventListener("click", () => selectConversation(conversation.id));

    const icon = document.createElement("span");
    icon.className = "item-icon";
    icon.textContent = "◷";
    const label = document.createElement("span");
    label.className = "item-label";
    label.textContent = conversation.title;
    button.append(icon, label);
    conversationList.append(button);
  }
}

function renderMessages(messages) {
  messageList.replaceChildren();
  for (const message of messages) appendMessage(message);
  messageList.scrollTop = messageList.scrollHeight;
  welcome.hidden = messages.length > 0;
  messageList.hidden = messages.length === 0;
}

function appendMessage(message) {
  const row = document.createElement("article");
  row.className = `message ${message.role}`;
  const avatar = document.createElement("div");
  avatar.className = "message-avatar";
  avatar.textContent = message.role === "user" ? "Y" : "✳";
  const content = document.createElement("div");
  content.className = "message-content";
  const meta = document.createElement("div");
  meta.className = "message-meta";
  meta.textContent = message.role === "user" ? "You" : "ai-chatinator · Local demo";
  const text = document.createElement("div");
  text.className = "message-text";
  text.textContent = message.content;
  content.append(meta, text);
  row.append(avatar, content);
  messageList.append(row);
  messageList.hidden = false;
  welcome.hidden = true;
  messageList.scrollTop = messageList.scrollHeight;
}

async function loadProfile() {
  currentUser = await api("/api/me");
  document.querySelector("#credit-count").textContent = currentUser.credits.toLocaleString();
  document.querySelector("#profile-email").textContent = currentUser.email;
  document.querySelector("#profile-avatar").textContent = currentUser.email[0].toUpperCase();
}

async function loadConversations() {
  const conversations = await api("/api/conversations");
  renderConversations(conversations);
  if (!currentConversationId && conversations.length) {
    await selectConversation(conversations[0].id);
  }
}

async function selectConversation(conversationId) {
  currentConversationId = conversationId;
  attachedFiles = [];
  renderAttachments();
  isSharedView = false;
  shareEnabledForCurrentConversation = false;
  shareButton.textContent = "↗ Share";
  shareButton.disabled = false;
  chatFooter.hidden = false;
  renderConversations(await api("/api/conversations"));
  const messages = await api(`/api/conversations/${encodeURIComponent(conversationId)}/messages`);
  renderMessages(messages);
  document.querySelector("#sidebar").classList.remove("open");
}

async function startConversation() {
  const conversation = await api("/api/conversations", { method: "POST" });
  currentConversationId = conversation.id;
  shareEnabledForCurrentConversation = false;
  attachedFiles = [];
  renderAttachments();
  shareButton.textContent = "↗ Share";
  renderMessages([]);
  renderConversations(await api("/api/conversations"));
  shareButton.disabled = false;
  messageInput.focus();
}

function showTyping() {
  const row = document.createElement("article");
  row.className = "message";
  row.id = "typing-indicator";
  const avatar = document.createElement("div");
  avatar.className = "message-avatar";
  avatar.textContent = "✳";
  const content = document.createElement("div");
  content.className = "message-content";
  const meta = document.createElement("div");
  meta.className = "message-meta";
  meta.textContent = "ai-chatinator · Local demo";
  const dots = document.createElement("div");
  dots.className = "typing";
  dots.innerHTML = "<i></i><i></i><i></i>";
  content.append(meta, dots);
  row.append(avatar, content);
  messageList.append(row);
  messageList.scrollTop = messageList.scrollHeight;
}

async function sendMessage(content) {
  if (!content.trim() || isSharedView) return;
  if (!currentConversationId) await startConversation();
  messageInput.value = "";
  messageInput.style.height = "30px";
  sendButton.disabled = true;
  showTyping();
  try {
    const result = await api(`/api/conversations/${encodeURIComponent(currentConversationId)}/messages`, {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({
        content: content.trim(),
        fileIds: attachedFiles.map(file => file.id)
      })
    });
    document.querySelector("#typing-indicator")?.remove();
    renderMessages(await api(`/api/conversations/${encodeURIComponent(currentConversationId)}/messages`));
    document.querySelector("#credit-count").textContent = result.creditsRemaining.toLocaleString();
    renderConversations(await api("/api/conversations"));
    attachedFiles = [];
    renderAttachments();
  } catch (error) {
    document.querySelector("#typing-indicator")?.remove();
    showToast(error.message);
    messageInput.value = content;
  } finally {
    sendButton.disabled = !messageInput.value.trim();
    messageInput.focus();
  }
}

function renderAttachments() {
  attachmentList.replaceChildren();
  for (const file of attachedFiles) {
    const item = document.createElement("span");
    item.className = "attachment-chip";
    item.textContent = `${file.fileName} · ${Math.ceil(file.sizeBytes / 1024)} KB`;
    attachmentList.append(item);
  }
}

async function uploadAttachment(file) {
  if (!currentConversationId) await startConversation();
  const response = await fetch(
    `/api/conversations/${encodeURIComponent(currentConversationId)}/files`,
    {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": file.type || "application/octet-stream",
        "X-File-Name": encodeURIComponent(file.name)
      },
      body: file
    });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || body.error || body.title || "File upload failed.");
  }

  attachedFiles.push(body);
  renderAttachments();
}

async function initializeSharedView(token) {
  isSharedView = true;
  showApp();
  document.querySelector("#sidebar").hidden = true;
  document.querySelector(".topbar .model-badge").textContent = "Shared conversation · Read only";
  document.querySelector(".topbar-actions").hidden = true;
  chatFooter.hidden = true;
  const shared = await api(`/api/shared/${encodeURIComponent(token)}`);
  const banner = document.createElement("div");
  banner.className = "shared-banner";
  banner.textContent = "Read-only shared conversation";
  welcome.hidden = true;
  messageList.before(banner);
  renderMessages(shared.messages);
  const title = document.createElement("p");
  title.className = "muted";
  title.textContent = shared.title;
  messageList.prepend(title);
}

authSwitch.addEventListener("click", () => setAuthMode(!isRegister));
authForm.addEventListener("submit", async event => {
  event.preventDefault();
  authError.textContent = "";
  const form = new FormData(authForm);
  try {
    await api(`/api/auth/${isRegister ? "register" : "login"}`, {
      method: "POST",
      body: JSON.stringify({ email: form.get("email"), password: form.get("password") })
    });
    await loadProfile();
    showApp();
    await loadConversations();
  } catch (error) {
    authError.textContent = error.message;
  }
});

document.querySelector("#new-chat").addEventListener("click", () => startConversation().catch(error => showToast(error.message)));
document.querySelector("#composer").addEventListener("submit", event => {
  event.preventDefault();
  sendMessage(messageInput.value);
});
fileInput.addEventListener("change", async () => {
  const files = Array.from(fileInput.files || []);
  fileInput.value = "";
  for (const file of files) {
    try {
      await uploadAttachment(file);
    } catch (error) {
      showToast(error.message);
    }
  }
});
messageInput.addEventListener("input", () => {
  messageInput.style.height = "30px";
  messageInput.style.height = `${Math.min(messageInput.scrollHeight, 180)}px`;
  sendButton.disabled = !messageInput.value.trim();
});
messageInput.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    if (!sendButton.disabled) sendMessage(messageInput.value);
  }
});
document.querySelectorAll(".suggestion").forEach(button => {
  button.addEventListener("click", () => {
    if (!currentConversationId) startConversation().then(() => sendMessage(button.dataset.prompt)).catch(error => showToast(error.message));
    else sendMessage(button.dataset.prompt);
  });
});
document.querySelector("#logout").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" });
  currentUser = null;
  currentConversationId = null;
  showAuth();
});
document.querySelector("#mobile-menu").addEventListener("click", () => {
  document.querySelector("#sidebar").classList.toggle("open");
});
shareButton.addEventListener("click", async () => {
  if (!currentConversationId) return;
  try {
    if (shareEnabledForCurrentConversation) {
      await api(`/api/conversations/${encodeURIComponent(currentConversationId)}/share`, { method: "DELETE" });
      shareEnabledForCurrentConversation = false;
      shareButton.textContent = "↗ Share";
      showToast("The read-only share link has been revoked.");
      return;
    }
    const shared = await api(`/api/conversations/${encodeURIComponent(currentConversationId)}/share`, { method: "POST" });
    shareEnabledForCurrentConversation = true;
    shareButton.textContent = "Revoke link";
    try {
      await navigator.clipboard.writeText(shared.url);
      showToast("Read-only share link copied. Select Revoke link to disable it.");
    } catch {
      showToast(`Share link: ${shared.url}`);
    }
  } catch (error) {
    showToast(error.message);
  }
});

async function boot() {
  const sharedMatch = location.pathname.match(/^\/shared\/([^/]+)$/);
  if (sharedMatch) {
    try {
      await initializeSharedView(sharedMatch[1]);
    } catch (error) {
      showAuth();
      authError.textContent = error.message;
    }
    return;
  }

  try {
    await loadProfile();
    showApp();
    await loadConversations();
  } catch {
    showAuth();
  }
}

boot();
