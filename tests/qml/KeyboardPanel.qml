import QtQuick

// Stands in for qs.Ui's KeyboardPanel in the test harness. The real one is a
// layer-shell window, which needs a Wayland compositor; the panel's logic
// doesn't. Same properties and sizing helpers the panel uses, and the lazy
// content is built straight away so the tests can reach it.
Item {
  id: root
  required property Item anchorItem
  required property QtObject bar
  property var owner: null
  property bool open: false
  property bool centerOnBar: false
  property Item focusTarget: null
  property int contentWidth: 380
  property int contentHeight: 560
  property Component content: null
  readonly property Item loadedContent: contentLoader.item
  readonly property real loadedContentHeight: contentLoader.item ? contentLoader.item.implicitHeight : 0
  width: contentWidth
  height: contentHeight

  function fittedContentWidth(width, cap) {
    var desired = Math.max(1, Number(width) || 1)
    return Math.round(cap !== undefined && Number(cap) > 0 ? Math.min(desired, Number(cap)) : desired)
  }
  function fittedContentHeight(implicitHeight, cap) {
    var desired = Math.max(1, Number(implicitHeight) || 1)
    return Math.round(cap !== undefined && Number(cap) > 0 ? Math.min(desired, Number(cap)) : desired)
  }

  Loader {
    id: contentLoader
    anchors.fill: parent
    sourceComponent: root.content
  }
}
