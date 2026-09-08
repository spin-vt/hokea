"""Turn the mkdocs-print-site print page into ONE self-contained,
navigable HTML file: dist/hokea-docs.html.

Called by scripts/docs-single-page.sh after the export build. What it does,
in order:

1. Inlines every local stylesheet (and the url(...) assets they reference)
   into <style> blocks; drops external font/preconnect links.
2. Strips ALL of the site's JavaScript, Material's chrome included. This is
   a deliberate design decision, not a limitation of the target (inline
   scripts are allowed — only external fetches are forbidden): Material's
   bundle is built for the multi-page site — its search runs in a web
   worker loaded from a separate file and fetches a JSON index, its
   "instant navigation" XHRs other pages, its sidebar links point at pages
   that don't exist in a single file — and mkdocs-print-site's own
   bootstrap JS *removes* Material's navigation on load because the print
   page isn't meant to use it. Rather than fight both, this script removes
   the dead chrome and builds a navigation layer designed for the
   flattened page (step 4).
3. Repairs anchors: mkdocs-print-site rewrites cross-page links to in-page
   anchors but occasionally leaves a heading's id unprefixed while
   prefixing the links to it; those ids are fixed so every internal link
   resolves. Anything still unresolvable (e.g. autorefs to Python
   builtins) is downgraded to plain text.
4. Adds the navigation layer:
   - a full table of contents at the top (#contents), replacing the
     placeholder print-site would have filled with JavaScript;
   - a floating, collapsible "Contents" drawer (a <details> element, so it
     works with JavaScript disabled; ~10 lines of inline JS close it when
     a link is tapped);
   - a "Back to contents" link at the end of every section;
   - mobile-first CSS: centered measure, tap-friendly TOC targets, no
     horizontal page scroll.
5. Writes dist/hokea-docs.html and runs the self-containment check: no
   <link> tags, no <script src=...>, every src/href either an in-page
   anchor that RESOLVES or an external hyperlink, every CSS url() a data:
   URI. Inline <script> bodies are permitted.
"""

import base64
import mimetypes
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

SRC = Path("site-export/print_page/index.html")
OUT = Path("dist/hokea-docs.html")

EXTRA_CSS = """
/* ---- single-file export: layout after Material's chrome is removed ---- */
body { background: #fff; }
.md-main__inner { display: block; margin-top: 0; }
.md-content { max-width: 50rem; margin: 0 auto; padding: 0.2rem 0.8rem 7rem; }
:root { scroll-behavior: smooth; }
[id] { scroll-margin-top: 0.75rem; }

/* top table of contents — print-site ships it print-only (display:none on
   screen, since its own TOC needed JS anyway); ours is the page's primary
   navigation, so show it everywhere */
#print-page-toc { display: block; }

/* top table of contents */
.hokea-toc-list, .hokea-toc-list ol { list-style: none; margin: 0; padding: 0; }
.hokea-toc-list ol { margin-left: 1rem; }
.hokea-toc-list a { display: block; padding: 0.4em 0.2em; }
.hokea-toc-list > li { margin-bottom: 0.35em; }
.hokea-toc-list > li > a, .hokea-toc-group { font-weight: 700; }
.hokea-toc-group { display: block; padding: 0.3em 0.2em; }

/* per-section back link */
.hokea-back { text-align: right; margin: 2rem 0 0; }
.hokea-back a { display: inline-block; padding: 0.5em 0.25em; }

/* floating contents drawer (details/summary: works without JS) */
#hokea-toc {
  position: fixed; right: 0.75rem; bottom: 0.75rem; z-index: 100;
  text-align: right;
}
#hokea-toc summary {
  list-style: none; cursor: pointer; user-select: none; display: inline-block;
  background: #4051b5; color: #fff; font-weight: 700; font-size: 0.7rem;
  border-radius: 2rem; padding: 0.65em 1.2em;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.35);
}
#hokea-toc summary::-webkit-details-marker { display: none; }
#hokea-toc summary::marker { content: ""; }
#hokea-toc .hokea-toc-panel {
  position: absolute; right: 0; bottom: calc(100% + 0.5rem);
  text-align: left;
  width: min(21rem, calc(100vw - 2rem));
  max-height: min(70vh, 32rem); overflow-y: auto; overscroll-behavior: contain;
  background: #fff; border: 1px solid #ccc; border-radius: 0.4rem;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25);
  padding: 0.6rem 0.9rem; font-size: 0.7rem; line-height: 1.35;
}
@media print { #hokea-toc, .hokea-back { display: none; } }

/* keep wide content scrolling inside its own box, never the page */
.md-typeset pre > code { overflow-x: auto; }
.md-typeset table:not([class]) { display: block; overflow-x: auto; }
"""

EXTRA_JS = """
// Close the floating contents drawer when a link in it is tapped, or when
// anything outside it is tapped. Without JS the drawer still opens and
// closes via its <summary>.
document.addEventListener('click', function (e) {
  var toc = document.getElementById('hokea-toc');
  if (!toc || !toc.open) return;
  var link = e.target.closest ? e.target.closest('a') : null;
  if ((link && toc.contains(link)) || !toc.contains(e.target)) {
    toc.open = false;
  }
});
"""


def inline_css_urls(css: str, css_dir: Path) -> str:
    """Inline url(...) references in CSS as data: URIs; drop unresolvable
    or remote ones (a missing decoration beats a blocked network fetch)."""
    def repl(m):
        ref = m.group(1).strip("'\"")
        if ref.startswith(("data:", "#")):
            return m.group(0)
        if ref.startswith(("http:", "https:", "//")):
            return "url()"
        path = (css_dir / ref.split("?")[0].split("#")[0]).resolve()
        if not path.is_file():
            return "url()"
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        b64 = base64.b64encode(path.read_bytes()).decode()
        return f"url(data:{mime};base64,{b64})"
    return re.sub(r"url\(([^)]*)\)", repl, css)


def heading_title(h) -> str:
    return h.get_text().replace("¶", "").strip()


def build_toc_list(soup, sections):
    """One <ol>: every top-level section with its h2 children (depth 2);
    the API reference sections grouped under one entry, without their h2s
    (the drawer stays scannable)."""
    ol = soup.new_tag("ol", attrs={"class": "hokea-toc-list"})
    api_children_ol = None
    for sec in sections:
        sid = sec.get("id")
        h1 = sec.find("h1")
        title = heading_title(h1) if h1 else sid
        li = soup.new_tag("li")
        a = soup.new_tag("a", href=f"#{sid}")
        a.string = title
        li.append(a)
        if sid.startswith("api"):
            if api_children_ol is None:
                group_li = soup.new_tag("li")
                group_li.append(li_a := soup.new_tag("a", href=f"#{sid}"))
                li_a.string = "API reference"
                api_children_ol = soup.new_tag("ol")
                group_li.append(api_children_ol)
                ol.append(group_li)
            if sid != "api":  # the overview is the group link itself
                api_children_ol.append(li)
            continue
        subs = [h2 for h2 in sec.find_all("h2") if h2.get("id")]
        if subs:
            sub_ol = soup.new_tag("ol")
            for h2 in subs:
                sub_li = soup.new_tag("li")
                sub_a = soup.new_tag("a", href=f"#{h2['id']}")
                sub_a.string = heading_title(h2)
                sub_li.append(sub_a)
                sub_ol.append(sub_li)
            li.append(sub_ol)
        ol.append(li)
    return ol


def main():
    html = SRC.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")

    # -- 1. stylesheets: inline local, drop external ------------------------
    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel") or [])
        href = link.get("href", "")
        if "stylesheet" in rel and not href.startswith(("http:", "https:", "//")):
            path = (SRC.parent / href.split("?")[0]).resolve()
            css = inline_css_urls(path.read_text(encoding="utf-8"), path.parent)
            style = soup.new_tag("style")
            style.string = css
            link.replace_with(style)
        else:
            link.decompose()  # fonts, favicon, preconnect: never fetched

    # -- 2. drop all site JS and the Material chrome it would drive --------
    for tag in soup.find_all("script"):
        tag.decompose()
    for sel in ("header.md-header", "nav.md-tabs", "div.md-sidebar",
                "footer.md-footer", "input.md-toggle", "label.md-overlay",
                "div.md-dialog", "div.md-progress", "a.md-top", "dialog",
                '[data-md-component="skip"]', '[data-md-component="announce"]',
                '[data-md-component="outdated"]'):
        for tag in soup.select(sel):
            tag.decompose()

    # -- 3a. repair prefix-less heading ids ---------------------------------
    # print-site prefixes each page's anchors with the page slug, but a
    # heading occasionally keeps its unprefixed id while its permalink (and
    # the links pointing at it) got the prefixed one. Move the heading onto
    # the prefixed id; keep the old id on a spare <span> only if something
    # still links to it.
    def all_ids():
        return {t["id"] for t in soup.find_all(attrs={"id": True})}

    href_targets = {a["href"][1:] for a in soup.find_all("a", href=True)
                    if a["href"].startswith("#")}
    fixed = []
    for perm in soup.select("a.headerlink[href^='#']"):
        target = perm["href"][1:]
        h = perm.parent
        if not h or not h.name or not re.fullmatch(r"h[1-6]", h.name):
            continue
        old = h.get("id")
        if old == target or target in all_ids():
            continue
        h["id"] = target
        fixed.append(target)
        if old and old in href_targets and old not in all_ids():
            keep = soup.new_tag("span", id=old)
            h.insert(0, keep)

    # -- 4. the navigation layer -------------------------------------------
    sections = [s for s in soup.select("section.print-page") if s.get("id")]

    # top TOC into print-site's (JS-only, so empty) placeholder
    placeholder = soup.find(id="print-page-toc")
    toc_nav = placeholder.find("nav") if placeholder else None
    if toc_nav is None:
        sys.exit("print-page-toc placeholder not found; did print-site change?")
    contents_section = placeholder.find_parent("section")
    contents_section["id"] = "contents"
    doc_title = soup.new_tag("h1")
    doc_title.string = "hokea — full documentation"
    contents_section.insert(0, doc_title)
    toc_title = toc_nav.find("h1")
    if toc_title is not None:
        toc_title.name = "h2"
        toc_title.string = "Contents"
    toc_nav.append(build_toc_list(soup, sections))

    # floating drawer with a copy of the same list
    drawer = soup.new_tag("details", id="hokea-toc")
    summary = soup.new_tag("summary")
    summary.string = "☰ Contents"
    drawer.append(summary)
    panel = soup.new_tag("nav", attrs={"class": "hokea-toc-panel",
                                       "aria-label": "Table of contents"})
    top_link_p = soup.new_tag("p", attrs={"class": "hokea-toc-list"})
    top_a = soup.new_tag("a", href="#contents")
    top_a.string = "↑ Top of document"
    top_link_p.append(top_a)
    panel.append(top_link_p)
    panel.append(build_toc_list(soup, sections))
    drawer.append(panel)
    soup.body.append(drawer)

    # per-section back links
    for sec in sections:
        p = soup.new_tag("p", attrs={"class": "hokea-back"})
        a = soup.new_tag("a", href="#contents")
        a.string = "↑ Back to contents"
        p.append(a)
        sec.append(p)

    # our CSS and JS, inline
    style = soup.new_tag("style")
    style.string = EXTRA_CSS
    soup.head.append(style)
    script = soup.new_tag("script")
    script.string = EXTRA_JS
    soup.body.append(script)

    # -- 3b. neutralize what still cannot resolve ---------------------------
    # Local-file links would dangle from a single file; in-page links whose
    # target does not exist (autorefs to Python builtins like #Exception)
    # would silently no-op. Both keep their text and lose their href.
    # print-site rewrites links to the site homepage (index.md — the
    # landing page) to "#." instead of its section anchor; point them at it.
    for a in soup.find_all("a", href="#."):
        a["href"] = "#index"

    ids = all_ids()
    dropped = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith(("http:", "https:", "mailto:")):
            continue
        if href.startswith("#") and href[1:] in ids:
            continue
        dropped.append(href)
        del a["href"]
    if dropped:
        print(f"de-linked {len(dropped)} unresolvable href(s): "
              f"{sorted(set(dropped))[:10]}")
    if fixed:
        print(f"repaired {len(fixed)} heading id(s): {fixed}")

    out_html = str(soup)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(out_html, encoding="utf-8")

    # -- 5. self-containment check ------------------------------------------
    # Inline <script> bodies are fine (the target allows inline JS); what is
    # forbidden is anything the page would try to FETCH: script/link/img/...
    # pointing at a file, or a CSS url() that is not a data: URI.
    problems = []
    if re.search(r"<script\b[^>]*\bsrc\s*=", out_html):
        problems.append("<script src=...> (external script fetch)")
    if re.search(r"<link\b", out_html):
        problems.append("residual <link> tag")
    ids = set(re.findall(r'\bid="([^"]+)"', out_html))
    for m in re.finditer(r'<[a-zA-Z][^<>]*?\s(?:src|href)="([^"]+)"', out_html):
        ref = m.group(1)
        if ref.startswith(("http:", "https:", "mailto:", "data:")):
            continue  # external hyperlink / embedded data: navigation, not a fetch
        if ref.startswith("#"):
            if ref[1:] in ids:
                continue  # in-page anchor that resolves
            problems.append(f"dangling in-page anchor: {ref}")
            continue
        problems.append(f"local file reference: {ref}")
    for m in re.finditer(r"url\(([^)]*)\)", out_html):
        ref = m.group(1).strip("'\"")
        if ref and not ref.startswith(("data:", "#")):
            problems.append(f"non-inlined CSS url: {ref}")
    if problems:
        sys.exit("NOT self-contained:\n  " + "\n  ".join(problems[:20]))
    size = OUT.stat().st_size
    n_anchor = out_html.count('href="#')
    print(f"OK: {OUT} ({size / 1e6:.1f} MB) is self-contained: all CSS "
          f"inlined, only inline scripts, no external assets, and every "
          f"one of the {n_anchor} in-page links resolves.")


if __name__ == "__main__":
    main()
