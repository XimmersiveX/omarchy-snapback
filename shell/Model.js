.pragma library

// Pure helpers for the panel: no QML, no I/O, so they can be tested with node.
// `t(key)` and `tn(key, count)` are the plugin's translation lookups.

var GLYPH = {
  bar: String.fromCodePoint(0xF05B2),       // window-restore
  last: String.fromCodePoint(0xF0193),      // content-save
  previous: String.fromCodePoint(0xF054D),  // undo-variant
  named: String.fromCodePoint(0xF056E),     // view-dashboard
  remove: String.fromCodePoint(0xF01B4),    // delete
  addOnly: String.fromCodePoint(0xF0E4D),   // layers-plus: put back, close nothing
  save: String.fromCodePoint(0xF0193)
}

var OWN = ["last", "previous"]
var NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/
var MAX_SESSIONS = 50
var MAX_APPS = 30

function str(value, max) {
  if (value === null || value === undefined) return ""
  return String(value).slice(0, max || 200)
}

function num(value) {
  var n = Number(value)
  return isFinite(n) && n >= 0 ? Math.floor(n) : 0
}

// `snapback status --json`, checked and trimmed to what the panel draws.
function parseStatus(text) {
  var raw
  try { raw = JSON.parse(String(text)) } catch (e) { return { ok: false } }
  if (!raw || typeof raw !== "object") return { ok: false }
  var atLogin = ["on", "off", "foreign"].indexOf(raw.atLogin) >= 0 ? raw.atLogin : "off"
  var sessions = Array.isArray(raw.sessions) ? raw.sessions.slice(0, MAX_SESSIONS) : []
  return {
    ok: true,
    version: str(raw.version, 40),
    atLogin: atLogin,
    hookPath: str(raw.hookPath, 500),
    watching: raw.watching === true,
    restoring: raw.restoring === true,
    sessions: sessions.filter(function (s) { return s && NAME_RE.test(String(s.name)) }).map(parseSession),
    lastRestore: parseRestore(raw.lastRestore)
  }
}

function parseSession(s) {
  return {
    name: String(s.name),
    error: s.error ? str(s.error, 300) : "",
    savedAt: str(s.saved_at, 40),
    windows: num(s.windows),
    reopen: num(s.reopen),
    attention: num(s.attention),
    apps: (Array.isArray(s.apps) ? s.apps.slice(0, MAX_APPS) : []).map(function (a) { return str(a, 60) })
  }
}

function parseRestore(r) {
  if (!r || typeof r !== "object") return null
  var counts = r.counts && typeof r.counts === "object" ? r.counts : {}
  return {
    at: str(r.at, 40),
    session: str(r.session, 64),
    trigger: r.trigger === "login" ? "login" : "manual",
    ok: r.ok === true,
    back: num(counts.placed) + num(counts["in-place"]),
    problems: (Array.isArray(r.problems) ? r.problems.slice(0, 20) : []).map(function (p) {
      return { app: str(p && p.app, 80), detail: str(p && p.detail, 200) }
    }),
    errors: (Array.isArray(r.errors) ? r.errors.slice(0, 5) : []).map(function (e) { return str(e, 300) }),
    misplaced: num(r.misplaced),
    closed: num(r.closed),
    kept: (Array.isArray(r.kept) ? r.kept.slice(0, 20) : []).map(function (k) { return str(k, 80) })
  }
}

// The windows switching to a session would close, from
// `snapback restore NAME --dry-run --json`. null when the plan can't be read.
function parseOthers(text) {
  var raw
  try { raw = JSON.parse(String(text)) } catch (e) { return null }
  if (!raw || typeof raw !== "object" || !Array.isArray(raw.others)) return null
  return raw.others.slice(0, 100).map(function (o) {
    return { name: str(o && (o.name || o.app), 80), title: str(o && o.title, 120) }
  })
}

// "T3 Code, Chromium and 2 more": each app once, the first `shown` by name.
function othersLine(others, shown, t) {
  var names = []
  for (var i = 0; i < others.length; i++)
    if (names.indexOf(others[i].name) < 0) names.push(others[i].name)
  var counts = names.map(function (n) {
    var c = others.filter(function (o) { return o.name === n }).length
    return c > 1 ? t("others.times").arg(n).arg(c) : n
  })
  if (counts.length <= shown) return counts.join(", ")
  return t("others.more").arg(counts.slice(0, shown).join(", ")).arg(counts.length - shown)
}

function isOwn(name) { return OWN.indexOf(name) >= 0 }

function glyphFor(name) {
  return name === "last" ? GLYPH.last : name === "previous" ? GLYPH.previous : GLYPH.named
}

function sessionTitle(name, t) {
  if (name === "last") return t("session.last")
  if (name === "previous") return t("session.previous")
  return name
}

// "Chromium, T3 Code, Foot +2"
function appsLine(apps, shown, t) {
  shown = shown || 3
  if (!apps || apps.length === 0) return ""
  var head = apps.slice(0, shown).join(", ")
  return apps.length > shown ? t("apps.more").arg(head).arg(apps.length - shown) : head
}

function parseTime(iso) {
  var ms = Date.parse(iso)
  return isFinite(ms) ? ms : NaN
}

// "just now", "5 min ago", "today 06:56", "yesterday 23:10", "20 Nov 14:02".
// `clock(date)` and `day(date)` format with the user's locale (QML side).
function when(iso, nowMs, t, tn, clock, day) {
  var ms = parseTime(iso)
  if (!isFinite(ms)) return ""
  var diff = nowMs - ms  // a time ahead of the clock reads "just now"
  if (diff < 60 * 1000) return t("time.justNow")
  if (diff < 60 * 60 * 1000) {
    var minutes = Math.floor(diff / 60000)
    return tn("time.minutesAgo", minutes).arg(minutes)
  }
  var then = new Date(ms)
  var now = new Date(nowMs)
  var startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  if (ms >= startOfToday) return t("time.today").arg(clock(then))
  if (ms >= startOfToday - 24 * 60 * 60 * 1000) return t("time.yesterday").arg(clock(then))
  return t("time.onDay").arg(day(then)).arg(clock(then))
}

// The name typed into "Save as": spaces become dashes, anything else a
// session name can't hold is dropped. Snapback's own names are refused.
function cleanName(text) {
  var name = String(text || "").trim().replace(/\s+/g, "-").replace(/[^A-Za-z0-9._-]/g, "")
  name = name.replace(/^[._-]+/, "").slice(0, 64).replace(/\.json$/i, "")
  if (!NAME_RE.test(name) || isOwn(name)) return ""
  return name
}

// The last line of a failed command's stderr, without the "snapback: " prefix.
function errorLine(stderr) {
  var lines = String(stderr || "").trim().split("\n")
  return lines[lines.length - 1].replace(/^snapback: /, "").slice(0, 300)
}
