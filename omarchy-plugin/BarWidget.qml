import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "quantumfire.iphone-mirror"

  property var status: ({running: false, state: "stopped", error: null})
  property string uiError: ""
  property string lastNotifiedError: ""
  property int selectedIndex: 0
  property bool cursorActive: false
  readonly property string helper: Qt.resolvedUrl("plugin_control.py").toString().replace("file://", "")
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property bool changing: status.state === "starting" || status.state === "stopping"
  readonly property color stateColor: status.state === "error" ? Color.urgent
    : changing ? "#e5b567" : status.running ? "#73c991" : foreground
  readonly property string stateText: status.state === "starting" ? "Starting"
    : status.state === "running" ? "Running"
    : status.state === "stopping" ? "Stopping"
    : status.state === "disconnected" ? "iPhone disconnected"
    : status.state === "error" ? "Error"
    : "Stopped"

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function notifyError(message) {
    var clean = (message || "").trim()
    if (clean === "" || clean === lastNotifiedError) return
    lastNotifiedError = clean
    notifyProc.command = ["notify-send", "-a", "iPhone Mirror", "iPhone Mirror", clean]
    notifyProc.running = true
  }

  function acceptStatus(value) {
    var states = ["starting", "running", "stopping", "disconnected", "error", "stopped"]
    if (!value || typeof value.running !== "boolean"
        || states.indexOf(value.state) < 0
        || !(value.error === null || typeof value.error === "string"))
      throw new Error("iPhone Mirror returned invalid status data.")
    status = value
    uiError = value.error || (value.state === "error" ? "iPhone Mirror reported an error." : "")
    if (uiError !== "") notifyError(uiError)
    else lastNotifiedError = ""
  }

  function refresh() {
    if (!statusProc.running) statusProc.running = true
  }

  function action(name) {
    if (actionProc.running) return
    uiError = ""
    close()
    actionProc.command = ["python3", helper, name]
    actionProc.running = true
  }

  function activateSelected() {
    if (selectedIndex === 0) action("start")
    else action("stop")
  }

  Timer {
    interval: 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  onOpenedChanged: if (opened) {
    cursorActive = false
    selectedIndex = 0
    refresh()
  }

  Process {
    id: statusProc
    command: ["python3", root.helper, "status"]
    stdout: StdioCollector { id: statusOutput; waitForEnd: true }
    stderr: StdioCollector { id: statusError; waitForEnd: true }
    onExited: function(code) {
      if (code !== 0) {
        root.uiError = statusError.text.trim() || "Could not read iPhone Mirror status."
        root.status = {running: false, state: "error", error: root.uiError}
        root.notifyError(root.uiError)
        return
      }
      try {
        root.acceptStatus(JSON.parse(statusOutput.text))
      } catch (error) {
        root.uiError = error.message
        root.status = {running: false, state: "error", error: root.uiError}
        root.notifyError(root.uiError)
      }
    }
  }

  Process {
    id: actionProc
    stderr: StdioCollector { id: actionError; waitForEnd: true }
    onExited: function(code) {
      if (code !== 0) {
        root.uiError = actionError.text.trim() || "iPhone Mirror action failed."
        root.notifyError(root.uiError)
      }
      root.refresh()
    }
  }

  Process { id: notifyProc }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰄜"
    foreground: root.stateColor
    dimmed: !root.status.running
    tooltipText: "iPhone Mirror\n" + root.stateText
    onPressed: root.toggle()
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: catcher
    contentWidth: panel.fittedContentWidth(Style.space(340))
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(420))

    PanelKeyCatcher {
      id: catcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        root.cursorActive = true
        root.selectedIndex = Math.max(0, Math.min(1, root.selectedIndex + dy))
      }
      onActivateRequested: root.activateSelected()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: content
        width: parent.width
        spacing: Style.space(12)

        PanelHero {
          width: parent.width
          title: "iPhone Mirror"
          meta: root.stateText
          foreground: root.foreground
          fontFamily: root.fontFamily
          iconComponent: Component {
            Text {
              text: "󰄜"
              color: root.stateColor
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
            }
          }
        }

        Text {
          width: parent.width
          text: root.status.running ? "The mirror process is running." : "The mirror process is not running."
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          wrapMode: Text.WordWrap
        }

        Button {
          width: parent.width
          text: root.status.running ? "Open or focus mirror" : "Start mirror"
          foreground: root.foreground
          hasCursor: root.cursorActive && root.selectedIndex === 0
          enabled: !actionProc.running && root.status.state !== "stopping"
          onClicked: root.action("start")
        }

        Button {
          width: parent.width
          text: "Stop mirror"
          foreground: root.foreground
          hasCursor: root.cursorActive && root.selectedIndex === 1
          enabled: !actionProc.running && root.status.running
          onClicked: root.action("stop")
        }

        Text {
          width: parent.width
          text: root.uiError
          visible: text !== ""
          color: Color.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }
      }
    }
  }
}
