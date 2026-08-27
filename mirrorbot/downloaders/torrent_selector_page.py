"""The self-contained file-selection page served by TorrentSelector.

All of the selector's HTML, CSS, and JS lives here so torrent_selector.py holds
only the aiohttp handlers and selection state.
"""


def render_selection_page(rows_html: str) -> str:
    return _PAGE.replace("__ROWS__", rows_html)


_PAGE = """\
<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Select torrent files</title>
<style>
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
[hidden] { display: none !important; }
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
body{background:var(--bg)}
.appbar{position:sticky;top:0;z-index:8;border-bottom:1px solid var(--line);background:color-mix(in srgb,var(--surface) 94%,transparent);backdrop-filter:blur(14px)}
.appbar-inner{max-width:1180px;margin:0 auto;padding:14px 18px;display:flex;align-items:center;justify-content:space-between;gap:14px}
.brand{display:grid;gap:5px;min-width:0}.brand h1{font-size:22px;margin:0}.brand p{margin:0;color:var(--muted)}
.meta-pills{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}.meta-pills span{display:inline-flex;align-items:center;min-height:34px;border:1px solid var(--line);border-radius:999px;background:var(--surface-soft);padding:6px 10px;color:var(--muted);font-weight:760;white-space:nowrap}
.shell{max-width:1180px;margin:0 auto;padding:16px 18px calc(var(--selectionbar-height,72px) + 32px);display:grid;gap:12px}
.toolbar,.tree-card,.selectionbar{border:1px solid var(--line);border-radius:10px;background:var(--surface);box-shadow:var(--shadow)}
.toolbar{padding:10px;display:flex;align-items:center;gap:10px}.toolbar input{flex:1;min-width:220px}.toolbar-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
ul{list-style:none;margin:0;padding:0}.tree-card{overflow:hidden}
.row{display:grid;grid-template-columns:32px 24px minmax(0,1fr) auto;gap:9px;padding:10px 14px 10px calc(14px + var(--depth) * 20px);border-bottom:1px solid var(--line);align-items:center;transition:background .12s ease}.row:hover{background:var(--surface-soft)}
.folder>.row{background:color-mix(in srgb,var(--surface-soft) 45%,var(--surface))}.folder-name,.name{min-width:0;overflow-wrap:anywhere}.folder-name{justify-content:flex-start;min-height:0;padding:0;text-align:left;background:transparent;color:var(--text);font-weight:820;border:0;border-radius:2px}.folder-name:hover{background:transparent;color:var(--primary);text-decoration:underline}
.name{font-weight:760}small{color:var(--muted);white-space:nowrap}.expand{width:30px;height:30px;min-height:30px;padding:0;margin:0;background:var(--surface-soft);color:var(--text);border:1px solid var(--line-strong);border-radius:7px;font-weight:900}.spacer{width:30px}
.selectionbar{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:10;width:min(1180px,calc(100vw - 32px));padding:10px;display:flex;align-items:center;justify-content:space-between;gap:10px;border-color:color-mix(in srgb,var(--primary) 42%,var(--line));background:color-mix(in srgb,var(--primary-soft) 45%,var(--surface));backdrop-filter:blur(14px)}.selectionbar .count{font-weight:850}.selection-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end}
@media(max-width:760px){.appbar-inner{display:grid;padding:12px}.brand h1{font-size:20px}.meta-pills{justify-content:flex-start}.shell{padding:12px 10px calc(var(--selectionbar-height,120px) + 24px)}.toolbar{display:grid;align-items:stretch}.toolbar input{min-width:0}.toolbar-actions{width:100%;display:grid;grid-template-columns:1fr 1fr}.toolbar-actions button{width:100%}.tree-card{border-radius:8px}.row{grid-template-columns:30px 22px minmax(0,1fr);gap:7px;padding:10px 10px 10px calc(10px + var(--depth) * 12px)}small{display:none}.folder-name,.name{font-size:13px}.selectionbar{display:grid;bottom:max(10px,env(safe-area-inset-bottom));width:calc(100vw - 20px);max-height:45vh;overflow:auto}.selection-actions{display:grid;grid-template-columns:1fr 1fr;width:100%}.selection-actions button{width:100%}}@media(max-width:430px){.toolbar-actions{grid-template-columns:1fr}.row{padding-left:calc(8px + var(--depth) * 9px)}}
</style></head><body>
<header class="appbar"><div class="appbar-inner"><div class="brand"><h1>Select torrent files</h1><p>Expand folders and choose only the files you want.</p></div><div class="meta-pills"><span>Nothing selected by default</span><span>Temporary selector</span></div></div></header>
<main class="shell">
<form method="post">
<section class="toolbar"><input id="search" type="search" placeholder="Search files and folders"><div class="toolbar-actions"><button class="secondary" type="button" id="check-all">Check all</button><button class="secondary" type="button" id="uncheck-all">Uncheck all</button></div></section>
<section class="tree-card"><ul id="tree">__ROWS__</ul></section>
<section class="selectionbar" id="selectionbar"><span class="count" id="count">0 files selected</span><div class="selection-actions"><button type="submit">Start download</button><button class="cancel" type="submit" name="action" value="cancel">Cancel</button></div></section>
</form>
</main>
<script>
const setChildren=(folder,checked)=>folder.querySelectorAll('input[type=checkbox]').forEach(box=>{box.checked=checked;box.indeterminate=false;});
const selectionbar=document.getElementById('selectionbar');
const syncSelectionSpace=()=>document.documentElement.style.setProperty('--selectionbar-height',`${Math.ceil(selectionbar.getBoundingClientRect().height)}px`);
new ResizeObserver(syncSelectionSpace).observe(selectionbar);window.addEventListener('resize',syncSelectionSpace);syncSelectionSpace();
const updateCount=()=>{const n=document.querySelectorAll('.file-check:checked').length;document.getElementById('count').textContent=`${n} file${n===1?'':'s'} selected`;};
const updateParents=element=>{
 let folder=element.closest('.folder');
 while(folder){
  const parent=folder.querySelector(':scope > .row > .folder-check');
  const files=[...folder.querySelectorAll('.file-check')];
  parent.checked=files.length>0&&files.every(file=>file.checked);
  parent.indeterminate=files.some(file=>file.checked)&&!parent.checked;
  folder=folder.parentElement.closest('.folder');
 } updateCount();
};
const toggleFolder=target=>{
 const tree=document.getElementById(target); tree.hidden=!tree.hidden;
 const button=document.querySelector(`.expand[data-target="${target}"]`);
 button.textContent=tree.hidden?'+':'-'; button.setAttribute('aria-expanded',String(!tree.hidden));
};
document.querySelectorAll('.expand,.folder-name').forEach(button=>button.addEventListener('click',()=>toggleFolder(button.dataset.target)));
document.querySelectorAll('.folder-check').forEach(box=>box.addEventListener('change',()=>{setChildren(box.closest('.folder'),box.checked);updateParents(box.closest('.folder').parentElement);}));
document.querySelectorAll('.file-check').forEach(box=>box.addEventListener('change',()=>{updateParents(box);}));
document.getElementById('check-all').addEventListener('click',()=>{document.querySelectorAll('input[type=checkbox]').forEach(box=>{box.checked=true;box.indeterminate=false;});updateCount();});
document.getElementById('uncheck-all').addEventListener('click',()=>{document.querySelectorAll('input[type=checkbox]').forEach(box=>{box.checked=false;box.indeterminate=false;});updateCount();});
document.getElementById('search').addEventListener('input',e=>{const q=e.target.value.toLowerCase();document.querySelectorAll('#tree li.file').forEach(row=>row.hidden=!!q&&!row.textContent.toLowerCase().includes(q));document.querySelectorAll('#tree li.folder').forEach(row=>{const match=!q||row.textContent.toLowerCase().includes(q);row.hidden=!match;if(q&&match){const tree=row.querySelector(':scope > ul');if(tree)tree.hidden=false;const button=row.querySelector(':scope > .row > .expand');if(button){button.textContent='-';button.setAttribute('aria-expanded','true')}}});});
</script>
</body></html>
"""
