import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// Snapback in the Omarchy bar: a switch for restoring windows at login, the
// saved setups (click one to switch the desk to it), and "save open windows
// as a setup". The panel only draws what `snapback status --json` says.
Panel {
  id: root
  moduleName: "snapback"
  ipcTarget: "snapback"
  manageIpc: false

  readonly property Item panelFlick: panel.loadedContent ? panel.loadedContent.panelFlick : null
  readonly property Item saveField: panel.loadedContent ? panel.loadedContent.saveField : null

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color barIconColor: snap.atLoginOn ? barForeground : Qt.darker(barForeground, 1.55)

  // The keyboard cursor walks these rows: the switch, each session, the save box.
  readonly property var rows: (snap.atLogin === "foreign" ? [] : ["switch"])
    .concat(snap.sessions.map(function (s) { return "session:" + s.name }))
    .concat(["save"])
    .concat(snap.screens.length > 1 ? ["main"] : [])
  property int rowIndex: 0
  property bool cursorActive: false
  readonly property string currentRow: cursorActive ? rows[Math.min(rowIndex, rows.length - 1)] : ""
  property double nowMs: Date.now()

  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  function clock(date) { return date.toLocaleTimeString(Qt.locale(Commons.I18n.formatLocale), Locale.ShortFormat) }
  function day(date) { return date.toLocaleDateString(Qt.locale(Commons.I18n.formatLocale), "d MMM") }
  function when(iso) {
    return Model.when(iso, root.nowMs, strings.t, strings.tn, root.clock, root.day)
  }

  function setRowCursor(id) {
    cursorActive = true
    rowIndex = Math.max(0, rows.indexOf(id))
  }

  function moveCursor(dy) {
    cursorActive = true
    rowIndex = Math.max(0, Math.min(rows.length - 1, rowIndex + dy))
    scrollCursorIntoView()
  }

  function activate(id) {
    if (id === "switch") snap.toggleAtLogin()
    else if (id === "save" && saveField) saveField.forceActiveFocus()
    else if (id === "main") cycleMain()
    else if (id.indexOf("session:") === 0) switchTo(id.slice(8))
  }

  // Moves "main" to the next screen, left to right.
  function cycleMain() {
    var next = Model.nextMain(snap.screens)
    if (next) snap.setMain(next.connector)
  }

  function deleteCurrent() {
    if (currentRow.indexOf("session:") !== 0) return
    var name = currentRow.slice(8)
    if (!Model.isOwn(name)) snap.remove(name)
  }

  // The panel closes first: a restore moves focus around and puts it back on
  // the window that had it, which the open panel would otherwise hold.
  function restore(name, closeOthers) {
    if (snap.busy) return
    snap.flash(strings.t("restore.started").arg(Model.sessionTitle(name, strings.t)))
    root.close()
    snap.restore(name, closeOthers === true)
  }

  // Clicking a setup switches to it: the desk becomes the setup, and windows
  // it doesn't have close (politely; unsaved work can still ask). Snapback
  // keeps the desk as it was as "Before switch", so a misclick is one click
  // to undo. The row's button puts windows back without closing anything.
  function switchTo(name) { restore(name, true) }

  function save(text) {
    var name = Model.cleanName(text)
    if (name === "") {
      snap.flash(strings.t("save.badName"), true)
      return
    }
    snap.save(name)
  }

  function scrollCursorIntoView() {
    if (!panelFlick || !panel.loadedContent) return
    var item = panel.loadedContent.rowItem(currentRow)
    if (!item) return
    Qt.callLater(function () {
      var margin = Style.space(6)
      var top = item.mapToItem(panelFlick.contentItem, 0, 0).y
      var bottom = top + item.height
      var maxY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
      if (top < panelFlick.contentY + margin) panelFlick.contentY = Math.max(0, top - margin)
      else if (bottom > panelFlick.contentY + panelFlick.height - margin)
        panelFlick.contentY = Math.min(maxY, bottom + margin - panelFlick.height)
    })
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: if (opened) {
    cursorActive = false
    rowIndex = 0
    nowMs = Date.now()
    if (panelFlick) panelFlick.contentY = 0
    snap.refresh()
    Qt.callLater(function () { if (panel.loadedContent) panel.loadedContent.forceActiveFocus() })
  }

  Component.onCompleted: snap.refresh()

  Service {
    id: snap
    _strings: strings
    onSaved: function (name) { if (root.saveField) root.saveField.text = "" }
  }
  property alias service: snap

  ShellIpc {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { snap.refresh(); return "ok" }
    // omarchy-shell snapback switchTo NAME: what clicking the setup does, e.g.
    // from a keybinding.
    function switchTo(name: string): string {
      if (!Model.NAME_RE.test(name)) return "bad setup name"
      if (snap.busy) return "busy"
      root.switchTo(name)
      return "ok"
    }
    function state(): string {
      return JSON.stringify({ opened: root.opened, busy: snap.busy, restoring: snap.restoringName,
                              status: snap.actionStatus, error: snap.actionIsError })
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: Model.GLYPH.bar
    foreground: root.barIconColor
    tooltipText: strings.t("tooltip")
    onPressed: function (buttonCode) {
      if (buttonCode === Qt.RightButton) snap.refresh()
      else root.toggle()
    }
  }

  // Keeps "5 min ago" honest and follows the autosave while the panel is open.
  Timer {
    interval: 5000
    repeat: true
    running: root.opened
    onTriggered: { root.nowMs = Date.now(); snap.refresh() }
  }

  // The bar icon dims when restore at login is turned off elsewhere.
  Timer {
    interval: 60000
    repeat: true
    running: !root.opened
    onTriggered: snap.refresh()
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: panel.loadedContent
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight, Style.space(560))

    content: PanelKeyCatcher {
      id: keyCatcher
      property alias panelFlick: panelFlick
      property alias saveField: saveField
      implicitHeight: column.implicitHeight
      anchors.fill: parent
      blocked: saveField.activeFocus

      function rowItem(id) {
        if (id === "switch") return hero
        if (id === "save") return saveRow
        if (id === "main") return mainRow
        for (var i = 0; i < sessionColumn.children.length; i++) {
          var child = sessionColumn.children[i]
          if (child.rowId === id) return child
        }
        return null
      }

      onMoveRequested: function (dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveCursor(dy)
      }
      onActivateRequested: if (root.cursorActive) root.activate(root.currentRow)
      onDeleteRequested: if (root.cursorActive) root.deleteCurrent()
      onCloseRequested: root.close()
      onTabRequested: function (direction) { root.switchPanel(direction) }
      onTextKey: function (t) {
        if ((t === "a" || t === "A") && root.currentRow.indexOf("session:") === 0)
          root.restore(root.currentRow.slice(8), false)
        else if (t === "r" || t === "R") snap.refresh()
        else if (t === "s" || t === "S") { root.setRowCursor("save"); saveField.forceActiveFocus() }
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          x: Style.space(2)
          width: panelFlick.width - Style.space(4)
          spacing: Style.space(12)

          Item {
            id: header
            width: parent.width
            implicitHeight: hero.implicitHeight
            readonly property bool ringVisible: root.currentRow === "switch"

            PanelHero {
              id: hero
              width: parent.width
              title: strings.t("title")
              meta: !snap.loaded ? strings.t("meta.loading")
                : snap.busy ? strings.t("meta.restoring")
                : snap.atLogin === "foreign" ? strings.t("meta.foreign")
                : snap.atLoginOn ? strings.t("meta.on") : strings.t("meta.off")
              detail: root.autosaveLine()
              foreground: root.foreground
              fontFamily: root.fontFamily
              iconOpacity: snap.atLoginOn ? 1.0 : 0.5
              iconComponent: Component {
                Text {
                  textFormat: Text.PlainText
                  text: Model.GLYPH.bar
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.display
                }
              }

              trailingControl: Component {
                ToggleSwitch {
                  id: loginSwitch
                  visible: snap.loaded && snap.atLogin !== "foreign"
                  checked: snap.atLoginOn
                  busy: snap.switchBusy
                  hasCursor: header.ringVisible
                  foreground: hero.foreground
                  onHovered: function (on) { if (on) root.setRowCursor("switch") }
                  onToggled: snap.toggleAtLogin()

                  PanelToolTip {
                    visible: loginSwitch.containsMouse
                    text: snap.atLoginOn ? strings.t("switch.on") : strings.t("switch.off")
                    fontFamily: hero.fontFamily
                  }
                }
              }
            }
          }

          PlainLine {
            visible: snap.atLogin === "foreign"
            text: strings.t("foreign.help").arg(snap.hookPath)
            color: root.urgent
          }

          PlainLine {
            visible: snap.actionStatus !== ""
            text: snap.actionStatus
            color: snap.actionIsError ? root.urgent : root.dim
          }

          LastRestore {
            visible: snap.lastRestore !== null
            width: parent.width
          }

          PanelSeparator { foreground: root.foreground }

          Column {
            width: parent.width
            spacing: Style.space(10)

            PanelSectionHeader {
              text: strings.t("sessions")
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            PlainLine {
              visible: snap.loaded && snap.sessions.length === 0
              text: strings.t("sessions.none")
              color: root.dim
              horizontalAlignment: Text.AlignHCenter
            }

            Column {
              id: sessionColumn
              width: parent.width
              spacing: Style.space(6)

              Repeater {
                model: snap.sessions
                SessionRow {
                  required property var modelData
                  width: sessionColumn.width
                  session: modelData
                }
              }
            }

            CursorSurface {
              id: saveRow
              width: parent.width
              hasCursor: root.currentRow === "save" && !saveField.activeFocus
              foreground: root.foreground
              implicitHeight: saveLayout.implicitHeight + Style.spacing.rowPaddingX

              RowLayout {
                id: saveLayout
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Style.space(10)
                anchors.rightMargin: Style.space(10)
                spacing: Style.space(8)

                Text {
                  textFormat: Text.PlainText
                  text: Model.GLYPH.save
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.icon
                  Layout.alignment: Qt.AlignVCenter
                }

                TextField {
                  id: saveField
                  Layout.fillWidth: true
                  placeholderText: strings.t("save.placeholder")
                  foreground: root.foreground
                  font.family: root.fontFamily
                  maximumLength: 64
                  onActiveFocusChanged: if (activeFocus) root.setRowCursor("save")
                  onAccepted: root.save(text)
                  Keys.onEscapePressed: function (event) {
                    keyCatcher.forceActiveFocus()
                    event.accepted = true
                  }
                }
              }
            }

            // Which screen is main here. Setups opened on other screens than
            // they were saved on put their main screen's windows on it, and
            // fold screens this place doesn't have into it.
            CursorSurface {
              id: mainRow
              visible: snap.screens.length > 1
              width: parent.width
              hasCursor: root.currentRow === "main"
              foreground: root.foreground
              implicitHeight: mainLayout.implicitHeight + Style.spacing.rowPaddingX
              readonly property var current: snap.screens.filter(function (s) { return s.main })[0] || null

              MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onEntered: root.setRowCursor("main")
                onClicked: root.cycleMain()
              }

              RowLayout {
                id: mainLayout
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Style.space(10)
                anchors.rightMargin: Style.space(10)
                spacing: Style.space(8)

                Text {
                  textFormat: Text.PlainText
                  text: Model.GLYPH.screen
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.icon
                  Layout.alignment: Qt.AlignVCenter
                }

                ColumnLayout {
                  Layout.fillWidth: true
                  spacing: Style.space(1)

                  Text {
                    textFormat: Text.PlainText
                    Layout.fillWidth: true
                    text: mainRow.current
                      ? strings.t("screens.main").arg(Model.positionName(mainRow.current.position, strings.t)) : ""
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    elide: Text.ElideRight
                  }

                  Text {
                    textFormat: Text.PlainText
                    Layout.fillWidth: true
                    text: mainRow.current
                      ? strings.t(mainRow.current.marked ? "screens.marked" : "screens.guessed")
                          .arg(mainRow.current.identity) : ""
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                  }
                }
              }
            }
          }
        }
      }
    }
  }

  function autosaveLine() {
    if (!snap.loaded) return ""
    if (!snap.watching) return strings.t("autosave.off")
    var last = snap.sessions.filter(function (s) { return s.name === "last" })[0]
    return last && last.savedAt ? strings.t("autosave.on").arg(root.when(last.savedAt)) : ""
  }

  function restoreSummary(r) {
    var parts = [strings.tn("restore.back", r.back).arg(r.back)]
    if (r.closed > 0) parts.push(strings.tn("restore.closed", r.closed).arg(r.closed))
    if (r.misplaced > 0) parts.push(strings.tn("restore.misplaced", r.misplaced).arg(r.misplaced))
    return parts.join(" · ") + " · " + root.when(r.at)
  }

  component PlainLine: Text {
    textFormat: Text.PlainText
    width: parent ? parent.width : 0
    color: root.dim
    font.family: root.fontFamily
    font.pixelSize: Style.font.bodySmall
    wrapMode: Text.WordWrap
  }

  component LastRestore: Column {
    readonly property var r: snap.lastRestore
    spacing: Style.spacing.labelGap

    PlainLine {
      text: r ? strings.t(r.trigger === "login" ? "restore.title.login" : "restore.title.manual")
        + " (" + Model.sessionTitle(r.session, strings.t) + ")" : ""
      color: root.foreground
      opacity: 0.6
    }

    PlainLine {
      visible: r !== null && (r.back > 0 || r.errors.length === 0)
      text: r ? root.restoreSummary(r) : ""
      color: root.foreground
    }

    PlainLine {
      visible: r !== null && r.kept.length > 0
      text: r ? strings.t("restore.kept").arg(r.kept.join(", ")) : ""
      color: root.urgent
    }

    PlainLine {
      visible: r !== null && r.problems.length > 0
      text: r ? strings.tn("restore.problems", r.problems.length).arg(r.problems.length)
        .arg(r.problems.map(function (p) { return p.app }).join(", ")) : ""
      color: root.urgent
    }

    Repeater {
      model: r ? r.errors : []
      PlainLine {
        required property var modelData
        text: modelData
        color: root.urgent
      }
    }
  }

  component SessionRow: CursorSurface {
    id: sessionRow
    property var session: null
    readonly property string rowId: "session:" + (session ? session.name : "")
    readonly property bool own: session ? Model.isOwn(session.name) : true
    readonly property bool restoringThis: snap.restoringName === (session ? session.name : "")

    hasCursor: root.currentRow === rowId
    foreground: root.foreground
    implicitHeight: sessionLayout.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: snap.busy || session.error ? Qt.ArrowCursor : Qt.PointingHandCursor
      enabled: !session.error
      onEntered: root.setRowCursor(sessionRow.rowId)
      onClicked: root.switchTo(session.name)
    }

    RowLayout {
      id: sessionLayout
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(8)

      Text {
        textFormat: Text.PlainText
        text: Model.glyphFor(session.name)
        color: root.foreground
        opacity: sessionRow.restoringThis ? 0.5 : 1
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
        Layout.alignment: Qt.AlignVCenter
      }

      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: Model.sessionTitle(session.name, strings.t)
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: session.error
            ? strings.t("session.unreadable").arg(session.error)
            : strings.t("session.meta")
                .arg(strings.tn("session.windows", session.windows).arg(session.windows))
                .arg(root.when(session.savedAt))
          color: session.error ? root.urgent : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          visible: !session.error && session.apps.length > 0
          text: Model.appsLine(session.apps, 4, strings.t)
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      PanelActionButton {
        visible: !session.error
        iconText: Model.GLYPH.addOnly
        tooltipText: strings.t("session.addHint")
        foreground: root.foreground
        fontFamily: root.fontFamily
        enabled: !snap.busy
        Layout.alignment: Qt.AlignVCenter
        onHovered: function (on) { if (on) root.setRowCursor(sessionRow.rowId) }
        onClicked: root.restore(session.name, false)
      }

      PanelActionButton {
        visible: !sessionRow.own
        iconText: Model.GLYPH.remove
        tooltipText: strings.t("session.deleteHint")
        foreground: root.foreground
        fontFamily: root.fontFamily
        Layout.alignment: Qt.AlignVCenter
        onHovered: function (on) { if (on) root.setRowCursor(sessionRow.rowId) }
        onClicked: snap.remove(session.name)
      }
    }
  }
}
