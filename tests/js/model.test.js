// Tests for shell/Model.js, the panel's pure helpers. Run: node tests/js/model.test.js
"use strict"
const assert = require("assert")
const fs = require("fs")
const path = require("path")
const vm = require("vm")

const source = fs.readFileSync(path.join(__dirname, "../../shell/Model.js"), "utf8")
  .replace(/^\.pragma library\s*$/m, "")
const M = {}
vm.createContext(M)
// QML's String.arg: replaces the lowest-numbered %N. Installed inside the
// context, whose String.prototype the model's strings use.
vm.runInContext(`String.prototype.arg = function (value) {
  const nums = (this.match(/%(\\d+)/g) || []).map((m) => Number(m.slice(1)))
  if (nums.length === 0) return String(this)
  return this.split("%" + Math.min(...nums)).join(String(value))
}`, M)
vm.runInContext(source, M)

const en = JSON.parse(fs.readFileSync(path.join(__dirname, "../../shell/i18n/en.json"), "utf8"))
const t = (key) => { const v = en[key]; return typeof v === "object" ? v.other : v === undefined ? key : v }
const tn = (key, n) => { const v = en[key]; return typeof v === "object" ? (n === 1 ? v.one : v.other) : t(key) }

let failures = 0
function test(name, fn) {
  try { fn(); console.log("ok   " + name) } catch (e) { failures++; console.log("FAIL " + name + "\n     " + e.message) }
}

const STATUS = {
  version: "0.0.1", atLogin: "on", hookPath: "/h/snapback", watching: true, restoring: false,
  sessions: [
    { name: "last", saved_at: "2026-11-23T09:00:00-07:00", windows: 4, reopen: 4, attention: 0, apps: ["A", "B"] },
    { name: "../evil", saved_at: "x", windows: 1 },
    { name: "broken", error: "can't read it" },
  ],
  lastRestore: {
    at: "2026-11-23T06:56:51-07:00", session: "last", trigger: "login", ok: false,
    counts: { placed: 5, "in-place": 1, failed: 1 }, problems: [{ app: "steam", detail: "timeout" }],
    errors: [], misplaced: 0,
  },
}

test("parseStatus reads a status", () => {
  const s = M.parseStatus(JSON.stringify(STATUS))
  assert.strictEqual(s.ok, true)
  assert.strictEqual(s.atLogin, "on")
  assert.strictEqual(s.hookPath, "/h/snapback")
  assert.strictEqual(s.watching, true)
  assert.deepStrictEqual(Array.from(s.sessions, (x) => x.name), ["last", "broken"])  // bad name dropped
  assert.strictEqual(s.sessions[0].savedAt, "2026-11-23T09:00:00-07:00")
  assert.deepStrictEqual(Array.from(s.sessions[0].apps), ["A", "B"])
  assert.strictEqual(s.sessions[1].error, "can't read it")
  assert.strictEqual(s.lastRestore.back, 6)
  assert.strictEqual(s.lastRestore.trigger, "login")
  assert.strictEqual(s.lastRestore.problems[0].app, "steam")
})

test("parseStatus refuses junk and fills gaps safely", () => {
  assert.strictEqual(M.parseStatus("not json").ok, false)
  assert.strictEqual(M.parseStatus("null").ok, false)
  const s = M.parseStatus(JSON.stringify({ atLogin: "maybe", sessions: "x", watching: "yes", lastRestore: 3 }))
  assert.strictEqual(s.atLogin, "off")
  assert.strictEqual(s.watching, false)
  assert.strictEqual(s.sessions.length, 0)
  assert.strictEqual(s.lastRestore, null)
})

test("parseStatus bounds sizes and counts", () => {
  const many = Array.from({ length: 80 }, (_, i) => ({ name: "s" + i, windows: -3, apps: Array(50).fill("x".repeat(500)) }))
  const s = M.parseStatus(JSON.stringify({ sessions: many }))
  assert.strictEqual(s.sessions.length, 50)
  assert.strictEqual(s.sessions[0].windows, 0)
  assert.strictEqual(s.sessions[0].apps.length, 30)
  assert.strictEqual(s.sessions[0].apps[0].length, 60)
})

test("session titles", () => {
  assert.strictEqual(M.sessionTitle("last", t), "Latest autosave")
  assert.strictEqual(M.sessionTitle("previous", t), "Before this login")
  assert.strictEqual(M.sessionTitle("before-switch", t), "Before switch")
  assert.strictEqual(M.sessionTitle("work", t), "work")
  assert.ok(M.isOwn("last") && M.isOwn("previous") && M.isOwn("before-switch") && !M.isOwn("work"))
  assert.notStrictEqual(M.glyphFor("before-switch"), M.glyphFor("work"))
})

test("apps line", () => {
  assert.strictEqual(M.appsLine(["A", "B"], 3, t), "A, B")
  assert.strictEqual(M.appsLine(["A", "B", "C", "D", "E"], 3, t), "A, B, C +2")
  assert.strictEqual(M.appsLine([], 3, t), "")
})

test("when", () => {
  const now = new Date(2026, 10, 23, 12, 0, 0).getTime()
  const clock = (d) => String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0")
  const day = (d) => d.getDate() + "/" + (d.getMonth() + 1)
  const at = (h, m, dd) => new Date(2026, 10, dd || 23, h, m).toISOString()
  assert.strictEqual(M.when(at(11, 59, 23), now, t, tn, clock, day), "1 min ago")
  assert.strictEqual(M.when(new Date(now - 20000).toISOString(), now, t, tn, clock, day), "just now")
  assert.strictEqual(M.when(new Date(now + 60000).toISOString(), now, t, tn, clock, day), "just now")  // clock skew
  assert.strictEqual(M.when(at(11, 15), now, t, tn, clock, day), "45 min ago")
  assert.strictEqual(M.when(at(6, 56), now, t, tn, clock, day), "today 06:56")
  assert.strictEqual(M.when(at(23, 10, 22), now, t, tn, clock, day), "yesterday 23:10")
  assert.strictEqual(M.when(at(14, 2, 20), now, t, tn, clock, day), "20/11 14:02")
  assert.strictEqual(M.when("garbage", now, t, tn, clock, day), "")
})

test("cleanName", () => {
  assert.strictEqual(M.cleanName("  my work  setup "), "my-work-setup")
  assert.strictEqual(M.cleanName("games!"), "games")
  assert.strictEqual(M.cleanName("../etc/passwd"), "etcpasswd")
  assert.strictEqual(M.cleanName("-x"), "x")
  assert.strictEqual(M.cleanName("work.json"), "work")
  assert.strictEqual(M.cleanName("last"), "")
  assert.strictEqual(M.cleanName("previous"), "")
  assert.strictEqual(M.cleanName("before switch"), "")
  assert.strictEqual(M.cleanName("   "), "")
  assert.strictEqual(M.cleanName("ñ"), "")
  assert.strictEqual(M.cleanName("a".repeat(100)).length, 64)
})

test("errorLine", () => {
  assert.strictEqual(M.errorLine("Traceback\nsnapback: no saved session named 'x'\n"), "no saved session named 'x'")
  assert.strictEqual(M.errorLine(""), "")
})

test("screens", () => {
  const s = M.parseStatus(JSON.stringify({ screens: [
    { connector: "HDMI-A-1", identity: "TV", position: "left", main: false, marked: false },
    { connector: "DP-3", identity: "ROG", position: "middle", main: true, marked: true },
    { connector: "bad name; rm", identity: "x", position: "right" },
    { connector: "DP-2", identity: "ROG 2", position: "right", main: "yes" }] }))
  assert.deepStrictEqual(Array.from(s.screens, (x) => x.connector), ["HDMI-A-1", "DP-3", "DP-2"])
  assert.strictEqual(s.screens[2].main, false)
  assert.strictEqual(M.nextMain(s.screens).connector, "DP-2")
  assert.strictEqual(M.nextMain([s.screens[2], s.screens[1]]).connector, "DP-2")  // wraps
  assert.strictEqual(M.nextMain([{ connector: "a", main: false }, { connector: "b", main: false }]).connector, "a")
  assert.strictEqual(M.nextMain([s.screens[0]]), null)
  assert.strictEqual(M.positionName("middle", t), "Middle")
  assert.strictEqual(M.positionName("4", t), "Screen 4")
})

test("every string the panel uses is in en.json", () => {
  const used = new Set()
  for (const file of ["Panel.qml", "Service.qml", "Model.js"]) {
    const text = fs.readFileSync(path.join(__dirname, "../../shell", file), "utf8")
    for (const m of text.matchAll(/\b(?:t|tn)\("([a-zA-Z.]+)"/g)) used.add(m[1])
  }
  const missing = [...used].filter((k) => !(k in en))
  assert.deepStrictEqual(missing, [])
})

if (failures) { console.log(failures + " failed"); process.exit(1) }
