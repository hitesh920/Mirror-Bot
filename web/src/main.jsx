import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  ChevronRight,
  Cloud,
  Database,
  Download,
  ExternalLink,
  FileSearch,
  Files,
  Gauge,
  HardDrive,
  Home,
  Moon,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Server,
  Settings,
  Square,
  Sun,
  Trash2,
  Upload,
} from "lucide-react";
import "./styles.css";

const NAV_ITEMS = [
  { id: "home", label: "Home", icon: Home },
  { id: "add", label: "Add", icon: Plus },
  { id: "status", label: "Status", icon: Activity },
  { id: "files", label: "Files", icon: Files },
  { id: "drive", label: "Drive", icon: Cloud },
  { id: "jellyfin", label: "Jellyfin", icon: Server },
  { id: "admin", label: "Admin", icon: Settings },
];

const DESTINATIONS = [
  { id: "local", title: "Local", detail: "Save to Movies or Series" },
  { id: "telegram", title: "Telegram", detail: "Upload back to chat" },
  { id: "google_drive", title: "Google Drive", detail: "Upload to configured Drive" },
  { id: "buzzheavier", title: "BuzzHeavier", detail: "Upload to BuzzHeavier" },
];

const CATEGORIES = [
  { id: "movies", title: "Movies", detail: "Organize as a movie" },
  { id: "series", title: "Series", detail: "Organize by show and season" },
];

const VIDEO_QUALITIES = ["360", "480", "720", "1080"];
const AUDIO_QUALITIES = ["64", "128", "192", "256", "320"];

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    throw new Error(await response.text());
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response.text();
}


function formatBytes(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number) || number <= 0) return "-";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let size = number;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size >= 10 || unit === 0 ? size.toFixed(1) : size.toFixed(2)} ${units[unit]}`;
}

function formatNumber(value, suffix = "") {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return `${number.toFixed(number >= 100 ? 0 : 1)}${suffix}`;
}

function appUrl(url) {
  try {
    const parsed = new URL(url, window.location.href);
    const localServicePorts = new Set(["8001", "8002", "8003", "8004", "8005"]);
    const localHostnames = new Set(["127.0.0.1", "localhost"]);
    if (localServicePorts.has(parsed.port) || localHostnames.has(parsed.hostname)) {
      parsed.protocol = window.location.protocol;
      parsed.hostname = window.location.hostname;
    }
    return parsed.toString();
  } catch {
    return url;
  }
}

function detectSource(value) {
  const input = value.trim().toLowerCase();
  if (!input) return { label: "Unknown", detail: "Paste a source to begin." };
  if (input.startsWith("magnet:?")) return { label: "Torrent", detail: "Metadata and file selection will happen before download." };
  if (input.includes("drive.google.com")) return { label: "Google Drive", detail: "Drive links can be downloaded or copied elsewhere." };
  if (input.includes("buzzheavier.com")) return { label: "BuzzHeavier", detail: "BuzzHeavier links are supported directly." };
  if (["youtube.com", "youtu.be", "instagram.com", "tiktok.com", "twitter.com", "x.com"].some((host) => input.includes(host))) {
    return { label: "yt-dlp", detail: "Choose video or audio options below." };
  }
  if (input.startsWith("http://") || input.startsWith("https://")) return { label: "Direct link", detail: "Ready for direct download." };
  return { label: "Unknown", detail: "The backend will validate this when submitted." };
}

function useHashView() {
  const [view, setViewState] = useState(() => {
    const id = window.location.hash.replace("#", "");
    return NAV_ITEMS.some((item) => item.id === id) ? id : "home";
  });

  useEffect(() => {
    const onHash = () => {
      const id = window.location.hash.replace("#", "");
      if (NAV_ITEMS.some((item) => item.id === id)) setViewState(id);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const setView = (id) => {
    setViewState(id);
    window.location.hash = id;
  };

  return [view, setView];
}

function useTheme() {
  const [theme, setTheme] = useState(() => localStorage.getItem("mirror.theme") || "auto");

  useEffect(() => {
    const update = () => {
      const dark = theme === "auto" ? window.matchMedia("(prefers-color-scheme: dark)").matches : theme === "dark";
      document.documentElement.dataset.theme = dark ? "dark" : "light";
    };
    update();
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [theme]);

  const toggle = () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    localStorage.setItem("mirror.theme", next);
    setTheme(next);
  };

  return [document.documentElement.dataset.theme || "dark", toggle];
}

function App() {
  const [view, setView] = useHashView();
  const [theme, toggleTheme] = useTheme();
  const [state, setState] = useState(null);
  const [toast, setToast] = useState("");

  const showToast = (message) => {
    setToast(message);
    window.clearTimeout(window.__mirrorToast);
    window.__mirrorToast = window.setTimeout(() => setToast(""), 2800);
  };

  const refresh = async () => {
    try {
      setState(await api("/api/state"));
    } catch (error) {
      console.error(error);
    }
  };

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 3000);
    return () => window.clearInterval(timer);
  }, []);

  const stats = state?.stats;
  const jellyfinUrl = stats?.jellyfin_url ? appUrl(stats.jellyfin_url) : "";

  const context = {
    state,
    refresh,
    setView,
    showToast,
    jellyfinUrl,
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="top-inner">
          <div className="brand">
            <div className="brand-mark">M</div>
            <div>
              <strong>Mirror-Bot</strong>
              <span>Dashboard</span>
            </div>
          </div>
          <div className="top-actions">
            <StatusPill label={`Jellyfin ${stats?.jellyfin?.health || "unknown"}`} state={stats?.jellyfin?.running ? stats?.jellyfin?.health : "off"} />
            <StatusPill label={stats?.telegram_ui ? "Telegram on" : "Telegram off"} state={stats?.telegram_ui ? "healthy" : "off"} />
            <button className="icon-button" type="button" onClick={toggleTheme} title="Toggle theme">
              {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
            </button>
          </div>
        </div>
        <nav className="nav-tabs">
          {NAV_ITEMS.map((item) => (
            <button key={item.id} className={view === item.id ? "active" : ""} type="button" onClick={() => setView(item.id)}>
              <item.icon size={16} />
              {item.label}
            </button>
          ))}
        </nav>
      </header>

      <main className="page">
        {(view === "home" || view === "status") && <Metrics stats={stats} />}
        {view === "home" && <HomePage {...context} />}
        {view === "add" && <AddPage {...context} />}
        {view === "status" && <StatusPage {...context} />}
        {view === "files" && <FilesPage {...context} />}
        {view === "drive" && <DrivePage {...context} />}
        {view === "jellyfin" && <JellyfinPage {...context} />}
        {view === "admin" && <AdminPage {...context} />}
      </main>

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}

function StatusPill({ label, state }) {
  const className = state === "healthy" ? "ok" : state === "off" ? "off" : "warn";
  return (
    <span className="status-pill">
      <span className={`dot ${className}`} />
      {label}
    </span>
  );
}

function Metrics({ stats }) {
  const items = [
    ["CPU", `${stats?.cpu ?? "-"}%`, Gauge],
    ["RAM", `${stats?.ram ?? "-"}%`, Activity],
    ["Free", stats?.disk_free || "-", HardDrive],
    ["Tasks", String(stats?.tasks ?? 0), Database],
  ];
  return (
    <section className="metrics">
      {items.map(([label, value, Icon]) => (
        <div className="metric" key={label}>
          <span>{label}</span>
          <strong>{value}</strong>
          <Icon size={18} />
        </div>
      ))}
    </section>
  );
}

function PageHeader({ title, subtitle, action }) {
  return (
    <div className="page-header">
      <div>
        <h1>{title}</h1>
        {subtitle && <p>{subtitle}</p>}
      </div>
      {action}
    </div>
  );
}

function HomePage({ state, setView, jellyfinUrl }) {
  const recent = state?.recent?.slice(0, 5) || [];
  return (
    <section className="view-stack">
      <PageHeader title="Overview" subtitle="A focused control room for transfers, storage, media, and cloud tools." />
      <div className="home-grid">
        <div className="panel">
          <div className="panel-title">Quick actions</div>
          <div className="quick-grid">
            <QuickCard title="Add task" detail="Paste a link or upload files" icon={Plus} onClick={() => setView("add")} />
            <QuickCard title="Live status" detail="Watch active and completed work" icon={Activity} onClick={() => setView("status")} />
            <QuickCard title="Files" detail="Open the temporary file explorer" icon={Files} onClick={() => setView("files")} />
            <QuickCard title="Drive" detail="Search, share, delete, and quota" icon={Cloud} onClick={() => setView("drive")} />
            <QuickCard title="Jellyfin" detail="Open and scan your library" icon={Server} href={jellyfinUrl} />
            <QuickCard title="Admin" detail="Logs, restart, and speedtest" icon={Settings} onClick={() => setView("admin")} />
          </div>
        </div>
        <div className="panel">
          <div className="panel-title">Completed tasks</div>
          <TaskList tasks={recent} compact empty="No completed tasks yet" />
        </div>
      </div>
    </section>
  );
}

function QuickCard({ title, detail, icon: Icon, onClick, href }) {
  const content = (
    <>
      <Icon size={20} />
      <strong>{title}</strong>
      <span>{detail}</span>
    </>
  );
  if (href) {
    return (
      <a className="quick-card" href={href} target="_blank" rel="noreferrer">
        {content}
      </a>
    );
  }
  return (
    <button className="quick-card" type="button" onClick={onClick}>
      {content}
    </button>
  );
}

function AddPage({ state, setView, showToast }) {
  const [mode, setMode] = useState("link");
  const [link, setLink] = useState("");
  const [destination, setDestination] = useState("");
  const [category, setCategory] = useState("movies");
  const [name, setName] = useState("");
  const [zip, setZip] = useState(false);
  const [zipPassword, setZipPassword] = useState("");
  const [extract, setExtract] = useState(false);
  const [extractPassword, setExtractPassword] = useState("");
  const [ytdlpKind, setYtdlpKind] = useState("video");
  const [ytdlpQuality, setYtdlpQuality] = useState("1080");
  const [files, setFiles] = useState([]);
  const fileInput = useRef(null);

  const source = useMemo(() => detectSource(link), [link]);
  const telegramEnabled = Boolean(state?.stats?.telegram_ui);
  const availableDestinations = DESTINATIONS.filter((item) => item.id !== "telegram" || telegramEnabled);
  const qualities = ytdlpKind === "audio" ? AUDIO_QUALITIES : VIDEO_QUALITIES;

  useEffect(() => {
    if (!telegramEnabled && destination === "telegram") setDestination("");
  }, [telegramEnabled, destination]);

  useEffect(() => {
    setYtdlpQuality(ytdlpKind === "audio" ? "320" : "1080");
  }, [ytdlpKind]);

  const payload = () => ({
    destination,
    category,
    name,
    zip,
    zip_password: zipPassword,
    extract,
    extract_password: extractPassword,
    ytdlp_kind: ytdlpKind,
    ytdlp_quality: ytdlpQuality,
  });

  const addLink = async () => {
    await api("/api/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ link, ...payload() }),
    });
    setLink("");
    setDestination("");
    showToast("Task added");
    setView("status");
  };

  const uploadFiles = async () => {
    const data = new FormData();
    files.forEach((file) => data.append("file", file));
    const values = payload();
    data.set("destination", values.destination);
    data.set("category", values.category);
    data.set("name", values.name);
    if (values.zip) data.set("zip", "1");
    if (values.extract) data.set("extract", "1");
    if (values.zip_password) data.set("zip_password", values.zip_password);
    if (values.extract_password) data.set("extract_password", values.extract_password);
    await api("/api/upload", { method: "POST", body: data });
    setFiles([]);
    setDestination("");
    if (fileInput.current) fileInput.current.value = "";
    showToast("Upload task added");
    setView("status");
  };

  const submit = async () => {
    try {
      if (!destination) throw new Error("Choose a destination");
      if (mode === "upload") {
        if (!files.length) throw new Error("Choose at least one file");
        await uploadFiles();
      } else {
        if (!link.trim()) throw new Error("Paste a link first");
        await addLink();
      }
    } catch (error) {
      showToast(error.message);
    }
  };

  return (
    <section className="view-stack narrow">
      <PageHeader title="Add anything" subtitle="Paste a link or choose files. The page reveals only the choices that matter." />
      <div className="panel smart-add">
        <Segmented value={mode} onChange={setMode} options={[["link", "Link"], ["upload", "Upload"]]} />

        {mode === "link" ? (
          <div className="source-area">
            <textarea value={link} onChange={(event) => setLink(event.target.value)} placeholder="Paste URL, magnet, Drive link, BuzzHeavier link, or media link" />
            <div className="detected-row">
              <span className="detected-badge">{source.label}</span>
              <span>{source.detail}</span>
            </div>
          </div>
        ) : (
          <div className="upload-card" onClick={() => fileInput.current?.click()} role="button" tabIndex={0}>
            <input ref={fileInput} type="file" multiple onChange={(event) => setFiles(Array.from(event.target.files || []))} />
            <div className="upload-icon"><Upload size={22} /></div>
            <strong>{files.length ? `${files.length} file${files.length === 1 ? "" : "s"} selected` : "Choose files"}</strong>
            <span>{files.length ? files.slice(0, 3).map((file) => file.name).join(", ") : "Drag-style clean picker for browser uploads"}</span>
          </div>
        )}

        <ChoiceSection title="Destination" items={availableDestinations} value={destination} onChange={setDestination} toggleable />

        {destination === "local" && <ChoiceSection title="Local library" items={CATEGORIES} value={category} onChange={setCategory} compact />}

        {mode === "link" && source.label === "yt-dlp" && (
          <div className="option-panel">
            <div className="panel-title small">yt-dlp options</div>
            <Segmented value={ytdlpKind} onChange={setYtdlpKind} options={[["video", "Video"], ["audio", "Audio"]]} />
            <div className="quality-grid">
              {qualities.map((quality) => (
                <button key={quality} type="button" className={ytdlpQuality === quality ? "active" : ""} onClick={() => setYtdlpQuality(quality)}>
                  {ytdlpKind === "audio" ? `${quality} kbps` : `${quality}p`}
                </button>
              ))}
            </div>
            <p className="hint">{ytdlpKind === "audio" ? `MP3 audio at ${ytdlpQuality} kbps.` : `Video up to ${ytdlpQuality}p with audio.`}</p>
          </div>
        )}

        {destination && (
          <details className="processing-options">
            <summary>
              <span>Advanced processing</span>
              <small>Custom name, zip, extract, passwords</small>
            </summary>
            <div className="form-grid">
              <label>
                Custom name
                <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Optional" />
              </label>
              <label>
                Zip password
                <input value={zipPassword} onChange={(event) => setZipPassword(event.target.value)} placeholder="Optional" />
              </label>
              <label>
                Extract password
                <input value={extractPassword} onChange={(event) => setExtractPassword(event.target.value)} placeholder="Optional" />
              </label>
            </div>
            <div className="check-row">
              <label>
                <input type="checkbox" checked={zip} onChange={(event) => { setZip(event.target.checked); if (event.target.checked) setExtract(false); }} />
                Zip after download
              </label>
              <label>
                <input type="checkbox" checked={extract} onChange={(event) => { setExtract(event.target.checked); if (event.target.checked) setZip(false); }} />
                Extract after download
              </label>
            </div>
          </details>
        )}

        <div className="form-actions">
          <button className="secondary" type="button" onClick={() => { setLink(""); setFiles([]); setName(""); setDestination(""); }}>
            Clear
          </button>
          <button type="button" onClick={submit}>
            {mode === "upload" ? "Start upload" : "Start task"}
            <ChevronRight size={17} />
          </button>
        </div>
      </div>
    </section>
  );
}

function Segmented({ value, onChange, options }) {
  return (
    <div className="segmented">
      {options.map(([id, label]) => (
        <button key={id} type="button" className={value === id ? "active" : ""} onClick={() => onChange(id)}>
          {label}
        </button>
      ))}
    </div>
  );
}

function ChoiceSection({ title, items, value, onChange, compact = false, toggleable = false }) {
  return (
    <div>
      <div className="panel-title small">{title}</div>
      <div className={compact ? "choice-grid compact" : "choice-grid"}>
        {items.map((item) => (
          <button key={item.id} type="button" className={`choice-card ${value === item.id ? "active" : ""}`} onClick={() => onChange(toggleable && value === item.id ? "" : item.id)}>
            <strong>{item.title}</strong>
            <span>{item.detail}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function StatusPage({ state, refresh }) {
  return (
    <section className="view-stack">
      <PageHeader
        title="Status"
        subtitle="Live transfer progress and completed task history."
        action={<button className="danger" type="button" onClick={async () => { await api("/api/cancelall", { method: "POST" }); refresh(); }}>Cancel all</button>}
      />
      <div className="split-grid">
        <div className="panel">
          <div className="panel-title">Active tasks</div>
          <TaskList tasks={state?.active || []} refresh={refresh} empty="No active tasks" />
        </div>
        <div className="panel">
          <div className="panel-title">Completed tasks</div>
          <TaskList tasks={(state?.recent || []).slice(0, 12)} compact empty="No completed tasks" />
        </div>
      </div>
    </section>
  );
}

function TaskList({ tasks, compact = false, refresh, empty }) {
  if (!tasks.length) return <div className="empty">{empty}</div>;
  return (
    <div className="task-list">
      {tasks.map((task) => (
        <TaskCard key={task.full_id || task.id} task={task} compact={compact} refresh={refresh} />
      ))}
    </div>
  );
}

function TaskCard({ task, compact, refresh }) {
  const progress = task.progress ?? 0;
  const links = task.result?.links || [];
  const cancel = async () => {
    await api(`/api/cancel/${task.id}`, { method: "POST" });
    refresh?.();
  };

  return (
    <article className={`task-card ${compact ? "compact" : ""}`}>
      <div className="task-head">
        <div>
          <div className="task-name">
            {task.name}
          </div>
          <p>{task.destination} / {task.source}</p>
        </div>
        <span className="phase">{task.phase}</span>
      </div>
      {task.progress !== null && (
        <div className="progress">
          <span style={{ width: `${Math.min(100, Math.max(0, progress))}%` }} />
        </div>
      )}
      <div className="task-meta">
        <span>{task.processed} / {task.size}</span>
        <span>{task.speed}</span>
        {task.eta && <span>ETA {task.eta}</span>}
        {task.current_file && <span>{task.current_file}</span>}
      </div>
      {task.error && <pre>{task.error}</pre>}
      <div className="task-actions">
        {task.selection_url && !task.terminal && <a className="button-link" href={appUrl(task.selection_url)} target="_blank" rel="noreferrer">Selector <ExternalLink size={14} /></a>}
        {!task.terminal && <button className="danger small-button" type="button" onClick={cancel}>Cancel</button>}
        {links.map((link, index) => (
          <a className="button-link" key={`${link.url}-${index}`} href={appUrl(link.url)} target="_blank" rel="noreferrer">
            {link.label || `Open ${index + 1}`}
          </a>
        ))}
      </div>
    </article>
  );
}

function FilesPage({ jellyfinUrl, showToast }) {
  const [result, setResult] = useState(null);
  const openLocal = async () => {
    try {
      const response = await api("/api/local", { method: "POST" });
      window.open(appUrl(response.url), "_blank");
      setResult({ title: "File explorer opened", rows: [["Session", "Temporary 5 minute explorer"]], links: [{ label: "Click here", url: response.url }] });
    } catch (error) {
      showToast(error.message);
    }
  };
  const scan = async () => {
    try {
      const response = await api("/api/jellyfin/scan", { method: "POST" });
      setResult({ title: "Jellyfin scan requested", rows: [["State", response.state], ["Health", response.health], ["Cleanup", `${response.result} stale item(s) removed`]] });
      showToast("Jellyfin scan requested");
    } catch (error) {
      setResult({ title: "Action failed", rows: [["Error", error.message]] });
      showToast(error.message);
    }
  };
  return (
    <section className="view-stack narrow">
      <PageHeader title="Files" subtitle="Temporary explorer sessions and media library actions." />
      <div className="panel action-grid">
        <ActionButton title="Open file explorer" detail="Browse, move, rename, delete, upload, and scan" icon={Files} onClick={openLocal} />
        <ActionButton title="Open Jellyfin" detail="Open the media server" icon={Server} href={jellyfinUrl} />
        <ActionButton title="Scan library" detail="Full scan and metadata refresh" icon={RefreshCw} onClick={scan} />
      </div>
      <ResultPanel result={result} />
    </section>
  );
}

function DrivePage({ showToast }) {
  const [query, setQuery] = useState("");
  const [share, setShare] = useState("");
  const [deleteId, setDeleteId] = useState("");
  const [result, setResult] = useState(null);

  const run = async (fn) => {
    try {
      await fn();
    } catch (error) {
      setResult({ title: "Drive action failed", rows: [["Error", error.message]] });
      showToast(error.message);
    }
  };

  const quotaRows = (quota) => {
    const limit = Number(quota?.limit || 0);
    const usage = Number(quota?.usage || 0);
    const trash = Number(quota?.usageInDriveTrash || 0);
    const free = limit ? Math.max(0, limit - usage) : 0;
    return [["Used", formatBytes(usage)], ["Free", formatBytes(free)], ["Total", limit ? formatBytes(limit) : "Unlimited"], ["Trash", formatBytes(trash)]];
  };

  return (
    <section className="view-stack narrow">
      <PageHeader title="Google Drive" subtitle="Search, public share pages, deletion, and quota." />
      <div className="panel form-stack">
        <InputAction icon={FileSearch} label="Search Drive" value={query} onChange={setQuery} placeholder="Search files or folders" button="Search" onSubmit={() => run(async () => {
          const response = await api("/api/drive/search", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ query }) });
          setResult(response.url
            ? { title: "Search results ready", rows: [["Results", `${response.count}`]], links: [{ label: "Click here", url: response.url }] }
            : { title: "No matching Drive items", rows: [["Results", "0"]] });
        })} />
        <InputAction icon={ExternalLink} label="Share Drive link" value={share} onChange={setShare} placeholder="Public Drive file or folder link" button="Share" onSubmit={() => run(async () => {
          const response = await api("/api/drive/share", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ link: share }) });
          setResult({ title: response.name, rows: [["Files", `${response.files}`], ["Folders", `${response.folders}`], ["Expires", "5 minutes"]], links: [{ label: "Click here", url: response.url }] });
        })} />
        <InputAction icon={Trash2} label="Delete Drive item" value={deleteId} onChange={setDeleteId} placeholder="Drive link or ID" button="Delete" danger onSubmit={() => run(async () => {
          if (!window.confirm("Delete this Drive item permanently?")) return;
          const response = await api("/api/drive/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: deleteId }) });
          setResult({ title: "Drive item deleted", rows: [["Name", response.name || deleteId]] });
        })} />
        <div className="row-actions">
          <button className="secondary" type="button" onClick={() => run(async () => {
            const response = await api("/api/drive/stats");
            setResult(response.ready ? { title: "Drive quota", rows: quotaRows(response.quota) } : { title: "Drive is not ready", rows: [["Credentials", response.credentials ? "Found" : "Missing"], ["Token", response.token ? "Found" : "Missing"]] });
          })}>
            Drive quota
          </button>
        </div>
        <ResultPanel result={result} />
      </div>
    </section>
  );
}

function JellyfinPage({ state, jellyfinUrl, showToast, refresh }) {
  const status = state?.stats?.jellyfin;
  const [result, setResult] = useState(null);
  const action = async (name) => {
    try {
      const response = await api(`/api/jellyfin/${name}`, { method: "POST" });
      setResult({ title: "Jellyfin action complete", rows: [["Action", name], ["Result", response.result], ["State", response.state], ["Health", response.health]] });
      showToast("Jellyfin action sent");
      refresh();
    } catch (error) {
      setResult({ title: "Jellyfin action failed", rows: [["Error", error.message]] });
      showToast(error.message);
    }
  };

  return (
    <section className="view-stack narrow">
      <PageHeader title="Jellyfin" subtitle="Server control and full library refresh." />
      <div className="panel">
        <div className="server-card">
          <Server size={28} />
          <div>
            <strong>{status?.state || "unknown"}</strong>
            <span>{status?.health || "unknown"}</span>
          </div>
          <a className="button-link" href={jellyfinUrl} target="_blank" rel="noreferrer">Open Jellyfin <ExternalLink size={14} /></a>
        </div>
        <div className="button-grid">
          <button type="button" onClick={() => action("scan")}><RefreshCw size={16} /> Scan library</button>
          <button className="secondary" type="button" onClick={() => action("start")}><Play size={16} /> Start</button>
          <button className="secondary" type="button" onClick={() => action("stop")}><Square size={16} /> Stop</button>
          <button className="secondary" type="button" onClick={() => action("restart")}><RotateCcw size={16} /> Restart</button>
        </div>
      </div>
      <ResultPanel result={result} />
    </section>
  );
}

function AdminPage({ showToast }) {
  const [result, setResult] = useState(null);
  const speedtest = async () => {
    setResult({ title: "Running speedtest...", rows: [["Status", "Please wait"]] });
    try {
      const response = await api("/api/speedtest", { method: "POST" });
      setResult({
        title: "Speedtest result",
        rows: [
          ["Download", formatNumber(response.download_mbps, " Mbps")],
          ["Upload", formatNumber(response.upload_mbps, " Mbps")],
          ["Ping", formatNumber(response.ping_ms, " ms")],
          ["Server", `${response.sponsor} - ${response.server}`],
          ["ISP", response.isp],
        ],
      });
    } catch (error) {
      setResult({ title: "Speedtest failed", rows: [["Error", error.message]] });
      showToast(error.message);
    }
  };
  const restart = async () => {
    if (!window.confirm("Restart Mirror-Bot?")) return;
    await api("/api/restart", { method: "POST" });
    showToast("Restart requested");
  };
  return (
    <section className="view-stack narrow">
      <PageHeader title="Admin" subtitle="Runtime tools for logs, restart, and network checks." />
      <div className="panel action-grid">
        <ActionButton title="Speedtest" detail="Run a network speed test" icon={Gauge} onClick={speedtest} />
        <ActionButton title="Download logs" detail="Get sanitized app logs" icon={Download} href="/api/logs" />
        <ActionButton title="Restart bot" detail="Gracefully restart Mirror-Bot" icon={RotateCcw} onClick={restart} danger />
      </div>
      <ResultPanel result={result} />
    </section>
  );
}

function ActionButton({ title, detail, icon: Icon, onClick, href, danger }) {
  const content = (
    <>
      <Icon size={20} />
      <strong>{title}</strong>
      <span>{detail}</span>
    </>
  );
  const className = `action-card ${danger ? "danger-card" : ""}`;
  if (href) {
    return <a className={className} href={href} target="_blank" rel="noreferrer">{content}</a>;
  }
  return <button className={className} type="button" onClick={onClick}>{content}</button>;
}

function InputAction({ icon: Icon, label, value, onChange, placeholder, button, onSubmit, danger }) {
  return (
    <label className="input-action">
      <span><Icon size={15} /> {label}</span>
      <div>
        <input value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} />
        <button className={danger ? "danger" : ""} type="button" onClick={onSubmit}>{button}</button>
      </div>
    </label>
  );
}

function ResultPanel({ result }) {
  if (!result) return null;
  return (
    <div className="result-panel">
      <div className="result-title">{result.title}</div>
      {Boolean(result.rows?.length) && (
        <div className="result-grid">
          {result.rows.map(([label, value]) => (
            <div className="result-row" key={label}>
              <span>{label}</span>
              <strong>{String(value ?? "-")}</strong>
            </div>
          ))}
        </div>
      )}
      {Boolean(result.links?.length) && (
        <div className="result-actions">
          {result.links.map((link, index) => (
            <a className="button-link" key={`${link.url}-${index}`} href={appUrl(link.url)} target="_blank" rel="noreferrer">
              {link.label || "Click here"} <ExternalLink size={14} />
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);
