"""Fetch a Google Calendar ICS feed, fix known issues, write docs/calendar.ics.
 
Output: one file per feed in docs/ (routing: menu / WOL by title, four tag-based
feeds from the "Content type:" line in DESCRIPTION, everything else -> other.ics).
 
Fixes:
 1. Midnight-to-midnight timed events (TZID or UTC) -> true all-day (VALUE=DATE)
 2. HTML / entities in DESCRIPTION -> plain text
 3. DTSTAMP pinned to LAST-MODIFIED so output is byte-stable between runs
No third-party dependencies (Python 3.9+).
 
Usage: SOURCE_ICS_URL=... python fix_ics.py     (or: python fix_ics.py local.ics)
"""
import html, os, re, sys, urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
 
FEEDS = {  # file name -> calendar display suffix
    "school-event": "School Event", "enrichment": "Enrichment",
    "team-faculty": "Team/Faculty", "family-event": "Family Event",
    "menu": "Menu", "wol": "WOL Units", "other": "Other",
}
TAGS = {"school event": "school-event", "enrichment": "enrichment",
        "team/faculty": "team-faculty", "family event": "family-event"}
 
 # ---------- I/O helpers ----------
def load():
    if len(sys.argv) > 1:
        raw = open(sys.argv[1], "rb").read()
    else:
        req = urllib.request.Request(os.environ["SOURCE_ICS_URL"],
                                     headers={"User-Agent": "ics-fixer/1.0"})
        raw = urllib.request.urlopen(req, timeout=30).read()
    return raw.decode("utf-8")
 
 
def unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text)
 
 
def fold(line, limit=75):
    """Fold on UTF-8 octet length without splitting a character."""
    out, cur, cur_len = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if cur_len + n > limit:
            out.append(cur)
            cur, cur_len = " " + ch, 1 + n
        else:
            cur += ch
            cur_len += n
    out.append(cur)
    return "\r\n".join(out)
 
 def ics_unescape(s):
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), s)
 
 
def ics_escape(s):
    return (s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
             .replace("\r\n", "\n").replace("\n", "\\n"))
 
 
# ---------- description cleanup ----------
def clean_desc(s):
    s = html.unescape(s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "• ", s)
    s = re.sub(r'(?i)<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
               lambda m: m.group(2) if m.group(2).strip() == m.group(1)
               else f"{m.group(2)} ({m.group(1)})", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()
 
 
# ---------- date logic ----------
def split_prop(line):
    head, _, value = line.partition(":")
    name, *params = head.split(";")
    return name.upper(), dict(p.split("=", 1) for p in params if "=" in p), value
 
 
def parse_dt(params, value, default_tz):
    """Return date (all-day) or aware datetime."""
    if params.get("VALUE") == "DATE" or len(value) == 8:
        return datetime.strptime(value, "%Y%m%d").date()
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    tz = ZoneInfo(params.get("TZID", default_tz))
    return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=tz)
 
 
def local_midnight(dt, tz):
    l = dt.astimezone(tz)
    return l.time().replace(microsecond=0) == datetime.min.time()
 
 def fix_dates(lines, tz):
    """Rewrite DTSTART/DTEND if the event is really all-day. Returns (lines, changed)."""
    idx = {}
    for i, ln in enumerate(lines):
        n = ln.split(":", 1)[0].split(";")[0].upper()
        if n in ("DTSTART", "DTEND") and n not in idx:
            idx[n] = i
    if "DTSTART" not in idx:
        return lines, False
    s = parse_dt(*split_prop(lines[idx["DTSTART"]])[1:], tz.key)
    e = parse_dt(*split_prop(lines[idx["DTEND"]])[1:], tz.key) if "DTEND" in idx else None
    if isinstance(s, date) and not isinstance(s, datetime):
        return lines, False                      # already all-day
    if not local_midnight(s, tz):
        return lines, False
    if e is not None and not local_midnight(e, tz):
        return lines, False                      # timed event that only starts at midnight
    sd = s.astimezone(tz).date()
    ed = e.astimezone(tz).date() if e is not None else sd + timedelta(days=1)
    if ed <= sd:
        ed = sd + timedelta(days=1)
    lines[idx["DTSTART"]] = f"DTSTART;VALUE=DATE:{sd:%Y%m%d}"
    if "DTEND" in idx:
        lines[idx["DTEND"]] = f"DTEND;VALUE=DATE:{ed:%Y%m%d}"
    else:
        lines.append(f"DTEND;VALUE=DATE:{ed:%Y%m%d}")
    # a datetime UNTIL on a now-all-day series must become a date
    for i, ln in enumerate(lines):
        if ln.upper().startswith("RRULE:"):
            lines[i] = re.sub(r"UNTIL=(\d{8})T\d{6}Z?", r"UNTIL=\1", ln)
    return lines, True
 
 # ---------- main ----------
def route(summary, desc):
    if summary.startswith("Today's Menu"):
        return "menu"
    if re.match(r"(WOL\b|Unit \d+\s)", summary):
        return "wol"
    m = re.match(r"Content type:\s*([^\n]+)", desc)
    return TAGS.get(m.group(1).strip().lower(), "other") if m else "other"
 
 
def process(text):
    text = unfold(text)
    all_lines = text.replace("\r\n", "\n").split("\n")
    tzname = next((l.split(":", 1)[1] for l in all_lines if l.startswith("X-WR-TIMEZONE:")),
                  "America/New_York")
    tz = ZoneInfo(tzname)
    header, footer, ev, in_ev, events = [], [], [], False, {k: [] for k in FEEDS}
    seen_ev = False
    n_fixed = 0
    for ln in all_lines:
        if ln == "BEGIN:VEVENT":
            in_ev, ev, seen_ev = True, [], True
            continue
        if ln == "END:VEVENT":
            ev, changed = fix_dates(ev, tz)
            n_fixed += changed
            lm = next((l.split(":", 1)[1] for l in ev if l.startswith("LAST-MODIFIED:")), None)
            new, desc, summary = [], "", ""
            for l in ev:
                if l.startswith("DTSTAMP:") and lm:
                    l = f"DTSTAMP:{lm}"
                elif l.startswith("DESCRIPTION"):
                    head_, _, val = l.partition(":")
                    desc = clean_desc(ics_unescape(val))
                    l = f"{head_}:{ics_escape(desc)}"
                elif l.startswith("SUMMARY"):
                    summary = ics_unescape(l.partition(":")[2])
                new.append(l)
            events[route(summary, desc)] += [["BEGIN:VEVENT"] + new + ["END:VEVENT"]]
          in_ev = False
            continue
        if in_ev:
            ev.append(ln)
        elif ln:
            (footer if seen_ev else header).append(ln)
    print(f"all-day fixed: {n_fixed}", file=sys.stderr)
 
    files = {}
    for key, label in FEEDS.items():
        h = []
        for l in header:
            if l.startswith("X-WR-CALNAME:"):
                l = f"X-WR-CALNAME:{l.split(':', 1)[1]} - {label}"
            h.append(l)
        lines = h + [l for e in events[key] for l in e] + footer
        files[key] = "\r\n".join(fold(l) for l in lines) + "\r\n"
        print(f"{key}: {len(events[key])} events", file=sys.stderr)
    return files
 
 
if __name__ == "__main__":
    os.makedirs("docs", exist_ok=True)
    for key, content in process(load()).items():
        with open(f"docs/{key}.ics", "w", encoding="utf-8", newline="") as f:
            f.write(content)
 
