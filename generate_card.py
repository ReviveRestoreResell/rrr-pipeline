#!/usr/bin/env python3
"""
generate_card.py  —  RRR Intake Card Generator (template-based v2)
Loads card_template.html and substitutes {{TOKEN}} placeholders from JSON data.

Usage:
    python generate_card.py path/to/SKU.json
    python generate_card.py --batch path/to/folder/
    python generate_card.py --batch path/to/folder/ --batch-label "NWLG Batch 1"

Output: items/{SKU}.html  (relative to this script)
"""

import json
import hashlib
import ntpath
import os
import re
import shutil
import sys
import glob
from datetime import datetime

# ── Manifest constants ─────────────────────────────────────────────────────

MANIFEST_FILENAME = "_render_manifest.json"


# ── Helpers ────────────────────────────────────────────────────────────────

def get(obj, *keys, default=None):
    """Safe nested dict access."""
    for k in keys:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(k, None)
        if obj is None:
            return default
    return obj if obj is not None else default


def esc(s):
    """HTML-escape a string value."""
    if s is None:
        return ""
    s = str(s)
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


def fmt_price(val, fallback="—"):
    """Format a numeric price as $X or $X.XX."""
    if val is None:
        return fallback
    try:
        f = float(val)
        if f == int(f):
            return f"${int(f)}"
        return f"${f:.2f}"
    except (TypeError, ValueError):
        return str(val)


def fmt_price_raw(val, fallback=""):
    """Format a numeric price as X.XX (no dollar sign) for data-value attrs."""
    if val is None:
        return fallback
    try:
        f = float(val)
        return f"{f:.2f}"
    except (TypeError, ValueError):
        return str(val)


def label_to_slug(label):
    """Convert a batch label like 'NWLG Batch 1' to 'nwlg-batch-1'."""
    return label.lower().replace(" ", "-")


def atomic_write_text(path, text, encoding="utf-8"):
    """Write text to path atomically: tmp → fsync → validate non-empty → os.replace.

    DO NOT use shutil.move — on Windows it falls back to copy+delete when the
    destination exists, which is not atomic and lets OneDrive sync corrupt the file.
    """
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if os.path.getsize(tmp) == 0:
            os.remove(tmp)
            raise IOError(f"atomic_write_text: output was empty, aborting write to {path}")
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def atomic_write_json(path, data, encoding="utf-8"):
    """Write JSON to path atomically: tmp → fsync → round-trip validate → os.replace.

    DO NOT use shutil.move — see atomic_write_text for rationale.
    """
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding=encoding) as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        # Validate round-trip
        with open(tmp, "r", encoding=encoding) as f:
            json.load(f)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def get_basename(path):
    """Extract filename from a path that may use Windows backslashes (on Linux)."""
    if not path:
        return ""
    # ntpath.basename handles both / and \ separators
    result = ntpath.basename(path)
    # Fallback to os.path.basename if ntpath gives empty (shouldn't happen)
    return result or os.path.basename(path)


def field_status(value, absent_status="gray"):
    """Return green if value is non-empty, else absent_status."""
    if value and str(value).strip() and str(value).strip() not in ("—", "none", "null"):
        return "green"
    return absent_status


def field_icon(status):
    """Return HTML icon char for a status."""
    if status == "green":
        return "&#10003;"
    if status == "yellow":
        return "&#9888;"
    if status == "red":
        return "&#10007;"
    return "&minus;"


def copyblock_class(value):
    """Return extra class for copyblock when value is empty."""
    if not value or str(value).strip() in ("—", "", "none"):
        return "pending"
    return ""


# ── Condition mapping ──────────────────────────────────────────────────────

CONDITION_DISPLAY_MAP = {
    "NWT": "New with Tags",
    "NWOT": "New without Tags",
    "GPO": "Pre-Owned - Good",
    "VGPO": "Pre-Owned - Very Good",
    "FPO": "Pre-Owned - Acceptable",
    "Pre-Loved": "Pre-Loved",
    "Fair": "Fair",
}

# Fix 4 — eBay condition map (canonical eBay display strings)
EBAY_CONDITION_MAP = {
    "NWT": "New with tags",
    "NWOT": "New without tags",
    "GPO": "Pre-owned — Good",
    "VGPO": "Pre-owned — Very Good",
    "FPO": "Pre-owned — Acceptable",
    "Pre-Loved": "Pre-owned — Good",
    "Fair": "Pre-owned — Acceptable",
}

def condition_display(label):
    return CONDITION_DISPLAY_MAP.get(label, label or "—")

def ebay_condition_display(label):
    return EBAY_CONDITION_MAP.get(label, condition_display(label))


# ── Measurement helpers ─────────────────────────────────────────────────────

MEAS_LABELS = {
    'bust': 'Pit to Pit',
    'waist': 'Waist',
    'hip': 'Hip',
    'length': 'Length',
    'inseam': 'Inseam',
    'sleeve': 'Sleeve',
    'rise': 'Rise',
    'leg_opening': 'Leg Opening',
    'shoulder': 'Shoulder',
    'chest': 'Chest',
    'pit_to_pit': 'Pit to Pit',
    'shoulder_to_hem': 'Shoulder to Hem',
}

# Ordered display sequence for measurements
MEAS_ORDER = [
    'bust', 'chest', 'pit_to_pit', 'shoulder_to_hem', 'shoulder',
    'waist', 'hip', 'length', 'sleeve', 'inseam', 'rise', 'leg_opening',
]


def meas_val(meas_values, key):
    """Extract a measurement value as string or empty string."""
    m = meas_values.get(key, {})
    if isinstance(m, dict):
        v = m.get("in")
    else:
        v = m
    if v is None:
        return ""
    # Guard: if value is not numeric (e.g. a text note), skip it
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    if f == int(f):
        return str(int(f))
    return str(f)


def meas_badge(meas_values):
    """Build a compact measurement badge string for the sband."""
    parts = []
    order = ["bust", "waist", "hip", "length", "inseam", "sleeve", "rise", "leg_opening", "shoulder", "chest"]
    labels = {
        "bust": "Bust", "waist": "Waist", "hip": "Hip", "length": "Length",
        "inseam": "Inseam", "sleeve": "Sleeve", "rise": "Rise",
        "leg_opening": "Leg", "shoulder": "Shoulder", "chest": "Chest"
    }
    for k in order:
        if k in meas_values:
            v = meas_val(meas_values, k)
            if v:
                parts.append(f"{labels.get(k, k.title())} {v}&Prime;")
    return " &middot; ".join(parts) if parts else "—"


def build_measurement_rows_html(sku, meas_values, captured_date=""):
    """Build dynamic measurement rows HTML for any garment type."""
    if not meas_values:
        return (
            '<div class="row" data-field="ref.measurements" data-status="gray" data-value="">'
            '<div class="k">Measurements</div>'
            '<div class="v"><div class="copyblock pending"><span class="cb-val">No measurements recorded.</span></div></div>'
            '<button class="conf gray" onclick="openField(\'ref.measurements\')">&minus;</button>'
            '</div>'
        )

    rows = []
    source = f"Bench {captured_date}" if captured_date else "Bench"

    # Emit rows in defined order, then any extras not in our order list
    emitted = set()
    ordered_keys = [k for k in MEAS_ORDER if k in meas_values]
    extra_keys = [k for k in meas_values if k not in emitted and k not in ordered_keys]
    all_keys = ordered_keys + extra_keys

    for k in all_keys:
        if k in emitted:
            continue
        emitted.add(k)
        # Skip keys whose value is a non-numeric string (e.g. a text note)
        raw_entry = meas_values.get(k)
        if isinstance(raw_entry, str):
            # It's a freeform string, not a measurement — skip
            continue
        v = meas_val(meas_values, k)
        label = MEAS_LABELS.get(k, k.replace("_", " ").title())
        # Get convention if available
        m = meas_values.get(k, {})
        convention = ""
        if isinstance(m, dict):
            convention = m.get("convention", "")
        label_full = f"{label} ({convention})" if convention else label
        field_id = f"ref.{k}"
        status = "green" if v else "gray"
        icon = field_icon(status)
        display = f"{v} in" if v else "—"
        cb_class = "" if v else " pending"
        rows.append(
            f'<div class="row" data-field="{field_id}" data-status="{status}" '
            f'data-source="{esc(source)}" data-value="{esc(v)}">'
            f'<div class="k">{esc(label_full)}</div>'
            f'<div class="v"><div class="copyblock{cb_class}">'
            f'<span class="cb-val">{esc(display)}</span>'
            f'<button class="cb-btn" onclick="copyVal(this,\'{esc(v)}\')">'
            f'copy</button></div></div>'
            f'<button class="conf {status}" onclick="openField(\'{field_id}\')">{icon}</button>'
            f'</div>'
        )
    return "\n        ".join(rows)


# ── Photo grid builder ─────────────────────────────────────────────────────

def build_photo_grid(sku, photos_data, copy_capture=None):
    """Build the photo-grid HTML for the Photos sband."""
    lines = []

    # Collect all photos
    all_photos = []

    # Check copy_capture photo_inventory first (Phase 2)
    if copy_capture:
        pi = get(copy_capture, "photo_inventory", "photos", default=[])
        for ph in pi:
            fp = ph.get("file_path", "")
            fname = get_basename(fp) if fp else ""
            shot_type = ph.get("shot_type", "")
            if fname:
                all_photos.append((fname, shot_type))

    # Fall back to photos dict
    if not all_photos and photos_data:
        skip_keys = {"folder", "_status", "_migrated_date", "_role_inference",
                     "_missing_imgs", "_note"}
        for k, v in photos_data.items():
            if k in skip_keys or k.startswith("_"):
                continue
            if isinstance(v, dict):
                fname = v.get("file", "")
                if not fname:
                    # Try photo_read key for Batch 4 style
                    fname = ""
                label = k.replace("_", " ").title()
                if fname:
                    all_photos.append((fname, label))

    if not all_photos:
        return '<div style="color:var(--ink-mute);font-style:italic;font-size:13px">No photos recorded.</div>'

    lines.append('<div class="photo-grid" id="photoGrid">')
    for fname, label in all_photos:
        img_src = f"../thumbnails/{sku}/{fname}"
        lines.append(
            f'<a class="photo" href="{img_src}" target="_blank" onclick="openPhotoLB(this.href,this.querySelector(\'.ph-label\').textContent);return false;">'
            f'<img loading="lazy" src="{img_src}" alt="{esc(label)}" />'
            f'<div class="ph-label">{esc(label)}</div>'
            f'</a>'
        )
    lines.append('</div>')
    return "\n".join(lines)


# ── Flags builder ──────────────────────────────────────────────────────────

def build_flags_html(compliance_flags, item_flags, copy_capture_flags):
    """Build the flags HTML for the Open Items sband."""
    all_flags = []

    # Compliance flags (strings or dicts)
    for f in (compliance_flags or []):
        if isinstance(f, dict):
            all_flags.append(f)
        else:
            all_flags.append({"field": str(f), "reason": "", "severity": "warn"})

    # Item flags
    for f in (item_flags or []):
        if isinstance(f, dict):
            all_flags.append(f)
        elif f and f not in [x.get("field") for x in all_flags if isinstance(x, dict)]:
            all_flags.append({"field": str(f), "reason": "", "severity": "warn"})

    # Copy capture flags
    for f in (copy_capture_flags or []):
        if isinstance(f, dict):
            sev = f.get("severity", "info")
            field = f.get("field", "")
            reason = f.get("reason", "")
            all_flags.append({"field": field, "reason": reason, "severity": sev})

    if not all_flags:
        return '<div style="color:#065f46;font-size:13px;padding:4px 0">&#10003; No open items.</div>'

    lines = []
    for f in all_flags:
        if isinstance(f, dict):
            sev = f.get("severity", "warn")
            field = f.get("field", "")
            reason = f.get("reason", "")
            sev_label = sev.upper()
            field_html = f'<span class="flag-link" onclick="openField(\'{esc(field)}\')">{esc(field)}</span>' if field else ""
            reason_html = f" — {esc(reason)}" if reason else ""
            lines.append(
                f'<div class="flag-item" data-field="{esc(field)}">'
                f'<span class="flag-sev {sev}">{sev_label}</span>'
                f'<div>{field_html}{reason_html}</div>'
                f'</div>'
            )
        else:
            lines.append(
                f'<div class="flag-item">'
                f'<span class="flag-sev warn">WARN</span>'
                f'<div>{esc(str(f))}</div>'
                f'</div>'
            )
    return "\n      ".join(lines)


# ── Nav bar builder ────────────────────────────────────────────────────────

NAV_CSS = """
.nav-bar{position:sticky;top:0;z-index:100;background:#0f172a;display:flex;align-items:center;justify-content:space-between;padding:8px 18px;gap:12px;font-size:13px}
.nav-bar a,.nav-bar span{color:#e2e8f0;text-decoration:none;font-weight:500;white-space:nowrap}
.nav-bar a:hover{color:#fff;text-decoration:underline}
.nav-bar .nav-pos{color:#94a3b8;font-size:12px;flex:0 0 auto}
.nav-bar .nav-disabled{color:#475569;cursor:default;pointer-events:none}
"""

def build_nav_bar(nav, batch_slug=None):
    """Build sticky prev/next nav bar."""
    if not nav:
        return ""
    prev_sku = nav.get("prev_sku")
    next_sku = nav.get("next_sku")
    pos = nav.get("pos", 1)
    total = nav.get("total", 1)

    prev_html = (f'<a href="../items/{esc(prev_sku)}.html">&#8592; Prev</a>'
                 if prev_sku else '<span class="nav-disabled">&#8592; Prev</span>')
    next_html = (f'<a href="../items/{esc(next_sku)}.html">Next &#8594;</a>'
                 if next_sku else '<span class="nav-disabled">Next &#8594;</span>')
    index_href = f"../batches/{esc(batch_slug)}.html" if batch_slug else "../index.html"
    pos_html = f'<span class="nav-pos"><a href="{index_href}" style="color:#94a3b8">Index</a> &nbsp;·&nbsp; {pos} of {total}</span>'

    return f'<nav class="nav-bar">{prev_html}{pos_html}{next_html}</nav>\n'


# ── Flow bar builder ────────────────────────────────────────────────────────

def build_flow_classes(intake_meta):
    """
    Determine FLOW step classes (done/current/pending) from intake_meta.

    Steps: Sourcing, Intake, Intake Photos, Photography, Listing, Published, Sold
    Logic (Fix 3):
    - Phase 1 or phase2_complete=False: Sourcing=done, Intake=current, rest=pending
    - Phase 2 complete + photo_migration_complete=True: all up through Photo=done, Listing=current
    - Phase 2 complete + photo_migration_complete=False: Intake=done, Intake Photos=current
    """
    phase = get(intake_meta, "phase", default=1)
    is_p2_complete = intake_meta.get("phase2_complete", False)
    photo_done = intake_meta.get("photo_migration_complete", False)

    if phase == 1 or not is_p2_complete:
        # Sourcing=done, Intake=current
        return {
            "FLOW_SOURCING_CLASS": "done",
            "FLOW_INTAKE_CLASS": "current",
            "FLOW_PHOTO_INTAKE_CLASS": "pending",
            "FLOW_PHOTO_CLASS": "pending",
            "FLOW_LISTING_CLASS": "pending",
            "FLOW_PUBLISHED_CLASS": "pending",
            "FLOW_SOLD_CLASS": "pending",
        }

    # Phase 2 complete
    if is_p2_complete and photo_done:
        # Intake done, Photo done, Listing=current
        return {
            "FLOW_SOURCING_CLASS": "done",
            "FLOW_INTAKE_CLASS": "done",
            "FLOW_PHOTO_INTAKE_CLASS": "done",
            "FLOW_PHOTO_CLASS": "done",
            "FLOW_LISTING_CLASS": "current",
            "FLOW_PUBLISHED_CLASS": "pending",
            "FLOW_SOLD_CLASS": "pending",
        }
    else:
        # Intake done, Photo=current (Intake Photos step)
        return {
            "FLOW_SOURCING_CLASS": "done",
            "FLOW_INTAKE_CLASS": "done",
            "FLOW_PHOTO_INTAKE_CLASS": "current",
            "FLOW_PHOTO_CLASS": "pending",
            "FLOW_LISTING_CLASS": "pending",
            "FLOW_PUBLISHED_CLASS": "pending",
            "FLOW_SOLD_CLASS": "pending",
        }


# ── Copy-capture sband builder ────────────────────────────────────────────

def _str_val(v):
    """Extract .value if dict, else return as string."""
    if isinstance(v, dict):
        return str(v.get("value") or "")
    return str(v) if v is not None else ""


def _list_vals(lst):
    """Extract list of .value strings from a list of dicts or strings."""
    if not lst:
        return []
    out = []
    for item in lst:
        if isinstance(item, dict):
            v = item.get("value")
            if v:
                out.append(str(v))
        elif item:
            out.append(str(item))
    return out


def _cc_row(field_id, label, value, source="photo_agent", copyable=True):
    """Build a single .row block matching the existing card row pattern."""
    val_str = str(value) if value else ""
    status = "green" if (val_str and val_str.strip() not in ("—", "", "None", "null")) else "gray"
    icon = field_icon(status)
    cb_cls = "" if status == "green" else " pending"
    disp = esc(val_str) if val_str else "—"
    safe_val = esc(val_str)
    copy_btn = f'<button class="cb-btn" onclick="copyVal(this,\'{safe_val}\')">copy</button>' if copyable and val_str else ""
    return (
        f'<div class="row" data-field="{field_id}" data-status="{status}" '
        f'data-source="{esc(source)}" data-value="{safe_val}">'
        f'<div class="k">{esc(label)}</div>'
        f'<div class="v"><div class="copyblock{cb_cls}"><span class="cb-val">{disp}</span>{copy_btn}</div></div>'
        f'<button class="conf {status}" onclick="openField(\'{field_id}\')">{icon}</button>'
        f'</div>\n'
    )


def build_copy_capture_sband_html(sku, copy_capture, is_p2):
    """Build the Photo Intake sband using the standard .row block pattern."""
    if not is_p2 or not copy_capture:
        return (
            '<div class="sband" id="sbandCopyCap">'
            '<div class="sband-hdr" onclick="toggleBand(this)">'
            '<span class="sband-arrow">&#9658;</span>'
            '<span class="sband-label">Photo Intake</span>'
            '<span class="sband-badge" style="color:#dc2626">Not yet captured</span>'
            '</div>'
            '<div class="sband-body"><p style="color:var(--ink-mute);font-size:13px;margin:0">'
            'Photo intake has not been run for this item.</p></div>'
            '</div>'
        )

    cc = copy_capture

    # ── Pull values ──────────────────────────────────────────────────────
    color_family   = _str_val(get(cc, "color", "family"))
    color_specific = _str_val(get(cc, "color", "specific"))
    color_disp     = (f"{color_family} · {color_specific}"
                      if color_specific and color_specific != color_family
                      else color_family) or "—"
    color_sec_list = get(cc, "color", "secondary", default=[]) or []
    color_sec      = ", ".join(_list_vals(color_sec_list)) if color_sec_list else ""

    pattern_type  = _str_val(get(cc, "pattern", "type")) or "—"
    pattern_scale = _str_val(get(cc, "pattern", "scale")) or ""
    null_like = {"None", "null", ""}
    pattern_disp  = (f"{pattern_type} · {pattern_scale}"
                     if pattern_scale and pattern_scale not in null_like
                     else pattern_type)

    silhouette   = _str_val(get(cc, "silhouette", "value")) or "—"

    closure_type = _str_val(get(cc, "closures", "type")) or "—"
    closure_loc  = _str_val(get(cc, "closures", "location")) or ""
    closure_det  = (cc.get("closures", {}).get("details", "")
                    if isinstance(cc.get("closures"), dict) else "")
    closure_disp = f"{closure_type} · {closure_loc}" if closure_loc else closure_type

    hw_present  = get(cc, "hardware", "present", default=False)
    hw_finish   = _str_val(get(cc, "hardware", "finish")) if hw_present else ""
    hw_features = (cc.get("hardware", {}).get("features", [])
                   if isinstance(cc.get("hardware"), dict) else [])

    detailing = cc.get("detailing", []) or []

    aesthetic    = cc.get("aesthetic", {}) or {}
    primary_tags = _list_vals(aesthetic.get("primary_tags", []))
    era          = _str_val(aesthetic.get("era")) or "—"
    vibe         = _list_vals(aesthetic.get("vibe", []))
    tier_gates   = aesthetic.get("tier_gate_results", {}) or {}

    use_cases = _list_vals(cc.get("use_cases", []) or [])

    photo_inv   = cc.get("photo_inventory", {}) or {}
    photo_count = photo_inv.get("count", 0)
    photos_list = photo_inv.get("photos", []) or []

    cc_flags = cc.get("flags", []) or []

    # ── Badge ──────────────────────────────────────────────────────────────
    badge_parts = []
    if primary_tags:
        badge_parts.append(f"{len(primary_tags)} aesthetic{'s' if len(primary_tags) != 1 else ''}")
    if photo_count:
        badge_parts.append(f"{photo_count} photo{'s' if photo_count != 1 else ''}")
    badge = " &middot; ".join(badge_parts) if badge_parts else "captured"

    # ── Build row blocks ──────────────────────────────────────────────────
    rows = ""

    # Visual properties section
    rows += '<h4 style="margin-top:0">Visual Properties</h4>\n'
    rows += _cc_row("cc.color.family",   "Color",          color_disp)
    if color_sec:
        rows += _cc_row("cc.color.secondary", "Secondary Color", color_sec)
    rows += _cc_row("cc.pattern",        "Pattern",        pattern_disp)
    rows += _cc_row("cc.silhouette",     "Silhouette",     silhouette)
    rows += _cc_row("cc.closures",       "Closures",       closure_disp)
    if closure_det:
        rows += _cc_row("cc.closures.details", "Closure Note", closure_det)
    if hw_finish:
        rows += _cc_row("cc.hardware.finish",  "Hardware Finish", hw_finish)
    if hw_features:
        rows += _cc_row("cc.hardware.features", "Hardware Features",
                        ", ".join(hw_features))
    if detailing:
        rows += _cc_row("cc.detailing", "Detailing", ", ".join(detailing))
    rows += _cc_row("cc.aesthetic.era", "Era", era)

    # Aesthetics section
    rows += '<h4>Aesthetics &amp; Style</h4>\n'
    if primary_tags:
        rows += _cc_row("cc.aesthetic.primary_tags", "Aesthetic Tags",
                        ", ".join(primary_tags))
    if vibe:
        rows += _cc_row("cc.aesthetic.vibe", "Vibe",
                        ", ".join(vibe))
    if use_cases:
        rows += _cc_row("cc.use_cases", "Use Cases",
                        ", ".join(use_cases))

    # Tier gate rejects (shown as individual rows)
    rejected = {k: v for k, v in tier_gates.items() if "REJECTED" in str(v).upper()}
    if rejected:
        rows += '<h4>Tier Gate — Rejected</h4>\n'
        for tag, reason in rejected.items():
            short_reason = str(reason).split("—")[0].strip() if "—" in str(reason) else str(reason)
            rows += _cc_row(f"cc.gate.{esc(tag)}", esc(tag),
                            short_reason, source="tier_gate")

    # Agent flags
    if cc_flags:
        rows += f'<h4>Agent Flags ({len(cc_flags)})</h4>\n'
        for f in cc_flags:
            if isinstance(f, dict):
                field = f.get("field", "")
                reason = f.get("reason", "")
                rows += _cc_row(f"cc.flag.{esc(field)}", esc(field),
                                reason, source=f"photo_agent · {f.get('severity','info')}")

    # Photo reads — collapsible details per photo
    if photos_list:
        rows += f'<h4>Photo Reads ({len(photos_list)})</h4>\n'
        for ph in photos_list:
            fname = esc(ph.get("file", ""))
            shot  = esc(ph.get("shot_type", ""))
            read  = esc(ph.get("photo_read", ""))
            label = f"{shot} — {fname}" if shot else fname
            rows += (
                f'<details class="cc-photo-read">'
                f'<summary>{label}</summary>'
                f'<div class="cc-read-body">{read}</div>'
                f'</details>\n'
            )

    return (
        f'<div class="sband" id="sbandCopyCap">\n'
        f'  <div class="sband-hdr" onclick="toggleBand(this)">\n'
        f'    <span class="sband-arrow">&#9658;</span>\n'
        f'    <span class="sband-label">Photo Intake</span>\n'
        f'    <span class="sband-badge">{badge}</span>\n'
        f'  </div>\n'
        f'  <div class="sband-body">\n'
        f'{rows}'
        f'  </div>\n'
        f'</div>'
    )


# ── Template renderer ──────────────────────────────────────────────────────

def load_template(script_dir):
    tpl_path = os.path.join(script_dir, "card_template.html")
    with open(tpl_path, "r", encoding="utf-8") as f:
        return f.read()


def _pstatus(val, fallback_status="gray"):
    """Green if val present, else fallback_status."""
    return "green" if (val and str(val).strip() and str(val).strip() not in ("—", "none", "null")) else fallback_status


def _picon(status):
    return field_icon(status)


def _pcb(val):
    return copyblock_class(val)


def build_card(data, template, nav=None, batch_slug=None, generated_date=None):
    """Render one card by substituting {{TOKEN}} values into the template."""

    sku = data.get("sku", "Unknown")
    intake_meta = data.get("intake_meta", {})
    phase = intake_meta.get("phase", 1)
    is_p2 = (phase == 2)

    identity = data.get("identity", {})
    brand = get(identity, "brand", "value", default="")
    brand_verification_source = get(identity, "brand", "verification_source",
                                    default=get(identity, "brand", "status_source", default=""))
    model_val = get(identity, "model", "value", default="")
    rn_val = get(identity, "id_codes", "rn", default="")
    style_val = get(identity, "id_codes", "style", default="")
    garment_type = (get(identity, "garment_type")
                    or get(identity, "category_internal_subcategory")
                    or "")
    category = get(identity, "category_internal", default="")
    working_title = get(identity, "working_title", default="")
    vendoo_category = (get(identity, "category_customer_facing_recommendation", default="")
                       or get(identity, "category_customer_facing", default="")
                       or get(identity, "category_internal", default="—"))

    sizing = data.get("sizing", {})
    size_tag = sizing.get("size_tag", "")

    condition_data = data.get("condition", {})
    condition_label = condition_data.get("label", "")

    descriptors = data.get("descriptors", {})
    # Color: prefer copy_capture > descriptors
    if is_p2:
        color_primary = (get(data, "copy_capture", "color", "family", "value")
                         or descriptors.get("color")
                         or descriptors.get("color_observed", ""))
        color_secondary_list = get(data, "copy_capture", "color", "secondary", default=[]) or []
        if isinstance(color_secondary_list, str):
            color_secondary = color_secondary_list
        elif color_secondary_list and isinstance(color_secondary_list[0], dict):
            color_secondary = color_secondary_list[0].get("value", "")
        elif color_secondary_list:
            color_secondary = str(color_secondary_list[0])
        else:
            color_secondary = ""
    else:
        color_primary = descriptors.get("color_observed", "") or descriptors.get("color", "")
        color_secondary = ""

    material_pct = descriptors.get("material_pct", "")
    care_raw = descriptors.get("care", "")
    country_of_origin = descriptors.get("country_of_origin", "")
    stretch_flag = descriptors.get("stretch_flag", None)

    measurements = data.get("measurements", {})
    meas_values = measurements.get("values", {})
    captured_date = intake_meta.get("captured_date", "")

    commerce = data.get("commerce", {})
    pricing_data = data.get("pricing", {})
    cog = (pricing_data.get("acquisition_cost")
           or get(data, "commerce", "pricing", "acquisition_cost")
           or get(data, "intake_meta", "cog_backfill", "value"))
    by_platform = get(commerce, "pricing", "by_platform") or {}
    msrp = get(commerce, "pricing", "msrp")
    net = get(commerce, "pricing", "net_projection", "net")
    expected_sold = get(commerce, "pricing", "expected_sold")
    comp_summary = get(commerce, "pricing", "comp_summary", "ebay_sold") or {}
    comp_median = comp_summary.get("median")
    comp_low = comp_summary.get("low")
    comp_high = comp_summary.get("high")

    # Floor and walk-away by platform
    price_ebay_data = by_platform.get("ebay") or {}
    price_posh_data = by_platform.get("poshmark") or {}
    price_depop_data = by_platform.get("depop") or {}

    ebay_list = price_ebay_data.get("list") if price_ebay_data else None
    posh_list = price_posh_data.get("list") if price_posh_data else None
    depop_list = price_depop_data.get("list") if price_depop_data else None
    posh_floor = price_posh_data.get("floor") if price_posh_data else None
    posh_walk = price_posh_data.get("walk_away") if price_posh_data else None
    depop_floor = price_depop_data.get("floor") if price_depop_data else None

    batch = intake_meta.get("batch") or intake_meta.get("batch_name", "")
    phase2_date = intake_meta.get("phase2_date", "")

    copy_capture = data.get("copy_capture", {})
    photos_data = data.get("photos", {})

    # ── Copy outputs (canonical: top-level `copy.*` namespace) ──────────────
    # Reconcile pass 2026-05-13 (`copy_reconcile_40sku_2026-05-10`) landed
    # `copy.title / copy.description.{p1,p2_smc,p3_badges} / copy.keywords.{line,hashtags}`
    # plus an optional `copy.pending[]` array listing dotted-paths still awaited.
    # Rule: if a sub-key is absent (or listed in pending), do NOT render a
    # placeholder — leave the body empty and surface the pending badge instead.
    copy_block = data.get("copy", {}) or {}
    copy_pending_set = set(copy_block.get("pending", []) or [])

    def _copy_state(dotted_path):
        """Return (is_confirmed, text). A field is confirmed iff it's NOT in
        copy.pending[] AND has a non-empty string value at the dotted path."""
        if dotted_path in copy_pending_set:
            return (False, "")
        node = data
        for part in dotted_path.split("."):
            if isinstance(node, dict):
                node = node.get(part, "")
            else:
                return (False, "")
        if isinstance(node, str) and node.strip():
            return (True, node)
        return (False, "")

    title_ok, title_text   = _copy_state("copy.title")
    p1_ok,    p1_text      = _copy_state("copy.description.p1")
    p2_ok,    p2_text      = _copy_state("copy.description.p2_smc")
    p3_ok,    p3_text      = _copy_state("copy.description.p3_badges")
    kw_line_ok, kw_line    = _copy_state("copy.keywords.line")
    kw_hash_ok, kw_hash    = _copy_state("copy.keywords.hashtags")

    # Title+ block UI
    title_plus        = title_text  # empty when pending — no placeholder
    title_cws_class   = "cws-confirmed" if title_ok else "cws-pending"
    title_badge_class = "cws-confirmed" if title_ok else "cws-pending"
    title_badge_text  = "&#10003;&nbsp;Confirmed" if title_ok else "&#9203;&nbsp;Pending"
    title_char_count  = f"{len(title_text)}&nbsp;chars" if title_ok else "&#8212;&nbsp;chars"

    # Description+ block + sub-blocks
    desc_p1     = p1_text   # empty if pending
    desc_p2_txt = p2_text
    desc_p3     = p3_text
    p1_sub_cls   = "cws-subblock-confirmed" if p1_ok else "cws-subblock-pending"
    p1_badge_cls = "cws-subblock-confirmed" if p1_ok else "cws-subblock-pending"
    p1_badge_txt = "&#10003;&nbsp;Confirmed" if p1_ok else "&#9203;&nbsp;Pending"
    p2_sub_cls   = "cws-subblock-confirmed" if p2_ok else "cws-subblock-pending"
    p2_badge_cls = "cws-subblock-confirmed" if p2_ok else "cws-subblock-pending"
    p2_badge_txt = "&#10003;&nbsp;Confirmed" if p2_ok else "&#9203;&nbsp;Pending"
    p3_sub_cls   = "cws-subblock-confirmed" if p3_ok else "cws-subblock-pending"
    p3_badge_cls = "cws-subblock-confirmed" if p3_ok else "cws-subblock-pending"
    p3_badge_txt = "&#10003;&nbsp;Confirmed" if p3_ok else "&#9203;&nbsp;Pending"
    desc_all_ok       = p1_ok and p2_ok and p3_ok
    desc_block_class  = "cws-confirmed" if desc_all_ok else "cws-pending"
    desc_badge_class  = "cws-confirmed" if desc_all_ok else "cws-pending"
    desc_badge_text   = "&#10003;&nbsp;Confirmed" if desc_all_ok else "&#9203;&nbsp;Pending Review"

    # Keyword+ block: P4 assembly = line + " " + hashtags (locked order)
    kw_all_ok = kw_line_ok and kw_hash_ok
    if kw_line_ok and kw_hash_ok:
        kw_text = f"{kw_line} {kw_hash}"
    elif kw_line_ok:
        kw_text = kw_line
    elif kw_hash_ok:
        kw_text = kw_hash
    else:
        kw_text = ""
    kw_block_class = "cws-confirmed" if kw_all_ok else "cws-pending"
    kw_badge_class = "cws-confirmed" if kw_all_ok else "cws-pending"
    kw_badge_text  = "&#10003;&nbsp;Confirmed" if kw_all_ok else "&#9203;&nbsp;Pending Review"

    # Collapsed COPY sband header summary (fixes known issue #1)
    def _ico(ok):
        return "&check;" if ok else "&hellip;"
    copy_sband_badge = (
        f"Title+ {_ico(title_ok)} &nbsp;|&nbsp; "
        f"Desc+ {_ico(desc_all_ok)} &nbsp;|&nbsp; "
        f"KW+ {_ico(kw_all_ok)}"
    )

    # Operational flag — surface latest matching _card_corrections[] entry
    operational_flag_html = ""
    RECONCILE_PASS_ID = "copy_reconcile_40sku_2026-05-10"
    corrections = data.get("_card_corrections", []) or []
    matching_corrections = [c for c in corrections if c.get("pass_id") == RECONCILE_PASS_ID]
    if matching_corrections:
        op_flag = (matching_corrections[-1].get("operational_flag") or "").strip()
        if op_flag:
            operational_flag_html = (
                '<div style="margin:-4px 0 14px;padding:10px 14px;'
                'background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;'
                'color:#78350f;font-size:13px;line-height:1.45;'
                'display:flex;align-items:flex-start;gap:8px">'
                '<span style="font-size:16px;line-height:1.2">&#9888;&#xfe0f;</span>'
                f'<span><strong>Operational flag:</strong> {esc(op_flag)}</span>'
                '</div>'
            )

    # Publish-blocker badge — Rule #135: P2 SMC missing blocks publish
    publish_blocker_html = ""
    if "copy.description.p2_smc" in copy_pending_set:
        publish_blocker_html = (
            '<div style="margin:0 0 14px;padding:10px 14px;'
            'background:#fee2e2;border:1px solid #ef4444;border-radius:8px;'
            'color:#7f1d1d;font-size:13px;line-height:1.45;'
            'display:flex;align-items:center;gap:8px">'
            '<span style="font-size:16px;line-height:1.2">&#128683;</span>'
            '<span><strong>Publish blocked:</strong> P2 SMC awaiting physical-verify Description+ pass.</span>'
            '</div>'
        )

    # Vendoo tags
    tags = []
    for i in range(1, 6):
        t = get(data, "commerce", "tags", f"t{i}", default="")
        tags.append(t)

    # Aspects
    aspect_dept = get(data, "aspects", "department", default="—")
    aspect_size_type = get(data, "aspects", "size_type", default="—")

    # Nickname line
    subtitle_parts = [p for p in [brand, working_title or garment_type, size_tag, color_primary] if p]
    nickname = " · ".join(subtitle_parts)

    # Phase pill HTML
    if is_p2:
        phase_pill_html = '<span class="pill" style="border-color:var(--green-bd);background:var(--green-bg);color:#065f46;"><strong>Phase 2</strong>' + (phase2_date or "") + '</span>'
    else:
        phase_pill_html = '<span class="pill" style="border-color:var(--yellow-bd);background:var(--yellow-bg);color:#854d0e;"><strong>Phase 1</strong></span>'

    # COG
    cog_pill = fmt_price(cog, "—")
    cog_raw = fmt_price_raw(cog, "")
    cog_display = fmt_price(cog, "—")

    # List pill
    list_parts = []
    if ebay_list is not None:
        list_parts.append(f"{fmt_price(ebay_list)} eBay")
    if posh_list is not None:
        list_parts.append(f"{fmt_price(posh_list)} Posh")
    if depop_list is not None:
        list_parts.append(f"{fmt_price(depop_list)} Depop")
    list_pill = " &middot; ".join(list_parts) if list_parts else "—"

    # Net pill
    net_pill = (f"{fmt_price(net)} @ sold") if net is not None else "—"

    # Pricing badge
    pricing_badge = (f"{fmt_price(ebay_list)} eBay &middot; Net {fmt_price(net)} &middot; "
                     f"{get(commerce, 'pricing', 'confidence', default='?')} confidence"
                     if ebay_list is not None else "—")

    # Material for eBay (comma-separated format)
    material_ebay = material_pct.replace(" · ", ", ") if material_pct else ""

    # Stretch value
    if stretch_flag is True:
        stretch_val = "Yes"
    elif stretch_flag is False:
        stretch_val = "No"
    else:
        stretch_val = "—"
    stretch_status = "green" if stretch_flag is not None else "gray"

    # Flags HTML
    compliance_flags = data.get("compliance", {}).get("flags", [])
    item_flags = data.get("flags", [])
    cc_flags = copy_capture.get("flags", []) if copy_capture else []
    flags_html = build_flags_html(compliance_flags, item_flags, cc_flags)

    # Photo grid HTML
    photo_grid_html = build_photo_grid(sku, photos_data, copy_capture if is_p2 else None)

    # Bench session display
    bench_sess = intake_meta.get("bench_session", {})
    operators = bench_sess.get("operators", [])
    bench_session_display = (f"{captured_date} · {' / '.join(operators)}"
                              if operators else captured_date)

    # Tags
    tag_defaults = ["", "", "", "", ""]
    for i, t in enumerate(tags[:5]):
        tag_defaults[i] = t

    # Generated date
    if generated_date is None:
        generated_date = datetime.utcnow().strftime("%Y-%m-%d")

    # Page title
    page_title = f"{esc(sku)} · {esc(brand)} {esc(working_title or garment_type)} · RRR Item Dashboard"

    # Meas badge for sband
    meas_badge_str = meas_badge(meas_values)

    # Measurement rows HTML (Fix 2)
    measurement_rows_html = build_measurement_rows_html(sku, meas_values, captured_date)

    # Flow bar classes (Fix 3)
    flow_classes = build_flow_classes(intake_meta)

    # Comp pricing helpers
    def comp_tok(val, prefix="COMP"):
        s = _pstatus(val)
        raw = fmt_price_raw(val, "") if val is not None else ""
        disp = fmt_price(val, "—")
        blk = _pcb(raw)
        icon = _picon(s)
        return s, raw, disp, blk, icon

    comp_median_s, comp_median_raw, comp_median_disp, comp_median_blk, comp_median_icon = comp_tok(comp_median)
    p75_s = _pstatus(ebay_list)
    p75_blk = _pcb(fmt_price_raw(ebay_list, ""))
    p75_icon = _picon(p75_s)
    expected_s = _pstatus(expected_sold)
    expected_raw = fmt_price_raw(expected_sold, "")
    expected_disp = fmt_price(expected_sold, "—")
    expected_blk = _pcb(expected_raw)
    expected_icon = _picon(expected_s)

    # Comp range
    if comp_low is not None and comp_high is not None:
        comp_range_raw = f"{comp_low}-{comp_high}"
        comp_range_disp = f"${comp_low} – ${comp_high}"
        comp_range_s = "green"
    else:
        comp_range_raw = ""
        comp_range_disp = "—"
        comp_range_s = "gray"
    comp_range_blk = _pcb(comp_range_raw)
    comp_range_icon = _picon(comp_range_s)

    # Platform floor/walk helpers
    def price_tok(val):
        s = _pstatus(val)
        raw = fmt_price_raw(val, "")
        disp = fmt_price(val, "—")
        blk = _pcb(raw)
        icon = _picon(s)
        return s, raw, disp, blk, icon

    pf_s, pf_raw, pf_disp, pf_blk, pf_icon = price_tok(posh_floor)
    pw_s, pw_raw, pw_disp, pw_blk, pw_icon = price_tok(posh_walk)
    df_s, df_raw, df_disp, df_blk, df_icon = price_tok(depop_floor)

    # Color tokens
    cp_status = _pstatus(color_primary, "yellow")
    cs_val = color_secondary or ""
    cs_class = "pending" if not cs_val else ""
    cs_display = cs_val or "none"

    # RN tokens
    rn_status = _pstatus(rn_val)
    rn_icon = _picon(rn_status)
    rn_block = _pcb(rn_val)

    # Style tokens
    style_status = _pstatus(style_val)
    style_icon = _picon(style_status)
    style_block = _pcb(style_val)
    style_display = style_val or "—"

    # Model tokens
    model_display = model_val or "—"
    model_status = _pstatus(model_val, "yellow")
    model_icon = _picon(model_status)
    model_block = _pcb(model_val)

    # Material tokens
    mat_status = _pstatus(material_pct)
    mat_icon = _picon(mat_status)
    mat_block = _pcb(material_pct)

    # Care tokens
    care_status = _pstatus(care_raw)
    care_icon = _picon(care_status)
    care_block = _pcb(care_raw)
    care_select_class = "pending" if not care_raw else ""

    # Country tokens
    coo_val = country_of_origin or "—"
    coo_status = _pstatus(country_of_origin)
    coo_icon = _picon(coo_status)
    coo_select_class = "pending" if not country_of_origin else ""

    # Fix 6 — Pattern
    if is_p2:
        pattern_val = (get(data, "copy_capture", "pattern", "type", "value", default="")
                       or descriptors.get("pattern_observed", ""))
        pattern_source = get(data, "copy_capture", "pattern", "type", "source", default="")
    else:
        pattern_val = descriptors.get("pattern_observed", "")
        pattern_source = "descriptors"
    if not pattern_val:
        pattern_val = "—"
    if not pattern_source:
        pattern_source = "descriptors"
    pattern_status = _pstatus(pattern_val)

    # Fix 5 — Shipping / packaging
    packed_weight_value = get(data, "commerce", "packed_weight", "value", default="")
    packed_weight_unit = get(data, "commerce", "packed_weight", "unit", default="oz")
    packed_weight_source = get(data, "commerce", "packed_weight", "source", default="estimated")
    packed_dim_l = get(data, "commerce", "packed_dim", "l", default="")
    packed_dim_w = get(data, "commerce", "packed_dim", "w", default="")
    packed_dim_h = get(data, "commerce", "packed_dim", "h", default="")
    packed_dim_source = get(data, "commerce", "packed_dim", "source", default="estimated")
    shipping_strategy = get(data, "commerce", "shipping", "strategy", default="Buyer Pays Calculated")

    # Packed weight: split into lbs and oz
    # If unit is oz and value < 16, lbs=0; if value >= 16 convert
    pkg_weight_lbs = "0"
    pkg_weight_oz = str(packed_weight_value) if packed_weight_value != "" else "0"
    if packed_weight_value != "" and packed_weight_unit == "oz":
        try:
            oz_total = float(packed_weight_value)
            lbs = int(oz_total // 16)
            oz = oz_total % 16
            pkg_weight_lbs = str(lbs)
            pkg_weight_oz = str(int(oz) if oz == int(oz) else oz)
        except (TypeError, ValueError):
            pass
    elif packed_weight_value != "" and packed_weight_unit == "lbs":
        try:
            lbs_total = float(packed_weight_value)
            lbs = int(lbs_total)
            oz = round((lbs_total - lbs) * 16)
            pkg_weight_lbs = str(lbs)
            pkg_weight_oz = str(oz)
        except (TypeError, ValueError):
            pass

    pkg_weight_status = "yellow" if packed_weight_value != "" else "gray"
    pkg_dim_status = "yellow" if packed_dim_l != "" else "gray"

    # Vendoo notes
    vendoo_notes_raw = get(data, "compliance", "note", default="") or get(data, "note", default="") or ""
    # Trim long notes for Vendoo internal field — use copy_capture emphasis if available
    # For now map to empty (most items don't have a short Vendoo note)
    vendoo_notes = ""  # Can be populated from a future copy_outputs field
    vn_status = "gray"
    vn_block_class = "pending"
    vn_icon = "&minus;"

    # Tag token helper (Fix 2 - empty tags get gray status)
    def tag_row(n, val):
        s = "green" if (val and val.strip() and val != "—") else "gray"
        icon = _picon(s)
        cb = _pcb(val) if (val and val.strip() and val != "—") else "pending"
        v = val if (val and val.strip() and val != "—") else "—"
        return s, v, cb, icon

    t1s, t1v, t1cb, t1ic = tag_row(1, tag_defaults[0])
    t2s, t2v, t2cb, t2ic = tag_row(2, tag_defaults[1])
    t3s, t3v, t3cb, t3ic = tag_row(3, tag_defaults[2])
    t4s, t4v, t4cb, t4ic = tag_row(4, tag_defaults[3])
    t5s, t5v, t5cb, t5ic = tag_row(5, tag_defaults[4])

    # ── Build tokens dict ──────────────────────────────────────────────────
    tokens = {
        # Core identity
        "SKU":                      sku,
        "PAGE_TITLE":               page_title,
        "NICKNAME":                 esc(nickname),
        "BATCH_NAME":               esc(batch),
        "COG_PILL":                 esc(cog_pill),
        "COG_RAW":                  esc(cog_raw),
        "COG_DISPLAY":              esc(cog_display),
        "LIST_PILL":                list_pill,
        "NET_PILL":                 esc(net_pill),
        "PHASE_PILL_HTML":          phase_pill_html,
        "BRAND_VALUE":              esc(brand) if brand else "—",
        "BRAND_VERIFICATION_SOURCE": esc(brand_verification_source),
        # Fix 1 — Brand source (for vendoo.brand row data-source)
        "BRAND_SOURCE":             esc(get(data, "identity", "brand", "verification_source", default="")
                                        or get(data, "identity", "brand", "status_source", default="")
                                        or get(data, "identity", "brand", "source", default="")),
        # Fix 4 — eBay condition display
        "EBAY_CONDITION_DISPLAY":   esc(ebay_condition_display(condition_label)),
        "EBAY_CONDITION_VALUE":     esc(condition_label),
        "MODEL_VALUE":              esc(model_display),
        "MODEL_STATUS":             model_status,
        "MODEL_BLOCK_CLASS":        model_block,
        "MODEL_ICON":               model_icon,
        "RN_VALUE":                 esc(rn_val) if rn_val else "—",
        "RN_STATUS":                rn_status,
        "RN_BLOCK_CLASS":           rn_block,
        "RN_ICON":                  rn_icon,
        "STYLE_VALUE":              esc(style_display),
        "STYLE_STATUS":             style_status,
        "STYLE_BLOCK_CLASS":        style_block,
        "STYLE_ICON":               style_icon,
        "VENDOO_CATEGORY":          esc(vendoo_category),
        "CATEGORY":                 esc(category),
        "GARMENT_TYPE":             esc(garment_type),
        "SIZE_TAG":                 esc(size_tag),
        "CONDITION_VALUE":          esc(condition_label),
        "CONDITION_DISPLAY":        esc(condition_display(condition_label)),
        # Colors
        "COLOR_PRIMARY":            esc(color_primary) if color_primary else "—",
        "COLOR_PRIMARY_VALUE":      esc(color_primary) if color_primary else "—",
        "COLOR_PRIMARY_STATUS":     cp_status,
        "COLOR_SECONDARY_VALUE":    esc(cs_val),
        "COLOR_SECONDARY_CLASS":    cs_class,
        "COLOR_SECONDARY_DISPLAY":  esc(cs_display),
        # Material / care / country
        "MATERIAL":                 esc(material_pct),
        "MATERIAL_EBAY":            esc(material_ebay),
        "MATERIAL_STATUS":          mat_status,
        "MATERIAL_BLOCK_CLASS":     mat_block,
        "MATERIAL_ICON":            mat_icon,
        "MATERIAL_PCT":             esc(material_pct),
        "CARE_INSTRUCTIONS":        esc(care_raw) if care_raw else "—",
        "CARE_STATUS":              care_status,
        "CARE_ICON":                care_icon,
        "CARE_BLOCK_CLASS":         care_block,
        "CARE_SELECT_CLASS":        care_select_class,
        "COUNTRY_OF_ORIGIN":        esc(country_of_origin) if country_of_origin else "—",
        "COO_STATUS":               coo_status,
        "COO_ICON":                 coo_icon,
        "COO_SELECT_CLASS":         coo_select_class,
        # Stretch
        "STRETCH_VALUE":            esc(stretch_val),
        "STRETCH_STATUS":           stretch_status,
        # Aspects
        "ASPECT_DEPARTMENT":        esc(aspect_dept),
        "ASPECT_SIZE_TYPE":         esc(aspect_size_type),
        # Measurements (Fix 2)
        "MEASUREMENT_ROWS_HTML":    measurement_rows_html,
        "MEAS_BADGE":               meas_badge_str,
        # Legacy single-field tokens (kept for any remaining template refs)
        "MEAS_BUST":                meas_val(meas_values, "bust") or "—",
        "MEAS_LENGTH":              meas_val(meas_values, "length") or "—",
        "MEAS_SLEEVE":              meas_val(meas_values, "sleeve") or "—",
        # Pricing
        "PRICE_EBAY":               esc(fmt_price(ebay_list, "—")),
        "PRICE_EBAY_RAW":           esc(fmt_price_raw(ebay_list, "")),
        "PRICE_POSH":               esc(fmt_price(posh_list, "—")),
        "PRICE_POSH_RAW":           esc(fmt_price_raw(posh_list, "")),
        "PRICE_DEPOP":              esc(fmt_price(depop_list, "—")),
        "PRICE_DEPOP_RAW":          esc(fmt_price_raw(depop_list, "")),
        "PRICE_POSH_FLOOR":         esc(pf_disp),
        "PRICE_POSH_FLOOR_RAW":     esc(pf_raw),
        "PRICE_POSH_FLOOR_STATUS":  pf_s,
        "PRICE_POSH_FLOOR_BLOCK":   pf_blk,
        "PRICE_POSH_FLOOR_ICON":    pf_icon,
        "PRICE_POSH_WALK":          esc(pw_disp),
        "PRICE_POSH_WALK_RAW":      esc(pw_raw),
        "PRICE_POSH_WALK_STATUS":   pw_s,
        "PRICE_POSH_WALK_BLOCK":    pw_blk,
        "PRICE_POSH_WALK_ICON":     pw_icon,
        "PRICE_DEPOP_FLOOR":        esc(df_disp),
        "PRICE_DEPOP_FLOOR_RAW":    esc(df_raw),
        "PRICE_DEPOP_FLOOR_STATUS": df_s,
        "PRICE_DEPOP_FLOOR_BLOCK":  df_blk,
        "PRICE_DEPOP_FLOOR_ICON":   df_icon,
        "MSRP":                     esc(f"${msrp}" if msrp else "—"),
        "MSRP_RAW":                 esc(str(msrp) if msrp else ""),
        "NET_RAW":                  esc(fmt_price_raw(net, "")),
        "NET_DISPLAY":              esc(fmt_price(net, "—")),
        "PRICING_BADGE":            pricing_badge,
        # Comp pricing
        "COMP_MEDIAN":              esc(comp_median_disp),
        "COMP_MEDIAN_RAW":          esc(comp_median_raw),
        "COMP_MEDIAN_STATUS":       comp_median_s,
        "COMP_MEDIAN_BLOCK":        comp_median_blk,
        "COMP_MEDIAN_ICON":         comp_median_icon,
        "COMP_P75_STATUS":          p75_s,
        "COMP_P75_BLOCK":           p75_blk,
        "COMP_P75_ICON":            p75_icon,
        "COMP_RANGE":               esc(comp_range_disp),
        "COMP_RANGE_RAW":           esc(comp_range_raw),
        "COMP_RANGE_STATUS":        comp_range_s,
        "COMP_RANGE_BLOCK":         comp_range_blk,
        "COMP_RANGE_ICON":          comp_range_icon,
        "EXPECTED":                 esc(expected_disp),
        "EXPECTED_RAW":             esc(expected_raw),
        "EXPECTED_STATUS":          expected_s,
        "EXPECTED_BLOCK":           expected_blk,
        "EXPECTED_ICON":            expected_icon,
        # Copy (canonical copy.* namespace, per copy_reconcile_40sku_2026-05-10)
        "TITLE_PLUS_TEXT":          esc(title_plus),
        "TITLE_CWS_CLASS":          title_cws_class,
        "TITLE_BADGE_CLASS":        title_badge_class,
        "TITLE_BADGE_TEXT":         title_badge_text,
        "TITLE_CHAR_COUNT":         title_char_count,
        "DESC_BLOCK_CLASS":         desc_block_class,
        "DESC_BADGE_CLASS":         desc_badge_class,
        "DESC_BADGE_TEXT":          desc_badge_text,
        "DESC_P1_TEXT":             esc(desc_p1),
        "DESC_P1_SUBBLOCK_CLASS":   p1_sub_cls,
        "DESC_P1_BADGE_CLASS":      p1_badge_cls,
        "DESC_P1_BADGE_TEXT":       p1_badge_txt,
        "DESC_P2_TEXT":             esc(desc_p2_txt),
        "DESC_P2_SUBBLOCK_CLASS":   p2_sub_cls,
        "DESC_P2_BADGE_CLASS":      p2_badge_cls,
        "DESC_P2_BADGE_TEXT":       p2_badge_txt,
        "DESC_P3_TEXT":             esc(desc_p3),
        "DESC_P3_SUBBLOCK_CLASS":   p3_sub_cls,
        "DESC_P3_BADGE_CLASS":      p3_badge_cls,
        "DESC_P3_BADGE_TEXT":       p3_badge_txt,
        "KEYWORD_PILLS_HTML":       esc(kw_text),
        "KW_BLOCK_CLASS":           kw_block_class,
        "KW_BADGE_CLASS":           kw_badge_class,
        "KW_BADGE_TEXT":            kw_badge_text,
        "COPY_SBAND_BADGE":         copy_sband_badge,
        "OPERATIONAL_FLAG_HTML":    operational_flag_html,
        "PUBLISH_BLOCKER_BADGE_HTML": publish_blocker_html,
        # Tags (Fix 2 - gray when empty)
        "TAG_1":                    esc(t1v),
        "TAG_1_STATUS":             t1s,
        "TAG_1_ICON":               t1ic,
        "TAG_1_BLOCK":              t1cb,
        "TAG_2":                    esc(t2v),
        "TAG_2_STATUS":             t2s,
        "TAG_2_ICON":               t2ic,
        "TAG_2_BLOCK":              t2cb,
        "TAG_3":                    esc(t3v),
        "TAG_3_STATUS":             t3s,
        "TAG_3_ICON":               t3ic,
        "TAG_3_BLOCK":              t3cb,
        "TAG_4":                    esc(t4v),
        "TAG_4_STATUS":             t4s,
        "TAG_4_ICON":               t4ic,
        "TAG_4_BLOCK":              t4cb,
        "TAG_5":                    esc(t5v),
        "TAG_5_STATUS":             t5s,
        "TAG_5_ICON":               t5ic,
        "TAG_5_BLOCK":              t5cb,
        # Fix 6 — Pattern
        "PATTERN_VALUE":            esc(pattern_val),
        "PATTERN_SOURCE":           esc(pattern_source),
        "PATTERN_STATUS":           pattern_status,
        "PATTERN_ICON":             _picon(pattern_status),
        # Fix 5 — Shipping / packaging
        "PKG_WEIGHT_LBS":           esc(pkg_weight_lbs),
        "PKG_WEIGHT_OZ":            esc(pkg_weight_oz),
        "PKG_WEIGHT_SOURCE":        esc(packed_weight_source),
        "PKG_WEIGHT_STATUS":        pkg_weight_status,
        "PKG_DIM_L":                esc(str(packed_dim_l) if packed_dim_l != "" else ""),
        "PKG_DIM_W":                esc(str(packed_dim_w) if packed_dim_w != "" else ""),
        "PKG_DIM_H":                esc(str(packed_dim_h) if packed_dim_h != "" else ""),
        "PKG_DIM_SOURCE":           esc(packed_dim_source),
        "PKG_DIM_STATUS":           pkg_dim_status,
        "SHIPPING_STRATEGY":        esc(shipping_strategy),
        # Vendoo notes
        "VENDOO_NOTES":             esc(vendoo_notes),
        "VENDOO_NOTES_STATUS":      vn_status,
        "VENDOO_NOTES_BLOCK_CLASS": vn_block_class,
        "VENDOO_NOTES_ICON":        vn_icon,
        # Dates / meta
        "CAPTURED_DATE":            esc(captured_date),
        "GENERATED_DATE":           esc(generated_date),
        "BENCH_SESSION_DISPLAY":    esc(bench_session_display),
        # Flags + photos
        "FLAGS_HTML":               flags_html,
        "PHOTO_GRID_HTML":          photo_grid_html,
        # Photo Intake sband
        "COPY_CAPTURE_SBAND_HTML":  build_copy_capture_sband_html(sku, copy_capture, is_p2),
        # Flow bar (Fix 3)
        **flow_classes,
    }

    # ── Substitute tokens ──────────────────────────────────────────────────
    html = template
    for key, val in tokens.items():
        html = html.replace("{{" + key + "}}", val if val is not None else "")

    # ── Inject nav bar after <body> tag ────────────────────────────────────
    if nav:
        nav_html = build_nav_bar(nav, batch_slug=batch_slug)
        html = html.replace("</style>", NAV_CSS + "\n</style>", 1)
        html = html.replace("<body>", "<body>\n" + nav_html, 1)

    return html


# ── Index / batch index builders (preserved from v1) ─────────────────────

def _write_index(index_path, items):
    """Write index.html as a batch card dashboard.

    Groups items by batch, shows one card per batch with item count,
    ready-light dot tally (red/yellow/green), and a link to the batch page.
    Batches whose name contains "copy" (case-insensitive) are shown without
    ready-light dots — they are copy-workstream cohorts, not intake batches.
    """
    from collections import defaultdict

    # Known display order — any other batches append alphabetically
    BATCH_ORDER = [
        "Batch 4",
        "Batch 5",
        "NWLG Batch 1",
        "Phase1 Migrated April 2026",
        "WERT Batch 1",
    ]

    # Group items by batch label
    batch_map = defaultdict(list)
    for sku, data in items:
        batch = (
            get(data, "intake_meta", "batch_name")
            or get(data, "intake_meta", "batch", default="Unknown")
        )
        batch_map[batch].append((sku, data))

    # Ordered list: known batches first, then any extras alphabetically
    ordered = [b for b in BATCH_ORDER if b in batch_map]
    for b in sorted(batch_map.keys()):
        if b not in ordered:
            ordered.append(b)

    cards = []
    for batch in ordered:
        batch_items = batch_map[batch]
        n = len(batch_items)
        slug = label_to_slug(batch)
        is_copy_cohort = "copy" in batch.lower()

        # Tally ready-lights (skip for copy-workstream cohorts)
        red = yellow = green = 0
        if not is_copy_cohort:
            for _, d in batch_items:
                s = _ready_status(d)
                if s == "green":
                    green += 1
                elif s == "yellow":
                    yellow += 1
                else:
                    red += 1

        # Build dot tally HTML
        dot_parts = []
        if not is_copy_cohort:
            if red:
                dot_parts.append(
                    f'<span class="dc"><span class="rd rd-r"></span>{red}</span>'
                )
            if yellow:
                dot_parts.append(
                    f'<span class="dc"><span class="rd rd-y"></span>{yellow}</span>'
                )
            if green:
                dot_parts.append(
                    f'<span class="dc"><span class="rd rd-g"></span>{green}</span>'
                )
        dots_html = " ".join(dot_parts)

        # Batch description from intake_meta if available
        desc = ""
        for _, d in batch_items:
            bd = (
                get(d, "intake_meta", "batch_desc")
                or get(d, "intake_meta", "batch_description", default="")
            )
            if bd:
                desc = bd
                break

        link_label = "View Cohort" if is_copy_cohort else "View Batch"
        desc_html = f'<p class="card-desc">{esc(desc)}</p>' if desc else ""
        dots_row = f'<div class="card-dots">{dots_html}</div>' if dots_html else ""

        cards.append(f"""\
  <div class="card">
    <div class="card-hd">
      <span class="card-name">{esc(batch)}</span>
    </div>
    <div class="card-count">{n}<span class="card-count-unit"> items</span></div>
    {desc_html}
    {dots_row}
    <a class="card-link" href="batches/{esc(slug)}.html">{link_label} →</a>
  </div>""")

    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    cards_html = "\n".join(cards)

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="robots" content="noindex,nofollow"/>
<title>RRR Intake Cards</title>
<style>
*{{box-sizing:border-box}}
body{{font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;margin:0;padding:0;background:#f1f5f9;color:#111827}}
.hdr{{background:#0f172a;color:#e2e8f0;padding:16px 28px}}
.hdr h1{{margin:0;font-size:18px;font-weight:700;color:#fff}}
.hdr .sub{{font-size:12px;color:#94a3b8;margin-top:2px}}
.wrap{{max-width:1100px;margin:0 auto;padding:32px 22px 60px}}
.section-lbl{{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#94a3b8;margin-bottom:14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:20px 20px 18px;display:flex;flex-direction:column;gap:6px}}
.card-hd{{}}
.card-name{{font-size:15px;font-weight:700;color:#0f172a}}
.card-count{{font-size:28px;font-weight:800;color:#1d4ed8;line-height:1.1}}
.card-count-unit{{font-size:13px;font-weight:400;color:#6b7280}}
.card-desc{{font-size:12px;color:#6b7280;margin:0}}
.card-dots{{display:flex;gap:10px;margin-top:4px;flex-wrap:wrap}}
.dc{{display:inline-flex;align-items:center;gap:4px;font-size:12px;color:#374151;font-variant-numeric:tabular-nums}}
.rd{{display:inline-block;width:10px;height:10px;border-radius:50%}}
.rd-r{{background:#ef4444}}
.rd-y{{background:#f59e0b}}
.rd-g{{background:#22c55e}}
.card-link{{margin-top:8px;font-size:13px;font-weight:600;color:#0369a1;text-decoration:none}}
.card-link:hover{{text-decoration:underline}}
footer{{margin-top:24px;font-size:11px;color:#9ca3af}}
@media(max-width:600px){{.grid{{grid-template-columns:1fr 1fr}}}}
</style>
</head>
<body>
<div class="hdr">
  <h1>RRR Intake Cards</h1>
  <div class="sub">Internal operator view &nbsp;·&nbsp; Revive Restore Resell</div>
</div>
<div class="wrap">
  <div class="section-lbl">Batches</div>
  <div class="grid">
{cards_html}
  </div>
  <footer>For operators: Vaughn &middot; Tracie &middot; Shital &middot; Elle</footer>
</div>
</body>
</html>
"""
    atomic_write_text(index_path, html)


def _copy_completeness(data):
    """Return (count, total) — how many of the 5 RRR copy fields are present-and-nonempty.

    Fields: copy.title, copy.description.{p1, p2_smc, p3_badges}, copy.keywords.line
    """
    paths = (
        ("copy", "title"),
        ("copy", "description", "p1"),
        ("copy", "description", "p2_smc"),
        ("copy", "description", "p3_badges"),
        ("copy", "keywords", "line"),
    )
    n = 0
    for p in paths:
        v = get(data, *p, default="")
        if isinstance(v, str) and v.strip():
            n += 1
    return n, len(paths)



# Matches Tracie's "ready for list" note (case-insensitive).
# Accepts: "ready for list", "ready to list", "ready for listing", "ready to be listed".
_READY_FOR_LIST_RE = re.compile(
    r"\bready\s+(?:for|to)\s+(?:be\s+)?list(?:ing|ed)?\b",
    re.IGNORECASE,
)


def _has_ready_for_list_note(data):
    """True iff Tracie has signaled the item is ready for list.

    Two ways to signal (either is sufficient):
      (a) Top-level boolean: data["ready_for_list"] == True
      (b) The phrase "ready for list" (or variant) appears in ANY string value
          stored under a key whose name contains "note" (case-insensitive),
          anywhere in the JSON tree.
    """
    if isinstance(data, dict) and data.get("ready_for_list") is True:
        return True

    def _scan(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and "note" in k.lower() and isinstance(v, str):
                    if _READY_FOR_LIST_RE.search(v):
                        return True
                if _scan(v):
                    return True
        elif isinstance(node, list):
            for item in node:
                if _scan(item):
                    return True
        return False

    return _scan(data)


def _ready_status(data):
    """Return 'green' | 'yellow' | 'red' for the Ready-for-List light.

      green  = 5/5 copy AND Tracie's ready-for-list note present
      yellow = any copy present (1+ of 5) but not yet ready
      red    = no copy at all (0/5)
    """
    n, total = _copy_completeness(data)
    if n == 0:
        return "red"
    if n == total and _has_ready_for_list_note(data):
        return "green"
    return "yellow"


def _resolve_list_price(data):
    """Best-available list price across batch shapes:
       pricing.list_price -> pricing.card_snapshot.ebay_list -> None."""
    pr = data.get("pricing", {}) if isinstance(data.get("pricing"), dict) else {}
    lp = pr.get("list_price")
    if lp is not None:
        return lp
    snap = pr.get("card_snapshot", {}) if isinstance(pr.get("card_snapshot"), dict) else {}
    return snap.get("ebay_list")


def _write_batch_index(batches_dir, slug, batch_label, items):
    """Write batches/{slug}.html listing all items in this batch."""
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    rows = []
    for sku, data in items:
        brand = get(data, "identity", "brand", "value", default="")
        condition = get(data, "condition", "label", default="")
        copy_n, copy_total = _copy_completeness(data)
        if copy_n == copy_total:
            copy_cls = "copy-badge full"
        elif copy_n >= 3:
            copy_cls = "copy-badge mid"
        elif copy_n >= 1:
            copy_cls = "copy-badge low"
        else:
            copy_cls = "copy-badge empty"
        ready = _ready_status(data)
        ready_title = {
            "green":  "Ready for list \u2014 5/5 copy + Tracie's ready-for-list note",
            "yellow": "Copy work in progress",
            "red":    "Missing copy",
        }[ready]
        price_str = fmt_price(_resolve_list_price(data))
        rows.append(
            f'<tr>'
            f'<td class="ready-cell"><span class="ready-dot ready-{ready}" title="{esc(ready_title)}" aria-label="{esc(ready_title)}"></span></td>'
            f'<td><a href="../items/{esc(sku)}.html">{esc(sku)}</a></td>'
            f'<td>{esc(brand)}</td>'
            f'<td><span class="{copy_cls}">{copy_n}/{copy_total}</span></td>'
            f'<td>{esc(condition)}</td>'
            f'<td>{esc(price_str)}</td>'
            f'</tr>\n'
        )

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="robots" content="noindex,nofollow"/>
<title>{esc(batch_label)} — RRR Intake Cards</title>
<style>
*{{box-sizing:border-box}}
body{{font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;margin:0;padding:0;background:#fafaf9;color:#111827}}
.hdr{{background:#0f172a;color:#e2e8f0;padding:16px 28px;display:flex;align-items:baseline;gap:16px;flex-wrap:wrap}}
.hdr h1{{margin:0;font-size:18px;font-weight:700;color:#fff}}
.hdr .mute{{font-size:12px;color:#94a3b8}}
.back-link{{font-size:12px;color:#94a3b8;text-decoration:none;margin-left:auto;white-space:nowrap}}
.back-link:hover{{color:#fff;text-decoration:underline}}
.wrap{{max-width:1040px;margin:0 auto;padding:24px 22px 60px}}
.stats{{font-size:13px;color:#6b7280;margin-bottom:16px}}
.tbl-wrap{{background:#fff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
thead th{{background:#f8fafc;color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:.5px;font-weight:600;text-align:left;padding:10px 14px;border-bottom:1px solid #e5e7eb}}
tbody tr{{border-bottom:1px solid #f1f5f9}}
tbody tr:last-child{{border-bottom:none}}
tbody tr:hover{{background:#f8fafc}}
td{{padding:9px 14px;vertical-align:middle}}
a{{color:#0369a1;text-decoration:none}}a:hover{{text-decoration:underline}}
.phase-badge{{display:inline-block;font-size:10px;font-weight:700;padding:2px 8px;border-radius:99px;text-transform:uppercase;letter-spacing:.04em}}
.phase-badge.p1{{background:#fefce8;color:#854d0e;border:1px solid #fde68a}}
.phase-badge.p2{{background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0}}
.open-link{{font-weight:600;font-size:12px}}
.copy-badge{{display:inline-block;font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px;letter-spacing:.02em;font-variant-numeric:tabular-nums}}
.copy-badge.full{{background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0}}
.copy-badge.mid{{background:#fefce8;color:#854d0e;border:1px solid #fde68a}}
.copy-badge.low{{background:#fff7ed;color:#9a3412;border:1px solid #fed7aa}}
.copy-badge.empty{{background:#fef2f2;color:#991b1b;border:1px solid #fecaca}}
.ready-cell{{width:28px;padding-left:14px;padding-right:0;text-align:center}}
thead th.ready-th{{width:28px;padding-left:14px;padding-right:0}}
.ready-dot{{display:inline-block;width:12px;height:12px;border-radius:50%;vertical-align:middle;box-shadow:0 0 0 1px rgba(0,0,0,.08) inset}}
.ready-dot.ready-green{{background:#22c55e}}
.ready-dot.ready-yellow{{background:#eab308}}
.ready-dot.ready-red{{background:#ef4444}}
footer{{margin-top:20px;font-size:11px;color:#9ca3af}}
@media(max-width:700px){{
  thead th:nth-child(5),td:nth-child(5){{display:none}}
}}
</style>
</head>
<body>
<div class="hdr">
  <h1>{esc(batch_label)}</h1>
  <span class="mute">{len(items)} items &nbsp;·&nbsp; RRR Intake Cards</span>
  <a class="back-link" href="../index.html">&#8592; All Batches</a>
</div>
<div class="wrap">
<div class="stats">{len(items)} items &nbsp;·&nbsp; Generated {generated_at}</div>
<div class="tbl-wrap">
<table>
<thead>
<tr>
  <th class="ready-th" title="Ready-for-list signal: green=ready, yellow=copy in progress, red=no copy" aria-label="Ready"></th>
  <th>SKU</th>
  <th>Brand</th>
  <th>Copy</th>
  <th>Condition</th>
  <th>Price</th>
</tr>
</thead>
<tbody>
{"".join(rows)}</tbody>
</table>
</div>
<footer>For operators: Vaughn · Tracie · Shital · Elle</footer>
</div>
</body>
</html>
"""
    os.makedirs(batches_dir, exist_ok=True)
    out_path = os.path.join(batches_dir, f"{slug}.html")
    atomic_write_text(out_path, html)
    return out_path


# ── Manifest helpers ───────────────────────────────────────────────────────

def compute_source_hash(json_path):
    """Hash the parsed-and-re-serialized JSON content.
    Immune to whitespace, key-order, and line-ending noise (which is what
    bit us with OneDrive sync re-stamps showing as mtime changes)."""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_renderer_version(script_dir):
    """Short hash of (script + template). Bumps automatically when either
    changes, so check_updates.py knows template fixes invalidate prior renders."""
    script_path = os.path.join(script_dir, "generate_card.py")
    template_path = os.path.join(script_dir, "card_template.html")
    h = hashlib.sha256()
    for p in (script_path, template_path):
        if os.path.isfile(p):
            with open(p, "rb") as f:
                h.update(f.read())
    return h.hexdigest()[:12]


def _manifest_path(items_dir):
    return os.path.join(items_dir, MANIFEST_FILENAME)


def load_manifest(items_dir):
    p = _manifest_path(items_dir)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_manifest(items_dir, manifest):
    p = _manifest_path(items_dir)
    sorted_manifest = {k: manifest[k] for k in sorted(manifest)}
    atomic_write_json(p, sorted_manifest)


def build_manifest_entry(json_path, source_hash, renderer_version):
    source_mtime = datetime.utcfromtimestamp(os.path.getmtime(json_path)).isoformat() + "Z"
    rendered_at = datetime.utcnow().isoformat() + "Z"
    return {
        "source_path": json_path,
        "source_sha256": source_hash,
        "source_mtime": source_mtime,
        "rendered_at": rendered_at,
        "renderer_version": renderer_version,
    }


# ── Main entry points ──────────────────────────────────────────────────────


def generate_one(json_path, output_dir, template, nav=None, batch_slug=None, renderer_version=""):
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    sku = data.get("sku")
    if not sku:
        sku = os.path.splitext(os.path.basename(json_path))[0]
        data["sku"] = sku

    generated_date = datetime.utcnow().strftime("%Y-%m-%d")
    html = build_card(data, template, nav=nav, batch_slug=batch_slug, generated_date=generated_date)

    # Check for unresolved tokens
    import re
    unresolved = re.findall(r'\{\{[A-Z_]+\}\}', html)
    if unresolved:
        unique = sorted(set(unresolved))
        print(f"    WARN {sku}: unresolved tokens: {', '.join(unique)}")

    out_path = os.path.join(output_dir, f"{sku}.html")
    atomic_write_text(out_path, html)

    # Manifest entry — content-hash + provenance for check_updates.py
    source_hash = compute_source_hash(json_path)
    manifest_entry = build_manifest_entry(json_path, source_hash, renderer_version)

    return sku, out_path, data, manifest_entry


def main():
    args = sys.argv[1:]
    if not args:
        print("Usage:")
        print("  python generate_card.py path/to/SKU.json")
        print("  python generate_card.py --batch path/to/folder/")
        print("  python generate_card.py --batch path/to/folder/ --batch-label 'NWLG Batch 1'")
        print("  python generate_card.py --full-index path/to/intake-root/")
        sys.exit(1)

    # ── Full index mode ────────────────────────────────────────────────────────
    # --full-index <intake-root>
    # Walks all immediate subdirectories of the intake root, loads every *.json,
    # and writes a comprehensive index.html covering all batches.
    if args[0] == "--full-index":
        if len(args) < 2:
            print("Error: --full-index requires the intake root path")
            sys.exit(1)
        intake_root = args[1]
        script_dir = os.path.dirname(os.path.abspath(__file__))

        EXCLUDE_DIRS = {"_archive", "_phase1_backup", "_p2_preflight", "github pages", "items"}

        all_items = []
        for entry in sorted(os.scandir(intake_root), key=lambda e: e.name):
            if not entry.is_dir():
                continue
            if entry.name.lower() in EXCLUDE_DIRS or entry.name.startswith("."):
                continue
            for jf in sorted(glob.glob(os.path.join(entry.path, "*.json"))):
                try:
                    with open(jf, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    sku = data.get("sku") or os.path.splitext(os.path.basename(jf))[0]
                    all_items.append((sku, data))
                except Exception as e:
                    print(f"  SKIP {os.path.basename(jf)}: {e}")

        all_items.sort(key=lambda x: x[0])
        index_path = os.path.join(script_dir, "index.html")
        _write_index(index_path, all_items)
        print(f"index.html written — {len(all_items)} items from {intake_root}")
        sys.exit(0)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(script_dir, "items")
    os.makedirs(output_dir, exist_ok=True)

    # Load template once
    template = load_template(script_dir)

    # Render manifest — tracks source content hash + render time per SKU.
    # check_updates.py reads this to know what's truly stale vs. mtime noise.
    renderer_version = compute_renderer_version(script_dir)
    manifest = load_manifest(output_dir)

    if args[0] == "--batch":
        if len(args) < 2:
            print("Error: --batch requires a folder path")
            sys.exit(1)
        folder = args[1]

        # Parse optional --batch-label flag
        batch_label = None
        batch_slug = None
        remaining = args[2:]
        i = 0
        while i < len(remaining):
            if remaining[i] == "--batch-label" and i + 1 < len(remaining):
                batch_label = remaining[i + 1]
                batch_slug = label_to_slug(batch_label)
                i += 2
            else:
                i += 1

        json_files = sorted(glob.glob(os.path.join(folder, "*.json")))
        if not json_files:
            print(f"No .json files found in: {folder}")
            sys.exit(1)

        total = len(json_files)
        ok = []
        errors = []

        def peek_sku(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                return d.get("sku") or os.path.splitext(os.path.basename(path))[0]
            except Exception:
                return os.path.splitext(os.path.basename(path))[0]

        for idx, jf in enumerate(json_files):
            prev_file = json_files[idx - 1] if idx > 0 else None
            next_file = json_files[idx + 1] if idx < total - 1 else None

            nav = {
                "prev_sku": peek_sku(prev_file) if prev_file else None,
                "next_sku": peek_sku(next_file) if next_file else None,
                "pos": idx + 1,
                "total": total,
            }

            try:
                sku, out_path, data, manifest_entry = generate_one(
                    jf, output_dir, template, nav=nav, batch_slug=batch_slug,
                    renderer_version=renderer_version,
                )
                ok.append((sku, data))
                manifest[sku] = manifest_entry
                print(f"  OK  {sku}  ({idx+1}/{total})  ->  {out_path}")
            except Exception as e:
                import traceback
                errors.append((jf, str(e)))
                print(f"  ERR {os.path.basename(jf)}: {e}")
                traceback.print_exc()

        # Persist manifest (one write per batch -- not per SKU)
        if ok:
            save_manifest(output_dir, manifest)
            print(f"  MFS  {MANIFEST_FILENAME} updated ({len(ok)} entries written)")

        # Build index
        if ok:
            ok_sorted = sorted(ok, key=lambda x: x[0])
            if batch_slug:
                batches_dir = os.path.join(script_dir, "batches")
                batch_index_path = _write_batch_index(batches_dir, batch_slug, batch_label, ok_sorted)
                print(f"\n  IDX  {batch_index_path} written ({len(ok_sorted)} items)")
            else:
                index_path = os.path.join(script_dir, "index.html")
                _write_index(index_path, ok_sorted)
                print(f"\n  IDX  index.html updated ({len(ok_sorted)} items)")

        print(f"\nDone: {len(ok)} generated, {len(errors)} errors.")
        if errors:
            for jf, msg in errors:
                print(f"  ERR  {os.path.basename(jf)}: {msg}")
        return

    # Single-SKU mode
    json_path = args[0]
    try:
        sku, out_path, data, manifest_entry = generate_one(
            json_path, output_dir, template,
            renderer_version=renderer_version,
        )
        manifest[sku] = manifest_entry
        save_manifest(output_dir, manifest)
        print(f"OK  {sku}  ->  {out_path}")
    except Exception as e:
        import traceback
        print(f"ERR  {os.path.basename(json_path)}: {e}")
        traceback.print_exc()
        sys.exit(2)


if __name__ == "__main__":
    main()
