#!/usr/bin/env python3
"""Parse 17 FullEnrich template HTML files and emit a single CSV."""

import csv
import os
import re
import sys
from bs4 import BeautifulSoup, NavigableString, Tag

BASE_DIR = "/tmp/fullenrich-templates"
OUT_PATH = os.path.join(BASE_DIR, "templates.csv")

# All 17 active templates.
TEMPLATES_TABBED_FULL = [
    "airtable-fullenrich",
    "attio-fullenrich",
    "googlesheets-fullenrich",
    "heyreach-fullenrich",
    "lemlist-fullenrich",
    "linkedin-slack-fullenrich",
    "monday-fullenrich",
    "notion-fullenrich",
    "pipedrive-fullenrich",
    "typeform-fullenrich",
]
TEMPLATES_TABBED_ZAPIER_COMING_SOON = [
    "googleads-fullenrich",
    "facebookads-fullenrich",
    "monday-autofill-fullenrich",
    "domains-googlesheets-fullenrich",
]
TEMPLATES_CLAY_ONLY = [
    "clay-fullenrich",
    "linkedin-clay-fullenrich",
    "domains-clay-fullenrich",
]

TEMPLATES = (
    TEMPLATES_TABBED_FULL
    + TEMPLATES_TABBED_ZAPIER_COMING_SOON
    + TEMPLATES_CLAY_ONLY
)

COLUMNS = [
    "slug",
    "card_title",
    "hero_title",
    "hero_description",
    "apps",
    "category",
    "status",
    "logo_primary_url",
    "logo_secondary_url",
    "zapier_template_url",
    "zapier_callout_description",
    "zapier_steps_html",
    "zapier_troubleshooting_html",
    "make_template_url",
    "make_callout_description",
    "make_steps_html",
    "make_troubleshooting_html",
    "n8n_template_url",
    "n8n_callout_description",
    "n8n_steps_html",
    "n8n_troubleshooting_html",
    "clay_template_url",
    "clay_callout_description",
    "clay_steps_html",
    "clay_troubleshooting_html",
    "media_files",
]


def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def soup_of(path):
    return BeautifulSoup(read_file(path), "html.parser")


# ------------------------------------------------------------------------
# Load index.html once so we can look up card_title / apps / category.
# ------------------------------------------------------------------------

INDEX_SOUP = soup_of(os.path.join(BASE_DIR, "index.html"))


def lookup_card_info(slug, detail_soup):
    """Return (card_title, apps, category) for a slug, using index.html.

    Fallback to detail-page <h1> / hero logos when the slug has no card link
    (e.g. HeyReach is marked `coming-soon` in index.html with no href).
    """
    card = None
    # Preferred: find <a href="slug.html"> in index.html, walk up to card.
    link = INDEX_SOUP.find("a", href=f"{slug}.html")
    if link is not None:
        card = link.find_parent(class_="integration-card")

    # Fallback: match by integration-title that equals the detail hero h1.
    if card is None:
        hero = detail_soup.select_one("section.detail-hero h1")
        wanted_title = hero.get_text(strip=True) if hero else ""
        for c in INDEX_SOUP.select(".integration-card"):
            t = c.select_one(".integration-title")
            if t and t.get_text(strip=True) == wanted_title:
                card = c
                break

    card_title = ""
    apps = ""
    category = ""

    if card is not None:
        t = card.select_one(".integration-title")
        if t:
            card_title = t.get_text(" ", strip=True)
        a = card.select_one(".integration-apps")
        if a:
            apps = a.get_text(" ", strip=True)
        category = card.get("data-category", "") or ""

    # Final fallbacks.
    if not card_title:
        hero = detail_soup.select_one("section.detail-hero h1")
        if hero:
            card_title = hero.get_text(" ", strip=True)
    if not apps:
        logos = detail_soup.select(".detail-logos img")
        alts = [img.get("alt") for img in logos if img.get("alt")]
        if alts:
            apps = " + ".join(alts)

    return card_title, apps, category


# ------------------------------------------------------------------------
# Tab helpers
# ------------------------------------------------------------------------

def find_tab(soup, tab_id):
    """Return the tab-content div for a given platform id (e.g. 'tab-zapier')."""
    return soup.select_one(f"div.tab-content#{tab_id}")


def is_coming_soon(tab):
    """Detect the 'Coming Soon' placeholder inside a tab-content div."""
    if tab is None:
        return False
    text = tab.get_text(" ", strip=True).lower()
    return "coming soon" in text and "template for this integration is coming" in text


def extract_template_url(tab, platform):
    """Find the 'Use this Zap' / 'Get Template' / 'Copy Template URL' URL.

    Zapier & Make: first <a> inside the first .callout with class get-template-btn.
    n8n: button with class get-template-btn and onclick=copyN8nTemplateUrl(); the
         URL lives in a <script> block — pull it out with regex.
    """
    if tab is None:
        return ""

    # Try anchor first.
    a = tab.select_one("a.get-template-btn")
    if a and a.get("href"):
        href = a["href"].strip()
        # Skip placeholder hrefs (e.g. href="#" for JS-triggered downloads).
        if href and href != "#":
            return href

    # n8n button → look up the script for the URL.
    btn = tab.select_one("button.get-template-btn")
    if btn is not None and platform == "n8n":
        onclick = btn.get("onclick", "")
        fn_match = re.search(r"(\w+)\s*\(", onclick)
        fn_name = fn_match.group(1) if fn_match else "copyN8nTemplateUrl"
        # Search the full page soup (tab may not include the <script>).
        # Walk up to find the <html>, then find the script.
        root = tab
        while root.parent is not None:
            root = root.parent
        for script in root.find_all("script"):
            stxt = script.string or ""
            if fn_name in stxt:
                m = re.search(r"const\s+url\s*=\s*['\"]([^'\"]+)['\"]", stxt)
                if m:
                    return m.group(1).strip()
                # Fallback: any quoted https URL.
                m = re.search(r"['\"](https?://[^'\"]+)['\"]", stxt)
                if m:
                    return m.group(1).strip()
        return ""

    return ""


def extract_callout_description(tab):
    """First .callout in the tab, with button/flow-diagram/code-block stripped.

    Returns inner HTML (not outer) and preserves remaining markup verbatim.
    """
    if tab is None:
        return ""
    callout = tab.select_one(".callout")
    if callout is None:
        return ""
    clone = BeautifulSoup(str(callout), "html.parser").select_one(".callout")
    # Strip the known sub-elements.
    for sel in [".get-template-btn", ".flow-diagram", ".code-block",
                "a.get-template-btn", "button.get-template-btn"]:
        for node in clone.select(sel):
            node.decompose()
    # Also drop any standalone <button>/<a class="...btn"> that slipped through.
    for node in clone.find_all(["button"]):
        node.decompose()
    # Clean dangling <br> at the start/end and consecutive <br><br>.
    # Keep it simple — just return the remaining inner HTML, trimmed.
    inner = clone.decode_contents().strip()
    # Trim trailing <br> tags.
    inner = re.sub(r"(?:\s*<br\s*/?>\s*)+$", "", inner)
    inner = re.sub(r"^(?:\s*<br\s*/?>\s*)+", "", inner)
    return inner.strip()


def extract_steps_html(tab):
    """Concatenate all <div class="step">...</div> blocks as raw HTML."""
    if tab is None:
        return ""
    steps = tab.select("div.step")
    return "".join(str(s) for s in steps)


def extract_troubleshooting_html(tab):
    """Return the inner <table> HTML from <div class="troubleshooting">."""
    if tab is None:
        return ""
    tb = tab.select_one("div.troubleshooting")
    if tb is None:
        return ""
    table = tb.find("table")
    return str(table) if table else ""


def extract_media_files(soup, tab_roots):
    """Collect unique relative (non-URL) image filenames from tab-content areas.

    tab_roots: iterable of BS elements to scan (tab-content divs, or steps-container
    for Clay-only pages).
    """
    seen = []
    for root in tab_roots:
        if root is None:
            continue
        for img in root.find_all("img"):
            src = (img.get("src") or "").strip()
            if not src:
                continue
            if src.startswith(("http://", "https://", "//", "data:")):
                continue
            if src not in seen:
                seen.append(src)
    return ",".join(seen)


# ------------------------------------------------------------------------
# Per-template build
# ------------------------------------------------------------------------

def build_row(slug):
    path = os.path.join(BASE_DIR, f"{slug}.html")
    soup = soup_of(path)

    card_title, apps, category = lookup_card_info(slug, soup)

    hero = soup.select_one("section.detail-hero")
    hero_title = ""
    hero_description = ""
    if hero is not None:
        h1 = hero.find("h1")
        if h1:
            hero_title = h1.get_text(" ", strip=True)
        # First <p> directly after the h1.
        if h1 is not None:
            sib = h1.find_next_sibling()
            while sib is not None and not (isinstance(sib, Tag) and sib.name == "p"):
                sib = sib.find_next_sibling()
            if isinstance(sib, Tag) and sib.name == "p":
                hero_description = sib.get_text(" ", strip=True)

    logo_primary = ""
    logo_secondary = ""
    # Walk .detail-logo wrappers in order, extracting img src where present.
    # Clay-only pages use an inline <svg> for the first logo — fall back to
    # the card's integration-logo img in index.html for that slug.
    logo_wrappers = soup.select(".detail-logos > .detail-logo")
    logo_urls = []
    for wrap in logo_wrappers:
        img = wrap.find("img")
        if img and img.get("src"):
            logo_urls.append(img["src"].strip())
        else:
            logo_urls.append(None)  # placeholder for SVG-only slot
    if logo_urls and logo_urls[0] is None:
        # Clay-only pages render the Clay mark as an inline SVG, so fall back
        # to the Clay wordmark hosted by the marketing site.
        if slug in TEMPLATES_CLAY_ONLY:
            logo_urls[0] = (
                "https://cdn.prod.website-files.com/61477f2c24a826836f969afe/"
                "664ffc89ff539b531cc46813_Clay-logo-black-2024.webp"
            )
        else:
            # Last-resort: look at the card's .integration-platforms img in index.html.
            link = INDEX_SOUP.find("a", href=f"{slug}.html")
            card = link.find_parent(class_="integration-card") if link else None
            if card is not None:
                plat = card.select_one(".integration-platforms img")
                if plat and plat.get("src"):
                    logo_urls[0] = plat["src"].strip() or None
    if len(logo_urls) >= 1 and logo_urls[0]:
        logo_primary = logo_urls[0]
    if len(logo_urls) >= 2 and logo_urls[1]:
        logo_secondary = logo_urls[1]

    is_clay_only = slug in TEMPLATES_CLAY_ONLY
    is_zapier_coming_soon = slug in TEMPLATES_TABBED_ZAPIER_COMING_SOON

    # Defaults.
    zapier_url = zapier_callout = zapier_steps = zapier_trouble = ""
    make_url = make_callout = make_steps = make_trouble = ""
    n8n_url = n8n_callout = n8n_steps = n8n_trouble = ""
    clay_url = clay_callout = clay_steps = clay_trouble = ""

    if is_clay_only:
        # The whole page has a single .steps-container, no tab-content wrapper.
        container = soup.select_one(".steps-container")
        if container is not None:
            clay_url = extract_template_url(container, "clay")
            clay_callout = extract_callout_description(container)
            clay_steps = extract_steps_html(container)
            clay_trouble = extract_troubleshooting_html(container)
        media = extract_media_files(soup, [container])
    else:
        zapier_tab = find_tab(soup, "tab-zapier")
        make_tab = find_tab(soup, "tab-make")
        n8n_tab = find_tab(soup, "tab-n8n")

        if is_zapier_coming_soon:
            zapier_url = ""
            zapier_callout = "Coming Soon — use Make or n8n tab instead"
            zapier_steps = ""
            zapier_trouble = ""
        elif zapier_tab is not None:
            if is_coming_soon(zapier_tab):
                zapier_url = ""
                zapier_callout = "Coming Soon — use Make or n8n tab instead"
                zapier_steps = ""
                zapier_trouble = ""
            else:
                zapier_url = extract_template_url(zapier_tab, "zapier")
                zapier_callout = extract_callout_description(zapier_tab)
                zapier_steps = extract_steps_html(zapier_tab)
                zapier_trouble = extract_troubleshooting_html(zapier_tab)

        if make_tab is not None:
            make_url = extract_template_url(make_tab, "make")
            make_callout = extract_callout_description(make_tab)
            make_steps = extract_steps_html(make_tab)
            make_trouble = extract_troubleshooting_html(make_tab)

        if n8n_tab is not None:
            n8n_url = extract_template_url(n8n_tab, "n8n")
            n8n_callout = extract_callout_description(n8n_tab)
            n8n_steps = extract_steps_html(n8n_tab)
            n8n_trouble = extract_troubleshooting_html(n8n_tab)

        media = extract_media_files(soup, [zapier_tab, make_tab, n8n_tab])

    row = {
        "slug": slug,
        "card_title": card_title,
        "hero_title": hero_title,
        "hero_description": hero_description,
        "apps": apps,
        "category": category,
        "status": "active",
        "logo_primary_url": logo_primary,
        "logo_secondary_url": logo_secondary,
        "zapier_template_url": zapier_url,
        "zapier_callout_description": zapier_callout,
        "zapier_steps_html": zapier_steps,
        "zapier_troubleshooting_html": zapier_trouble,
        "make_template_url": make_url,
        "make_callout_description": make_callout,
        "make_steps_html": make_steps,
        "make_troubleshooting_html": make_trouble,
        "n8n_template_url": n8n_url,
        "n8n_callout_description": n8n_callout,
        "n8n_steps_html": n8n_steps,
        "n8n_troubleshooting_html": n8n_trouble,
        "clay_template_url": clay_url,
        "clay_callout_description": clay_callout,
        "clay_steps_html": clay_steps,
        "clay_troubleshooting_html": clay_trouble,
        "media_files": media,
    }
    return row


# ------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------

def main():
    rows = []
    for slug in TEMPLATES:
        try:
            rows.append(build_row(slug))
        except Exception as e:
            print(f"ERROR parsing {slug}: {e}", file=sys.stderr)
            raise

    with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=COLUMNS,
            quoting=csv.QUOTE_ALL,
            extrasaction="raise",
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    # ---------------- validation ----------------
    with open(OUT_PATH, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        all_rows = list(reader)
    header = all_rows[0]
    data_rows = all_rows[1:]
    print(f"CSV path: {OUT_PATH}")
    print(f"File size bytes: {os.path.getsize(OUT_PATH)}")
    print(f"Total rows (incl. header): {len(all_rows)}")
    print(f"Data rows: {len(data_rows)}")
    print(f"Columns in header: {len(header)}")
    bad = [i for i, row in enumerate(all_rows) if len(row) != len(COLUMNS)]
    if bad:
        print(f"!!! Rows with wrong column count: {bad}")
    else:
        print("All rows have exactly 26 fields.")

    # Avg length per column.
    col_totals = [0] * len(COLUMNS)
    for r in data_rows:
        for i, v in enumerate(r):
            col_totals[i] += len(v)
    print("\nAverage field length per column:")
    for name, total in zip(COLUMNS, col_totals):
        avg = total / len(data_rows) if data_rows else 0
        print(f"  {name:32s} {avg:8.1f}")

    # First 200 chars of each column for airtable.
    airtable_row = None
    for r in data_rows:
        if r and r[0] == "airtable-fullenrich":
            airtable_row = r
            break
    if airtable_row is not None:
        print("\nFirst 200 chars of each column (airtable-fullenrich):")
        for name, val in zip(COLUMNS, airtable_row):
            snip = val[:200].replace("\n", "\\n")
            print(f"  {name}: {snip}")


if __name__ == "__main__":
    main()
