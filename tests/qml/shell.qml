import Quickshell
import QtQuick

// Drives the real panel (copied in as BarPanel.qml) without a compositor or
// the real command line: the service's commands are replaced by `true`, and
// the status Snapback would print is handed in directly.
ShellRoot {
  Item {
    id: harness

    property int failures: 0
    function check(name, got, want) {
      if (JSON.stringify(got) === JSON.stringify(want)) {
        console.log("  ok   " + name)
      } else {
        failures++
        console.log("  FAIL " + name + ": got " + JSON.stringify(got) + ", wanted " + JSON.stringify(want))
      }
    }

    function status(fields) {
      var base = {
        version: "0.0.1", atLogin: "on", hookPath: "/hooks/snapback", watching: true, restoring: false,
        sessions: [
          { name: "last", saved_at: new Date().toISOString(), windows: 4, reopen: 4, attention: 0, apps: ["A"] },
          { name: "previous", saved_at: "2026-11-23T00:52:30-07:00", windows: 6, reopen: 6, attention: 0, apps: [] },
          { name: "work", saved_at: "2026-11-20T10:00:00-07:00", windows: 2, reopen: 2, attention: 0, apps: [] }
        ],
        lastRestore: null
      }
      for (var key in fields) base[key] = fields[key]
      barPanel.service.applyStatus(JSON.stringify(base))
    }

    Component.onCompleted: {
      var service = barPanel.service
      service.cliPrefix = ["true"]

      console.log("rows")
      status({})
      check("the switch, each session, then the save box", barPanel.rows,
            ["switch", "session:last", "session:previous", "session:work", "save"])
      check("every row has an item to scroll to",
            barPanel.rows.map(function (id) { return panelContent().rowItem(id) !== null }),
            [true, true, true, true, true])
      barPanel.cursorActive = true
      barPanel.rowIndex = 0
      barPanel.moveCursor(1)
      check("down from the switch reaches the first session", barPanel.currentRow, "session:last")
      barPanel.moveCursor(10)
      check("the cursor stops at the save box", barPanel.currentRow, "save")
      barPanel.moveCursor(-10)
      check("and at the switch", barPanel.currentRow, "switch")

      console.log("bar icon")
      check("lit while restore at login is on", String(barPanel.barIconColor), String(barPanel.barForeground))
      status({ atLogin: "off" })
      check("dimmed when it's off", String(barPanel.barIconColor), String(Qt.darker(barPanel.barForeground, 1.55)))

      console.log("switch")
      barPanel.rowIndex = 0
      barPanel.activate("switch")
      check("the knob moves at once", service.atLoginOn, true)
      check("and waits for setup", service.switchBusy, true)
      status({ atLogin: "off" })
      check("a status from before setup leaves it on", service.atLoginOn, true)
      check("the bar icon follows the switch", String(barPanel.barIconColor), String(barPanel.barForeground))
      settle(function () {
        status({ atLogin: "on" })
        check("a status that agrees releases it", service.switchBusy, false)
        check("on", service.atLoginOn, true)
        foreignChecks()
      })
    }

    function foreignChecks() {
      var service = barPanel.service
      console.log("someone else's hook")
      status({ atLogin: "foreign" })
      check("no switch row", barPanel.rows[0], "session:last")
      service.setAtLogin(true)
      check("the switch can't take it over", service.switchBusy, false)

      console.log("sessions")
      status({})
      barPanel.setRowCursor("session:last")
      barPanel.deleteCurrent()
      check("Snapback's own sessions can't be deleted", service.actionStatus, "")
      barPanel.save("last")
      check("nor saved over", service.actionIsError, true)
      barPanel.save("!!")
      check("a name with nothing usable is refused", service.actionIsError, true)

      console.log("restore report")
      status({ lastRestore: { at: new Date().toISOString(), session: "last", trigger: "login", ok: false,
                              counts: { placed: 5, "in-place": 1 }, problems: [{ app: "steam", detail: "x" }],
                              errors: [], misplaced: 1 } })
      check("summary", barPanel.restoreSummary(service.lastRestore), "6 windows back · 1 still out of place · just now")
      check("autosave line", barPanel.autosaveLine(), "Autosaved just now")
      status({ watching: false })
      check("autosave off", barPanel.autosaveLine(), "Autosave isn't running")

      console.log("switch to")
      status({})
      service.previewReady("work", [{ name: "T3 Code", title: "a" }, { name: "Chromium", title: "b" }])
      check("asks before closing anything", barPanel.switchName, "work")
      check("names what it would close", panelContent().children.filter(function (c) { return c.message !== undefined })[0].message.indexOf("T3 Code, Chromium") >= 0, true)
      barPanel.cancelSwitch()
      check("cancel asks nothing more", barPanel.switchName, "")
      check("and runs nothing", service.restoringName, "")
      service.previewReady("work", [{ name: "T3 Code", title: "a" }])
      barPanel.confirmSwitch()
      check("confirm restores the session", service.restoringName, "work")
      check("closing the others", service.restoreCommand.indexOf("--close-others") >= 0, true)
      settle(function () {
        service.previewReady("previous", [])
        check("with nothing to close it just restores", service.restoringName, "previous")
        check("without closing", service.restoreCommand.indexOf("--close-others"), -1)
        console.log(failures === 0 ? "ALL OK" : failures + " FAILED")
      })
    }

    // Lets the `true` standing in for setup exit, as the real one would.
    function settle(then) { settleTimer.then = then; settleTimer.start() }
    Timer { id: settleTimer; property var then: null; interval: 300; onTriggered: then() }

    function panelContent() { return barPanel.children.filter(function (c) { return c.loadedContent })[0].loadedContent }
  }

  BarPanel {
    id: barPanel
  }
}
