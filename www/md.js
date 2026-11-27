// Minimal, safe markdown for AI answers (Ask the Manuals and the kit chat page). No dependencies.
// The whole text is HTML-escaped FIRST; only then are a few patterns turned into tags this file writes
// itself (<p> <br> <strong> <em> <code> <ul> <ol> <li> <h3>-<h6>). Model output can never add a tag, an
// attribute or a link: the only links come from cite(n), which the caller builds from its own source
// list (Ask the Manuals: [n] -> that source's PDF page). Unclosed **, * or ` (e.g. mid-stream) stay literal.

export const esc = s => String(s).replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));

function inline(s, cite){ // s is already escaped
  const codes = [];
  s = s.replace(/`([^`\n]+)`/g, (m, c) => `\u0000${codes.push(c) - 1}\u0000`);
  s = s.replace(/\*\*(?=\S)([^*\n]*?\S)\*\*/g, "<strong>$1</strong>")
       .replace(/__(?=\S)([^_\n]*?\S)__/g, "<strong>$1</strong>")
       .replace(/(^|[^*\w])\*(?=[^\s*])([^*\n]*?[^\s*])\*(?!\*)/g, "$1<em>$2</em>")
       .replace(/(^|[^_\w])_(?=[^\s_])([^_\n]*?[^\s_])_(?![_\w])/g, "$1<em>$2</em>");
  if (cite) s = s.replace(/\[(\d{1,3})\]/g, (m, n) => { const href = cite(+n);
    return href ? `<a href="${esc(href)}" target="_blank" rel="noopener">[${n}]</a>` : m; });
  return s.replace(/\u0000(\d+)\u0000/g, (m, i) => `<code>${codes[+i]}</code>`);
}

export function renderMarkdown(text, cite){
  const lines = esc(String(text).replace(/\r\n?/g, "\n").replace(/\u0000/g, "")).split("\n");
  const out = []; let list = null, para = [];
  const flushPara = () => { if (para.length){ out.push(`<p>${para.map(l => inline(l, cite)).join("<br>")}</p>`); para = []; } };
  const closeList = () => { if (list){ out.push(`</${list}>`); list = null; } };
  for (const raw of lines){
    const line = raw.replace(/\s+$/, "");
    let m;
    if (!line.trim()){ flushPara(); continue; }   // a blank line ends a paragraph; a list goes on if the next line is an item
    if ((m = line.match(/^\s{0,3}(#{1,6})\s+(.*?)\s*#*$/))){
      flushPara(); closeList(); const lvl = Math.min(6, m[1].length + 2);
      out.push(`<h${lvl}>${inline(m[2], cite)}</h${lvl}>`); continue; }
    if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)){ flushPara(); closeList(); out.push("<hr>"); continue; }
    if ((m = line.match(/^\s*([-*+•])\s+(.*)$/)) || (m = line.match(/^\s*(\d{1,3})[.)]\s+(.*)$/))){
      const kind = /\d/.test(m[1]) ? "ol" : "ul";
      flushPara(); if (list !== kind){ closeList(); out.push(kind === "ol" && +m[1] > 1 ? `<ol start="${+m[1]}">` : `<${kind}>`); list = kind; }
      out.push(`<li>${inline(m[2], cite)}</li>`); continue; }
    if (list && !para.length && /^\s{2,}\S/.test(line)){ // continuation of the previous list item
      out[out.length - 1] = out[out.length - 1].replace(/<\/li>$/, `<br>${inline(line.trim(), cite)}</li>`); continue; }
    closeList(); para.push(line.trim());
  }
  flushPara(); closeList();
  return out.join("");
}
