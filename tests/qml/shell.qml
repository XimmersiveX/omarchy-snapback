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
          { name: "work", saved_at: "2026-11-20T10:00:00-07:00", windows: 2, reopen: 2, attention: 0, apps: [] },
          { name: "before-switch", saved_at: "2026-11-23T10:00:00-07:00", windows: 3, reopen: 3, attention: 0, apps: [] }
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
      check("the switch, the user's setups, the save box, then undo", barPanel.rows,
            ["switch", "session:work", "save", "session:before-switch"])
      check("the autosave and the desk before login aren't listed",
            barPanel.rows.indexOf("session:last") + barPanel.rows.indexOf("session:previous"), -2)
      check("every row has an item to scroll to",
            barPanel.rows.map(function (id) { return panelContent().rowItem(id) !== null }),
            [true, true, true, true])
      barPanel.cursorActive = true
      barPanel.rowIndex = 0
      barPanel.moveCursor(1)
      check("down from the switch reaches the first setup", barPanel.currentRow, "session:work")
      barPanel.moveCursor(10)
      check("the cursor stops at the last row", barPanel.currentRow, "session:before-switch")
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
      check("no switch row", barPanel.rows[0], "session:work")
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
      check("a restore with trouble is shown", barPanel.restoreTrouble, true)
      status({ lastRestore: { at: new Date().toISOString(), session: "last", trigger: "login", ok: true,
                              counts: { placed: 6 }, problems: [], errors: [], misplaced: 0, kept: [] } })
      check("a clean one isn't", barPanel.restoreTrouble, false)
      check("autosave line", barPanel.autosaveLine(), "Autosaved just now")
      status({ watching: false })
      check("autosave off", barPanel.autosaveLine(), "Autosave isn't running")

      console.log("switch to")
      status({})
      barPanel.activate("session:work")
      check("clicking a setup switches to it at once", service.restoringName, "work")
      check("closing what it doesn't have", service.restoreCommand.indexOf("--close-others") >= 0, true)
      settle(function () {
        barPanel.restore("work", false)
        check("the row's button puts windows back without closing", service.restoreCommand.indexOf("--close-others"), -1)
        settle(function () {
          status({ sessions: [{ name: "before-switch", saved_at: "2026-11-23T10:00:00-07:00", windows: 1, apps: [] }] })
          barPanel.setRowCursor("session:before-switch")
          var shown = service.actionStatus
          barPanel.deleteCurrent()
          check("before-switch is Snapback's own: not deletable", service.actionStatus, shown)
          console.log("main screen")
          status({ screens: [{ connector: "HDMI-A-1", identity: "TV", position: "left", main: false },
                             { connector: "DP-3", identity: "ROG", position: "middle", main: true }] })
          check("a main-screen row, last", barPanel.rows[barPanel.rows.length - 1], "main")
          check("it has an item", panelContent().rowItem("main") !== null, true)
          status({ screens: [{ connector: "eDP-1", identity: "Laptop", position: "only", main: true }] })
          check("no main-screen row with one screen", barPanel.rows.indexOf("main"), -1)

          console.log(failures === 0 ? "ALL OK" : failures + " FAILED")
        })
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
