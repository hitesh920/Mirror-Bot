TEMP_PAGE_CSS = """
:root {
  color-scheme: light;
  --bg: #f5f7fb;
  --surface: #ffffff;
  --surface-soft: #f8fafc;
  --text: #162033;
  --muted: #657386;
  --line: #dce4ee;
  --line-strong: #c8d2df;
  --primary: #1769e0;
  --primary-soft: #eaf2ff;
  --green: #079455;
  --red: #b42318;
  --shadow: 0 12px 32px rgba(16, 24, 40, 0.08);
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --bg: #0f141b;
    --surface: #161d26;
    --surface-soft: #111821;
    --text: #e8edf5;
    --muted: #9aa7b8;
    --line: #263140;
    --line-strong: #354356;
    --primary: #69a4ff;
    --primary-soft: #13243d;
    --green: #35c887;
    --red: #ff6b5f;
    --shadow: none;
  }
}
* { box-sizing: border-box; }
html, body { max-width: 100%; overflow-x: hidden; }
body {
  margin: 0;
  overflow-x: hidden;
  background: var(--bg);
  color: var(--text);
  font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
header {
  border-bottom: 1px solid var(--line);
  background: color-mix(in srgb, var(--surface) 94%, transparent);
  backdrop-filter: blur(14px);
}
.top {
  max-width: 1180px;
  margin: 0 auto;
  padding: 24px 18px 16px;
}
h1 {
  margin: 0 0 6px;
  font-size: 24px;
  line-height: 1.15;
  overflow-wrap: anywhere;
}
h2, h3 { margin-top: 0; }
.sub, .meta {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  color: var(--muted);
}
.sub span, .meta span {
  display: inline-flex;
  align-items: center;
  min-height: 28px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface-soft);
  padding: 4px 9px;
}
main {
  width: 100%;
  max-width: 1180px;
  margin: 18px auto;
  padding: 0 18px;
}
form, .panel, table {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  box-shadow: var(--shadow);
}
table {
  width: 100%;
  min-width: 0;
  border-collapse: separate;
  border-spacing: 0;
  overflow: hidden;
}
th, td {
  padding: 11px 12px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: middle;
}
tr:last-child td { border-bottom: 0; }
th {
  background: var(--surface-soft);
  color: var(--muted);
  font-size: 12px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
input, button, a.download, .button-link {
  font: inherit;
}
input[type="search"], input[type="text"], input:not([type]) {
  width: 100%;
  min-height: 40px;
  border: 1px solid var(--line-strong);
  border-radius: 7px;
  background: var(--surface);
  color: var(--text);
  padding: 9px 11px;
}
input:focus, button:focus-visible, a:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--primary) 45%, transparent);
  outline-offset: 2px;
}
button, a.download, .button-link {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  min-height: 38px;
  border: 1px solid var(--line-strong);
  border-radius: 7px;
  background: var(--surface);
  color: var(--text);
  padding: 8px 11px;
  font-weight: 760;
  text-decoration: none;
  cursor: pointer;
}
button:hover, a.download:hover, .button-link:hover {
  background: var(--surface-soft);
}
button.primary, button[type="submit"], .primary, a.primary {
  border-color: var(--primary);
  background: var(--primary);
  color: #ffffff;
}
button.secondary, .secondary {
  background: var(--surface);
  color: var(--text);
}
button.danger, .danger, .cancel {
  color: var(--red);
}
.cancel {
  border-color: var(--line-strong);
  background: var(--surface);
}
.cancel:hover {
  background: var(--surface-soft);
}
.tools, .bar {
  position: sticky;
  top: 0;
  z-index: 3;
  border-bottom: 1px solid var(--line);
  background: color-mix(in srgb, var(--surface) 96%, transparent);
  backdrop-filter: blur(14px);
}
.tools {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  padding: 11px;
}
.tools input { flex: 1; min-width: min(220px, 100%); }
.table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
a { overflow-wrap: anywhere; }
.name { overflow-wrap: anywhere; }
.empty {
  color: var(--muted);
  text-align: center;
  padding: 30px;
}
#toast {
  display: none;
  position: fixed;
  right: 18px;
  bottom: 18px;
  z-index: 20;
  border-radius: 8px;
  background: var(--text);
  color: var(--bg);
  padding: 11px 14px;
  box-shadow: var(--shadow);
}
dialog {
  max-width: min(420px, calc(100vw - 28px));
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  background: var(--surface);
  color: var(--text);
  padding: 22px;
  box-shadow: 0 18px 50px rgba(0,0,0,.25);
}
dialog::backdrop { background: rgba(15,20,27,.62); }
@media (max-width: 700px) {
  .top { padding: 18px 12px 12px; }
  main { margin: 12px auto; padding: 0 10px; }
  h1 { font-size: 21px; }
  .sub, .meta { gap: 7px; }
  .sub span, .meta span { min-height: 26px; padding: 3px 8px; }
  form, .panel, table { border-radius: 8px; }
  .tools, .bar { position: sticky; top: 0; align-items: stretch; }
  .tools input { order: -1; flex: 1 1 100%; min-width: 0; }
  .tools button, .tools a.download, .tools .button-link { flex: 1 1 auto; }
  input[type="search"], input[type="text"], input:not([type]) { min-width: 0; }
  button, a.download, .button-link { min-height: 40px; }
  th, td { padding: 9px 8px; }
  #toast { left: 12px; right: 12px; bottom: 12px; max-width: none; text-align: center; }
  dialog { width: calc(100vw - 24px); padding: 18px; }
}
@media (max-width: 440px) {
  main { padding: 0 8px; }
  button, a.download, .button-link { width: 100%; }
  .sub span, .meta span { max-width: 100%; }
}
"""
