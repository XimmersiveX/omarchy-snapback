import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

// Snapback's state for the panel, read from `snapback status --json`, and the
// commands the panel runs. Everything goes through the command line in this
// plugin's folder, so the panel holds no logic of its own about windows.
Item {
  id: root

  // This file is shell/Service.qml; the snapback package sits beside shell/.
  readonly property string pluginDir: decodeURIComponent(String(Qt.resolvedUrl(".."))
    .replace(/^file:\/\//, "")).replace(/\/$/, "")
  // No bytecode: Omarchy reloads a plugin when a file in its folder changes.
  // -P: never import from the current folder (the shell's is home).
  // Replaced by tests, so they never reach the real command line.
  property var cliPrefix: ["env", "PYTHONPATH=" + pluginDir, "PYTHONDONTWRITEBYTECODE=1",
                           "python3", "-P", "-m", "snapback"]

  property bool loaded: false
  property string atLogin: ""          // "on", "off", "foreign"
  property bool watching: false
  property bool restoring: false
  property var sessions: []
  property var lastRestore: null
  property var screens: []
  property string hookPath: ""

  // The switch shows what was asked for until a status read agrees (-1: follow
  // the status), so it moves at once and an older read can't throw it back.
  property int _desiredAtLogin: -1
  readonly property bool atLoginOn: _desiredAtLogin === -1 ? atLogin === "on" : _desiredAtLogin === 1
  readonly property bool switchBusy: _desiredAtLogin !== -1 || setupProcess.running
  property string restoringName: ""
  readonly property bool busy: restoring || restoreProcess.running
  readonly property var restoreCommand: restoreProcess.command  // what the last restore ran

  property string actionStatus: ""
  property bool actionIsError: false
  property var _strings: null   // the panel's translations, for flash messages

  signal saved(string name)

  function cli(args) { return cliPrefix.concat(args) }

  function _same(a, b) { return JSON.stringify(a) === JSON.stringify(b) }

  property bool _refreshAgain: false
  function refresh() {
    if (statusProcess.running) { _refreshAgain = true; return }
    statusProcess.command = cli(["status", "--json"])
    statusProcess.running = true
  }

  function applyStatus(text) {
    var parsed = Model.parseStatus(text)
    if (!parsed.ok) return
    loaded = true
    atLogin = parsed.atLogin
    hookPath = parsed.hookPath
    watching = parsed.watching
    restoring = parsed.restoring
    if (!_same(sessions, parsed.sessions)) sessions = parsed.sessions
    if (!_same(lastRestore, parsed.lastRestore)) lastRestore = parsed.lastRestore
    if (!_same(screens, parsed.screens)) screens = parsed.screens
    if (_desiredAtLogin !== -1 && !setupProcess.running && atLoginOn === (atLogin === "on"))
      _desiredAtLogin = -1
  }

  function flash(message, error) {
    actionStatus = message
    actionIsError = error === true
    flashTimer.restart()
  }

  function t(key) { return _strings ? _strings.t(key) : key }

  // ---- Restore at login -----------------------------------------------------
  // On writes Omarchy's post-boot hook and starts the autosave; off removes the
  // hook and stops it. Saved sessions are kept either way.
  function setAtLogin(on) {
    if (switchBusy || atLogin === "foreign") return
    _desiredAtLogin = on ? 1 : 0
    setupProcess.command = cli(on ? ["setup"] : ["setup", "--remove"])
    setupProcess.running = true
  }
  function toggleAtLogin() { setAtLogin(!atLoginOn) }

  // ---- Sessions -------------------------------------------------------------
  function restore(name, closeOthers) {
    if (busy || !Model.NAME_RE.test(name)) return
    restoringName = name
    restoreProcess.command = cli(["restore", name, "--json", "--notify"]
      .concat(closeOthers === true ? ["--close-others"] : []))
    restoreProcess.running = true
  }

  function save(name) {
    if (saveProcess.running) return
    var existed = sessions.some(function (s) { return s.name === name })
    saveProcess.existed = existed
    saveProcess.name = name
    saveProcess.command = cli(["save", name])
    saveProcess.running = true
  }

  // Mark the main screen of this place (setups opened on other screens than
  // they were saved on put their main screen's windows there).
  function setMain(connector) {
    if (mainProcess.running) return
    mainProcess.command = cli(["screens", "--main", connector])
    mainProcess.running = true
  }

  function remove(name) {
    if (deleteProcess.running || Model.isOwn(name)) return
    deleteProcess.name = name
    deleteProcess.command = cli(["delete", name])
    deleteProcess.running = true
  }

  Timer {
    id: flashTimer
    interval: 5000
    onTriggered: root.actionStatus = ""
  }

  // A setup that never shows up in the status releases the switch.
  Timer {
    interval: 10000
    running: root._desiredAtLogin !== -1
    onTriggered: { root._desiredAtLogin = -1; root.refresh() }
  }

  Process {
    id: statusProcess
    stdout: StdioCollector { id: statusOut; waitForEnd: true }
    onExited: function (exitCode) {
      if (exitCode === 0) root.applyStatus(statusOut.text)
      if (root._refreshAgain) { root._refreshAgain = false; root.refresh() }
    }
  }

  Process {
    id: setupProcess
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { id: setupErr; waitForEnd: true }
    onExited: function (exitCode) {
      if (exitCode !== 0) {
        root._desiredAtLogin = -1
        root.flash(Model.errorLine(setupErr.text) || root.t("error.generic"), true)
      }
      root.refresh()
    }
  }

  Process {
    id: restoreProcess
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { id: restoreErr; waitForEnd: true }
    onRunningChanged: if (running) root.refresh()
    onExited: function (exitCode) {
      // Exit 1 with a report is a restore that ran and left something out;
      // the report says what. Only a restore that left no report flashes.
      var line = Model.errorLine(restoreErr.text)
      if (exitCode !== 0 && line !== "") root.flash(root.t("restore.failed").arg(line), true)
      root.restoringName = ""
      root.refresh()
    }
  }

  Process {
    id: mainProcess
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { id: mainErr; waitForEnd: true }
    onExited: function (exitCode) {
      if (exitCode !== 0) root.flash(Model.errorLine(mainErr.text) || root.t("error.generic"), true)
      root.refresh()
    }
  }

  Process {
    id: saveProcess
    property string name: ""
    property bool existed: false
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { id: saveErr; waitForEnd: true }
    onExited: function (exitCode) {
      if (exitCode === 0) {
        root.flash(root.t(existed ? "replaced" : "saved").arg(name))
        root.saved(name)
      } else {
        root.flash(Model.errorLine(saveErr.text) || root.t("error.generic"), true)
      }
      root.refresh()
    }
  }

  Process {
    id: deleteProcess
    property string name: ""
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { id: deleteErr; waitForEnd: true }
    onExited: function (exitCode) {
      if (exitCode === 0) root.flash(root.t("deleted").arg(name))
      else root.flash(Model.errorLine(deleteErr.text) || root.t("error.generic"), true)
      root.refresh()
    }
  }
}
