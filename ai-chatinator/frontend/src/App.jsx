import { useEffect, useRef, useState } from "react";

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body && !(options.body instanceof FormData)
        ? { "Content-Type": "application/json" }
        : {}),
      ...options.headers
    }
  });

  if (response.status === 204) return null;
  const text = await response.text();
  let body = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      if (response.ok) throw new Error("The server returned an invalid response.");
    }
  }
  if (!response.ok) {
    const validation = body?.errors && Object.values(body.errors).flat().join(" ");
    const error = new Error(
      validation || body?.detail || body?.error || body?.title || `Request failed (${response.status}).`
    );
    error.status = response.status;
    throw error;
  }
  return body;
}

function Brand() {
  return (
    <a className="brand" href="/" aria-label="ai-chatinator home">
      <span className="brand-mark">✳</span> ai-chatinator
    </a>
  );
}

export default function App() {
  const [user, setUser] = useState(null);
  const [authMode, setAuthMode] = useState("login");
  const [authError, setAuthError] = useState("");
  const [conversations, setConversations] = useState([]);
  const [conversationId, setConversationId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState([]);
  const [sending, setSending] = useState(false);
  const [shared, setShared] = useState(null);
  const [shareEnabled, setShareEnabled] = useState(false);
  const [toast, setToast] = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [provider, setProvider] = useState("Local demo");
  const fileInput = useRef(null);
  const messageList = useRef(null);
  const toastTimer = useRef(null);

  useEffect(() => {
    let active = true;
    const sharedRoute = window.location.pathname.match(/^\/shared\/([^/]+)$/);

    async function initialize() {
      try {
        if (sharedRoute) {
          const sharedConversation = await api(`/api/shared/${encodeURIComponent(sharedRoute[1])}`);
          if (active) setShared(sharedConversation);
          return;
        }

        const profile = await api("/api/me");
        const items = await api("/api/conversations");
        if (!active) return;
        setUser(profile);
        setConversations(items);
        if (items.length) {
          const first = items[0];
          const history = await api(`/api/conversations/${encodeURIComponent(first.id)}/messages`);
          if (!active) return;
          setConversationId(first.id);
          setMessages(history);
        }
      } catch (error) {
        if (!sharedRoute && error.status === 401) {
          if (active) setUser(null);
          return;
        }
        throw error;
      }
    }

    initialize().catch(error => {
      if (active) setAuthError(error.message);
    });
    return () => {
      active = false;
      clearTimeout(toastTimer.current);
    };
  }, []);

  useEffect(() => {
    if (messageList.current) messageList.current.scrollTop = messageList.current.scrollHeight;
  }, [messages, sending]);

  function showToast(message) {
    setToast(message);
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(""), 2800);
  }

  async function refreshConversations() {
    const items = await api("/api/conversations");
    setConversations(items);
    return items;
  }

  async function loadProfileAndConversations() {
    const profile = await api("/api/me");
    setUser(profile);
    const items = await refreshConversations();
    if (items.length) {
      setConversationId(items[0].id);
      setMessages(await api(`/api/conversations/${encodeURIComponent(items[0].id)}/messages`));
    }
  }

  async function selectConversation(id) {
    setConversationId(id);
    setAttachments([]);
    setShareEnabled(false);
    setSidebarOpen(false);
    setMessages(await api(`/api/conversations/${encodeURIComponent(id)}/messages`));
  }

  async function startConversation() {
    const conversation = await api("/api/conversations", { method: "POST" });
    setConversationId(conversation.id);
    setMessages([]);
    setAttachments([]);
    setShareEnabled(false);
    await refreshConversations();
    return conversation.id;
  }

  async function submitAuth(event) {
    event.preventDefault();
    setAuthError("");
    const form = new FormData(event.currentTarget);
    const endpoint = authMode === "register" ? "register" : "login";
    try {
      await api(`/api/auth/${endpoint}`, {
        method: "POST",
        body: JSON.stringify({ email: form.get("email"), password: form.get("password") })
      });
      await loadProfileAndConversations();
    } catch (error) {
      setAuthError(error.message);
    }
  }

  async function logout() {
    try {
      await api("/api/auth/logout", { method: "POST" });
      setUser(null);
      setConversationId(null);
      setConversations([]);
      setMessages([]);
      setAttachments([]);
    } catch (error) {
      showToast(error.message);
    }
  }

  async function uploadFiles(fileList) {
    for (const file of Array.from(fileList)) {
      try {
        const targetId = conversationId || await startConversation();
        const uploaded = await api(`/api/conversations/${encodeURIComponent(targetId)}/files`, {
          method: "POST",
          headers: {
            "Content-Type": file.type || "application/octet-stream",
            "X-File-Name": encodeURIComponent(file.name)
          },
          body: file
        });
        setAttachments(current => [...current, uploaded]);
      } catch (error) {
        showToast(error.message);
      }
    }
  }

  async function sendMessage(content) {
    if (!content.trim() || sending || shared) return;
    setSending(true);
    setInput("");
    const previousMessages = messages;
    try {
      const targetId = conversationId || await startConversation();
      const requestContent = content.trim();
      const idempotencyKey = crypto.randomUUID();
      setConversationId(targetId);
      setMessages(current => [...current, {
        id: `pending-${idempotencyKey}`,
        role: "user",
        content: requestContent
      }]);

      const response = await fetch(
        `/api/conversations/${encodeURIComponent(targetId)}/messages/stream`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotencyKey
          },
          body: JSON.stringify({
            content: requestContent,
            fileIds: attachments.map(file => file.id)
          })
        }
      );
      if (!response.ok || !response.body) {
        const text = await response.text();
        let body = null;
        try {
          body = text ? JSON.parse(text) : null;
        } catch {
          if (response.ok) throw new Error("The server returned an invalid response.");
        }
        const validation = body?.errors && Object.values(body.errors).flat().join(" ");
        throw new Error(
          validation || body?.detail || body?.error || body?.title || `Request failed (${response.status}).`
        );
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let completedResult = null;
      const assistantId = `assistant-${idempotencyKey}`;
      const consumeFrame = frame => {
        let eventName = "message";
        const data = [];
        for (const line of frame.split(/\r?\n/)) {
          if (line.startsWith("event:")) eventName = line.slice(6).trim();
          if (line.startsWith("data:")) data.push(line.slice(5).trim());
        }
        if (!data.length) return;

        const event = JSON.parse(data.join("\n"));
        if (eventName === "error" || event.type === "error") {
          throw new Error(event.detail || event.title || "The model stream failed.");
        }
        if (eventName === "delta" && event.content) {
          setMessages(current => {
            const last = current.at(-1);
            if (last?.id === assistantId) {
              return [...current.slice(0, -1), { ...last, content: last.content + event.content }];
            }
            return [...current, { id: assistantId, role: "assistant", content: event.content }];
          });
        }
        if (eventName === "complete") completedResult = event.result;
      };

      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        const frames = buffer.split(/\r?\n\r?\n/);
        buffer = frames.pop() || "";
        for (const frame of frames) consumeFrame(frame);
        if (done) break;
      }
      if (buffer.trim()) consumeFrame(buffer);
      if (!completedResult) throw new Error("The model stream ended before completion.");

      const result = completedResult;
      setConversationId(targetId);
      setMessages(await api(`/api/conversations/${encodeURIComponent(targetId)}/messages`));
      setUser(current => current ? { ...current, credits: result.creditsRemaining } : current);
      setProvider(result.provider === "local-demo" ? "Local demo" : result.provider);
      setAttachments([]);
      await refreshConversations();
    } catch (error) {
      setMessages(previousMessages);
      setInput(content);
      showToast(error.message);
    } finally {
      setSending(false);
    }
  }

  async function toggleShare() {
    if (!conversationId) return;
    try {
      if (shareEnabled) {
        await api(`/api/conversations/${encodeURIComponent(conversationId)}/share`, { method: "DELETE" });
        setShareEnabled(false);
        showToast("The read-only share link has been revoked.");
        return;
      }

      const link = await api(`/api/conversations/${encodeURIComponent(conversationId)}/share`, { method: "POST" });
      setShareEnabled(true);
      try {
        await navigator.clipboard.writeText(link.url);
        showToast("Read-only share link copied. Select Revoke link to disable it.");
      } catch {
        showToast(`Share link: ${link.url}`);
      }
    } catch (error) {
      showToast(error.message);
    }
  }

  if (shared) {
    return (
      <main className="app-shell">
        <section className="chat-panel">
          <header className="topbar">
            <Brand />
            <div className="model-badge">Shared conversation · Read only</div>
          </header>
          <div className="shared-banner">Read-only shared conversation</div>
          <section className="message-list" aria-live="polite">
            <p className="muted">{shared.title}</p>
            {shared.messages.map(message => <Message key={message.id} message={message} />)}
          </section>
        </section>
      </main>
    );
  }

  if (!user) {
    const registering = authMode === "register";
    return (
      <main className="auth-screen">
        <section className="auth-card">
          <Brand />
          <p className="eyebrow">YOUR PRIVATE WORKSPACE</p>
          <h1>{registering ? "Create your account" : "Welcome back"}</h1>
          <p className="muted">
            {registering ? "Your local workspace is ready when you are." : "Sign in to pick up where you left off."}
          </p>
          <form onSubmit={submitAuth}>
            <label htmlFor="email">Email</label>
            <input id="email" name="email" type="email" autoComplete="email" required maxLength={254} />
            <label htmlFor="password">Password</label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete={registering ? "new-password" : "current-password"}
              minLength={10}
              maxLength={128}
              required
            />
            <button className="primary-button wide" type="submit">
              {registering ? "Create account" : "Sign in"}
            </button>
          </form>
          <p className="auth-switch">
            {registering ? "Already have an account?" : "New here?"}{" "}
            <button className="text-button" type="button" onClick={() => {
              setAuthMode(registering ? "login" : "register");
              setAuthError("");
            }}>
              {registering ? "Sign in" : "Create an account"}
            </button>
          </p>
          <p className="error-text" role="alert">{authError}</p>
          <p className="local-note">Local account · Data stays on this machine</p>
        </section>
      </main>
    );
  }

  const onComposerSubmit = event => {
    event.preventDefault();
    sendMessage(input);
  };

  return (
    <main className="app-shell">
      <aside className={`sidebar${sidebarOpen ? " open" : ""}`}>
        <div className="sidebar-top">
          <Brand />
          <button className="new-chat-button" type="button" onClick={() => startConversation().catch(error => showToast(error.message))}>
            <span>＋</span> New chat
          </button>
        </div>
        <div className="history-heading">YOUR CHATS</div>
        <nav className="conversation-list" aria-label="Conversations">
          {conversations.map(conversation => (
            <button
              key={conversation.id}
              type="button"
              className={`conversation-item${conversation.id === conversationId ? " active" : ""}`}
              onClick={() => selectConversation(conversation.id).catch(error => showToast(error.message))}
            >
              <span className="item-icon">◷</span>
              <span className="item-label">{conversation.title}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="credits-card">
            <span className="credit-dot" />
            <span><strong>{Number(user.credits).toLocaleString()}</strong> local credits</span>
          </div>
          <div className="profile-row">
            <div className="avatar">{user.email?.[0]?.toUpperCase() ?? "A"}</div>
            <div className="profile-copy"><strong>{user.email}</strong><span>Local account</span></div>
            <button className="icon-button" type="button" aria-label="Sign out" title="Sign out" onClick={logout}>↗</button>
          </div>
        </div>
      </aside>

      <section className="chat-panel">
        <header className="topbar">
          <button className="icon-button mobile-menu" type="button" aria-label="Toggle conversation list" onClick={() => setSidebarOpen(open => !open)}>☰</button>
          <div className="model-badge"><span className="model-dot" /> {provider} <span className="chevron">⌄</span></div>
          <div className="topbar-actions">
            <button className="secondary-button" type="button" onClick={toggleShare} disabled={!conversationId}>
              <span>↗</span> {shareEnabled ? "Revoke link" : "Share"}
            </button>
          </div>
        </header>

        {messages.length === 0 ? (
          <section className="welcome">
            <div className="welcome-orb">✳</div>
            <p className="eyebrow">A THOUGHTFUL SPACE TO THINK</p>
            <h1>What’s on your mind?</h1>
            <p className="muted">Start a conversation. Your messages are saved locally on this machine.</p>
            <div className="suggestions">
              <Suggestion title="Untangle a problem" detail="Break a big question into clear steps" prompt="Help me break down a complex problem into smaller steps." onChoose={sendMessage} />
              <Suggestion title="Make a plan" detail="Turn an idea into a practical next step" prompt="Help me draft a concise project plan with milestones." onChoose={sendMessage} />
              <Suggestion title="Learn something" detail="Get a clear, beginner-friendly explanation" prompt="Explain a tricky concept in simple terms." onChoose={sendMessage} />
              <Suggestion title="Find inspiration" detail="Explore a few fresh directions" prompt="Give me a few creative ideas for a weekend project." onChoose={sendMessage} />
            </div>
          </section>
        ) : (
          <section className="message-list" ref={messageList} aria-live="polite">
            {messages.map(message => <Message key={message.id} message={message} />)}
            {sending && messages.at(-1)?.role === "user" && <TypingIndicator />}
          </section>
        )}

        <div className="chat-footer">
          <form className="composer" onSubmit={onComposerSubmit}>
            <div className="attachment-list" aria-live="polite">
              {attachments.map(file => (
                <span className="attachment-chip" key={file.id}>
                  {file.fileName} · {Math.ceil(file.sizeBytes / 1024)} KB
                </span>
              ))}
            </div>
            <textarea
              rows="1"
              maxLength={6000}
              placeholder="Message ai-chatinator…"
              aria-label="Message"
              value={input}
              onChange={event => {
                setInput(event.target.value);
                event.target.style.height = "30px";
                event.target.style.height = `${Math.min(event.target.scrollHeight, 180)}px`;
              }}
              onKeyDown={event => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  if (input.trim()) sendMessage(input);
                }
              }}
            />
            <div className="composer-bottom">
              <span>
                <label className="attach-button" htmlFor="file-input">＋ Attach file</label>
                <input
                  ref={fileInput}
                  id="file-input"
                  type="file"
                  accept=".txt,.md,.pdf,text/plain,text/markdown,application/pdf"
                  multiple
                  onChange={event => {
                    uploadFiles(event.target.files);
                    event.target.value = "";
                  }}
                />
                <span> · Enter to send · Shift + Enter for a new line</span>
              </span>
              <button className="send-button" type="submit" aria-label="Send message" disabled={!input.trim() || sending}>↑</button>
            </div>
          </form>
          <p className="disclaimer">{provider} provider · Responses depend on the configured model provider</p>
        </div>
      </section>
      <div className={`toast${toast ? " visible" : ""}`} role="status">{toast}</div>
    </main>
  );
}

function Message({ message }) {
  return (
    <article className={`message ${message.role}`}>
      <div className="message-avatar">{message.role === "user" ? "Y" : "✳"}</div>
      <div className="message-content">
        <div className="message-meta">{message.role === "user" ? "You" : "ai-chatinator"}</div>
        <div className="message-text">{message.content}</div>
      </div>
    </article>
  );
}

function Suggestion({ title, detail, prompt, onChoose }) {
  return (
    <button className="suggestion" type="button" onClick={() => onChoose(prompt)}>
      <span>↗</span><strong>{title}</strong><small>{detail}</small>
    </button>
  );
}

function TypingIndicator() {
  return (
    <article className="message">
      <div className="message-avatar">✳</div>
      <div className="message-content">
        <div className="message-meta">ai-chatinator</div>
        <div className="typing"><i /><i /><i /></div>
      </div>
    </article>
  );
}
