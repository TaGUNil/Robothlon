#!/usr/bin/env python3

import sys
import time
import re
from collections import deque, OrderedDict
from enum import Enum

from PyQt5 import QtCore, QtGui, QtWidgets, QtSerialPort

from ui_mainwindow import Ui_MainWindow


def format_time(seconds):
    return str(QtCore.QTime(0, 0, 0, 0).addSecs(seconds).toString("HH:mm:ss"))


class DeviceState(Enum):
    unknown = -1
    previous = 0
    paused = 1
    operational = 2
    reload = 3
    damaged = 4
    destroyed = 5


class DeviceType(Enum):
    tank = 0
    target = 1
    turret = 2


class DeviceMode(Enum):
    training = 0
    combat = 1


class DeviceParam(Enum):
    DefaultHitCnt = 1
    CurrentHitCnt = 2
    Group = 3
    IRPower = 4
    IRDamage = 5
    ReloadTime = 6
    RepairTime = 7


class Device(object):
    def __init__(self, device_id=None):
        self.id = device_id
        self.type = None
        self.group = None
        self.mode = None
        self.state = None
        self.health = None
        self.time = None
        self.missing_in_action = False


class PortManager(QtCore.QObject):
    connected = QtCore.pyqtSignal(bool)
    disconnected = QtCore.pyqtSignal(bool)

    message = QtCore.pyqtSignal(str)

    def __init__(self,
                 serial_port):
        super().__init__()

        self._serial_port = serial_port

        self._port_name = ""

    def set_port_name(self, port_name):
        self._port_name = str(port_name)

    def connect(self):
        port_name = self._port_name
        baud_rate = 115200
        
        self._serial_port.setPortName(port_name)
        self._serial_port.setBaudRate(baud_rate)

        if self._serial_port.open(QtCore.QIODevice.ReadWrite) != True:
            text = "Ошибка: не удалось открыть порт {}".format(port_name)
            self.message.emit(text)
            return

        self._serial_port.readAll()

        self.connected.emit(True)

        text = "Порт {} открыт".format(port_name)
        self.message.emit(text)

    def disconnect(self):
        self._serial_port.readAll()

        self._serial_port.close()

        self.disconnected.emit(True)

        text = ""
        self.message.emit(text)


class CommandTransport(QtCore.QObject):
    class State(Enum):
        disabled = 0
        idle = 1
        work = 2

    COMMAND_DELAY = 10

    def __init__(self,
                 serial_port):
        super().__init__()

        self._serial_port = serial_port

        self._command_queue = deque()

        self._timer = QtCore.QTimer()
        self._timer.setSingleShot(True)
        self._timer.setTimerType(QtCore.Qt.PreciseTimer)
        self._timer.setInterval(self.COMMAND_DELAY)

        self._unsent_bytes = 0

        self._state = self.State.disabled

        self._read_complete = False
        self._write_complete = False

        self._serial_port.bytesWritten.connect(self._handle_write)
        self._serial_port.readyRead.connect(self._handle_read)
        self._timer.timeout.connect(self._process_request)

    def enable(self):
        if self._state is not self.State.disabled:
            return

        self._state = self.State.idle

        command = "Ping\r\n"
        self.send_command(command, None)

        if len(self._command_queue) != 0:
            self._process_request()

    def disable(self):
        self._timer.stop()

        self._state = self.State.disabled

    def clear(self):
        self._timer.stop()

        self._command_queue.clear()

        self._unsent_bytes = 0

    def send_command(self, request, callback):
        self._command_queue.append({'request': request,
                                    'callback': callback})

        if self._state is self.State.idle:
            self._timer.start()

    def _process_request(self):
        if self._state is not self.State.idle:
            return

        if not self._serial_port.isOpen():
            self._state = self.State.disabled
            return

        data = bytes(self._command_queue[0]['request'], 'ascii')

        self._unsent_bytes = len(data)

        self._state = self.State.work

        self._read_complete = False
        self._write_complete = False

        self._serial_port.readAll()

        #print(data)
        self._serial_port.write(data)

    def _handle_write(self, sent_bytes):
        if self._state is not self.State.work:
            return

        self._unsent_bytes -= sent_bytes

        if self._unsent_bytes == 0:
            self._write_complete = True
            if self._read_complete:
                self._finalize_request()

    def _handle_read(self):
        if self._state is not self.State.work:
            return

        if self._serial_port.canReadLine():
            response = bytes(self._serial_port.readLine()).decode('ascii')
            response = response.strip()

            if len(response) != 0:
                #print(response)
                callback = self._command_queue[0]['callback']
                if callback is not None:
                    callback(response)

                self._read_complete = True
                if self._write_complete:
                    self._finalize_request()


    def _finalize_request(self):
        if self._state is not self.State.work:
            return

        self._command_queue.popleft()

        self._state = self.State.idle

        if len(self._command_queue) != 0:
            self._timer.start()


class DeviceManager(QtCore.QObject):
    FIRST_DEVICE = 1
    LAST_DEVICE = 31

    device_updated = QtCore.pyqtSignal(Device)

    upload_started = QtCore.pyqtSignal(bool)
    upload_finished = QtCore.pyqtSignal(bool)

    message = QtCore.pyqtSignal(str)

    def __init__(self,
                 command_transport,
                 settings_manager):
        super().__init__()

        self._command_transport = command_transport
        self._settings_manager = settings_manager

        field_count = 6
        info_regexp = r"^"
        for i in range(field_count):
            info_regexp += r"(\d+)"
            if i != field_count - 1:
                info_regexp += r"[\s,]+"

        self._info_parser = re.compile(info_regexp,
                                       re.ASCII)

        self._devices = {}

        self._current_device = self.FIRST_DEVICE

        self._upload = False
        self._first_uploaded = None

        self._enabled = False

    def enable(self):
        if self._enabled:
            return

        self._enabled = True

        self._query_next_device()

    def disable(self):
        self._enabled = False

    def clear(self):
        self._devices.clear()

        self._current_device = self.FIRST_DEVICE

    def _query_next_device(self):
        if not self._enabled:
            return

        command = "GetInfo {:d}\r\n".format(self._current_device)
        self._command_transport.send_command(command,
                                             self._process_query_response)

        if self._upload:
            if self._first_uploaded is not None:
                if self._first_uploaded == self._current_device:
                    self._first_uploaded = None
                    self._upload = False
                    self.upload_finished.emit(True)

        if self._upload:
            if self._first_uploaded is None:
                self._first_uploaded = self._current_device

            if self._current_device in self._devices:
                device = self._devices[self._current_device]
                if not device.missing_in_action:
                    settings_manager = self._settings_manager
                    params = settings_manager.get_device_params(device.type,
                                                                device.id)
                    for (param_name, param_value) in params.items():
                        try:
                            param_id = DeviceParam[param_name].value
                        except ValueError:
                            param_id = None

                        if param_id is not None:
                            command = "SetParameter {:d}, {:d}, {:d}\r\n"
                            command = command.format(self._current_device,
                                                     param_id,
                                                     param_value)
                            self._command_transport.send_command(command, None)

    def _process_query_response(self, response):
        if response == "Ack 2":
            if self._current_device in self._devices:
                self._devices[self._current_device].missing_in_action = True

                self.device_updated.emit(self._devices[self._current_device])
        else:
            match = self._info_parser.match(response)
            if match:
                valid = True

                try:
                    device_type = DeviceType(int(match.group(1)))
                except ValueError:
                    device_type = None
                    valid = False

                device_group = int(match.group(2))
                if device_group < 0 or device_group > 7:
                    device_group = None
                    valid = False

                try:
                    device_mode = DeviceMode(int(match.group(3)))
                except ValueError:
                    device_mode = None
                    valid = False

                try:
                    device_state = DeviceState(int(match.group(4)))
                except ValueError:
                    device_state = None
                    valid = False

                device_health = int(match.group(5))
                if device_health < 0 or device_health > 255:
                    device_health = None
                    valid = False

                device_time = int(match.group(6))
                if device_time < 0:
                    device_time = None
                    valid = False

                if valid:
                    device = Device(self._current_device)

                    device.id = self._current_device
                    device.type = device_type
                    device.group = device_group
                    device.mode = device_mode
                    device.state = device_state
                    device.health = device_health
                    device.time = device_time
                    device.missing_in_action = False

                    self._devices[self._current_device] = device

                    self.device_updated.emit(device)

        self._current_device += 1
        if self._current_device > self.LAST_DEVICE:
            self._current_device = self.FIRST_DEVICE

        if self._enabled:
            self._query_next_device()

    def set_mode(self, device_id, mode):
        if not self._enabled:
            return

        command = "SetMode {:d}, {:d}\r\n".format(device_id,
                                                  mode.value)
        self._command_transport.send_command(command,
                                             self._process_set_mode_response)

    def _process_set_mode_response(self, response):
        if response != "Ack 0":
            self.message.emit("Ошибка: не удалось переключить режим устройства")    

    def upload_settings(self):
        self._upload = True

        self.upload_started.emit(True)


class GameManager(QtCore.QObject):
    class State(Enum):
        disabled = 0
        idle = 1
        ready = 2
        running = 3
        paused = 4

    game_resetted = QtCore.pyqtSignal(bool)
    game_started = QtCore.pyqtSignal(bool)
    game_stopped = QtCore.pyqtSignal(bool)
    game_paused = QtCore.pyqtSignal(bool)
    game_unpaused = QtCore.pyqtSignal(bool)

    enabled = QtCore.pyqtSignal(bool)
    disabled = QtCore.pyqtSignal(bool)

    time = QtCore.pyqtSignal(str)

    message = QtCore.pyqtSignal(str)

    def __init__(self,
                 command_transport,
                 settings_manager):
        super().__init__()

        self._command_transport = command_transport
        self._settings_manager = settings_manager

        self._game_duration = self._settings_manager.get_game_duration()

        self._timer = QtCore.QTimer()
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._timer_callback)

        self._elapsed_timer = QtCore.QElapsedTimer()
        self._prev_elapsed_time = 0

        self._state = self.State.disabled

    def _timer_callback(self):
        seconds = self.refresh_time()

        if self._state is self.State.running:
            if seconds >= self._game_duration:
                self.stop_game()

    def refresh_time(self):
        elapsed_time = self._prev_elapsed_time
        if self._elapsed_timer.isValid():
            elapsed_time += self._elapsed_timer.elapsed()

        seconds = elapsed_time // 1000

        self.time.emit(format_time(seconds))

        return seconds

    def _start_timer(self, reset):
        if reset:
            self._prev_elapsed_time = 0

        self.refresh_time()

        self._elapsed_timer.start()
        self._timer.start()

    def _stop_timer(self, reset):
        self._timer.stop()

        if reset:
            self._prev_elapsed_time = 0
        elif self._elapsed_timer.isValid():
            self._prev_elapsed_time += self._elapsed_timer.elapsed()

        self._elapsed_timer.invalidate()

        self.refresh_time()

    def enable(self):
        if self._state is not self.State.disabled:
            return

        self._stop_timer(reset=True)

        self._state = self.State.idle

        self.enabled.emit(True)

    def disable(self):
        self._stop_timer(reset=True)

        self._state = self.State.disabled

        self.disabled.emit(True)

    def reset_game(self):
        if self._state is self.State.disabled:
            return

        command = "Reset4Combat\r\n"
        self._command_transport.send_command(command,
                                             self._process_reset_response)

    def _process_reset_response(self, response):
        if response == "Ack 0":
            self._stop_timer(reset=True)
            self._state = self.State.ready
            self.game_resetted.emit(True)
        else:
            self.message.emit("Ошибка: не удалось выполнить сброс")

    def start_game(self):
        if self._state is not self.State.ready:
            return

        device_state = DeviceState.operational
        command = "SetState4Combat {:d}\r\n".format(device_state.value)
        self._command_transport.send_command(command,
                                             self._process_start_response)

    def _process_start_response(self, response):
        if response == "Ack 0":
            self._start_timer(reset=True)
            self._state = self.State.running
            self.game_started.emit(True)
        else:
            self.message.emit("Ошибка: не удалось начать игру")
    
    def stop_game(self):
        if (self._state is not self.State.running and
            self._state is not self.State.paused):
            return

        device_state = DeviceState.destroyed
        command = "SetState4Combat {:d}\r\n".format(device_state.value)
        self._command_transport.send_command(command,
                                             self._process_stop_response)

    def _process_stop_response(self, response):
        if response == "Ack 0":
            self._stop_timer(reset=False)
            self._state = self.State.idle
            self.game_stopped.emit(True)
        else:
            self.message.emit("Ошибка: не удалось остановить игру")

    def pause_game(self):
        if (self._state is not self.State.running and
            self._state is not self.State.paused):
            return

        if self._state is self.State.running:
            device_state = DeviceState.paused
            command = "SetState4Combat {:d}\r\n".format(device_state.value)
            self._command_transport.send_command(command,
                                                 self._process_pause_response)
        elif self._state is self.State.paused:
            device_state = DeviceState.previous
            command = "SetState4Combat {:d}\r\n".format(device_state.value)
            self._command_transport.send_command(command,
                                                 self._process_unpause_response)

    def _process_pause_response(self, response):
        if response == "Ack 0":
            self._stop_timer(reset=False)
            self._state = self.State.paused
            self.game_paused.emit(True)
        else:
            self.message.emit("Ошибка: не удалось приостановить игру")

    def _process_unpause_response(self, response):
        if response == "Ack 0":
            self._start_timer(reset=False)
            self._state = self.State.running
            self.game_unpaused.emit(True)
        else:
            self.message.emit("Ошибка: не удалось продолжить игру")


class SettingsManager(QtCore.QObject):
    DEFAULT_GAME_DURATION = 10 * 60

    def __init__(self):
        super().__init__()

        self._settings = None

    def load(self, file_name):
        self._settings = QtCore.QSettings(file_name,
                                          QtCore.QSettings.IniFormat)

        if self._settings.status() != QtCore.QSettings.NoError:
            self._settings = None
            return False

        return True

    def get_game_duration(self):
        return int(self._settings.value('GameDuration',
                                        self.DEFAULT_GAME_DURATION))

    def get_device_params(self, device_type, device_id):
        params = {}

        if self._settings is not None:
            self._settings.sync()

            self._settings.beginGroup('Default')
            for key in self._settings.childKeys():
                params[str(key)] = int(self._settings.value(key))
            self._settings.endGroup()

            self._settings.beginGroup('Type_{:d}'.format(device_type.value))
            for key in self._settings.childKeys():
                params[str(key)] = int(self._settings.value(key))
            self._settings.endGroup()

            self._settings.beginGroup('Device_{:d}'.format(device_id))
            for key in self._settings.childKeys():
                params[str(key)] = int(self._settings.value(key))
            self._settings.endGroup()

        return params


class DeviceView(QtCore.QObject):
    class Column(Enum):
        group = 0
        state = 1
        health = 2
        mode = 3

    set_mode = QtCore.pyqtSignal(int, DeviceMode)

    def __init__(self,
                 tank_table,
                 turret_table,
                 target_table):
        super().__init__()

        self._tables = {DeviceType.tank: tank_table,
                        DeviceType.turret: turret_table,
                        DeviceType.target: target_table}

        for table in [tank_table, turret_table, target_table]:
            table.itemChanged.connect(self._item_changed_callback)

        self._devices = {DeviceType.tank: {},
                         DeviceType.turret: {},
                         DeviceType.target: {}}

        self._rows = {DeviceType.tank: {},
                      DeviceType.turret: {},
                      DeviceType.target: {}}

        self._colors = False

    def _item_changed_callback(self, item):
        table = item.tableWidget()
        column = item.column()
        row = item.row()

        if column != self.Column.mode.value:
            return

        if item.checkState() == QtCore.Qt.Checked:
            mode = DeviceMode.combat
        else:
            mode = DeviceMode.training

        device_type = None
        device_id = None

        for (key, value) in self._tables.items():
            if table is value:
                device_type = key

        if device_type is not None:
            for (key, value) in self._rows[device_type].items():
                if row == value:
                    device_id = key

        if device_id is not None:
            device = self._devices[device_type][device_id]
            if mode is not device.mode:
                self.set_mode.emit(device_id, mode)

    def enable_colors(self):
        self._colors = True

    def disable_colors(self):
        self._colors = False

    def update_device(self, device):
        table = self._tables[device.type]

        if device.id not in self._devices[device.type]:
            row = table.rowCount()
            table.setRowCount(row + 1)

            item = QtWidgets.QTableWidgetItem("{:d}".format(device.id))
            table.setVerticalHeaderItem(row, item)

            item = QtWidgets.QTableWidgetItem("")
            item.setFlags(QtCore.Qt.ItemIsEnabled)
            item.setTextAlignment(QtCore.Qt.AlignCenter)
            table.setItem(row, self.Column.group.value, item)

            item = QtWidgets.QTableWidgetItem("")
            item.setFlags(QtCore.Qt.ItemIsEnabled)
            item.setTextAlignment(QtCore.Qt.AlignCenter)
            table.setItem(row, self.Column.state.value, item)

            item = QtWidgets.QTableWidgetItem("")
            item.setFlags(QtCore.Qt.ItemIsEnabled)
            item.setTextAlignment(QtCore.Qt.AlignCenter)
            table.setItem(row, self.Column.health.value, item)

            item = QtWidgets.QTableWidgetItem("")
            item.setFlags(QtCore.Qt.ItemIsEnabled |
                          QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.Unchecked)
            item.setTextAlignment(QtCore.Qt.AlignCenter)
            table.setItem(row, self.Column.mode.value, item)

            self._rows[device.type][device.id] = row
        else:
            row = self._rows[device.type][device.id]

        self._devices[device.type][device.id] = device

        if self._colors:
            if device.state is DeviceState.operational:
                color = 'green'
            elif device.state is DeviceState.reload:
                color = 'green'
            elif device.state is DeviceState.damaged:
                color = 'yellow'
            elif device.state is DeviceState.destroyed:
                color = 'red'
            else:
                color = 'black'
        else:
            color = 'black'

        font = QtGui.QFont()
        font.setBold(True)

        text = "{:d}".format(device.group)
        item = table.item(row, self.Column.group.value)
        item.setText(text)
        item.setFont(font)
        if not device.missing_in_action:
            item.setFlags(QtCore.Qt.ItemIsEnabled)
        else:
            item.setFlags(QtCore.Qt.NoItemFlags)

        text = "{}".format(device.state.name.upper())
        item = table.item(row, self.Column.state.value)
        item.setText(text)
        item.setFont(font)
        item.setForeground(QtGui.QBrush(QtGui.QColor(color)))
        if not device.missing_in_action:
            item.setFlags(QtCore.Qt.ItemIsEnabled)
        else:
            item.setFlags(QtCore.Qt.NoItemFlags)

        text = "{:d}".format(device.health)
        item = table.item(row, self.Column.health.value)
        item.setText(text)
        item.setFont(font)
        item.setForeground(QtGui.QBrush(QtGui.QColor(color)))
        if not device.missing_in_action:
            item.setFlags(QtCore.Qt.ItemIsEnabled)
        else:
            item.setFlags(QtCore.Qt.NoItemFlags)

        item = table.item(row, self.Column.mode.value)
        if device.mode is DeviceMode.combat:
            item.setCheckState(QtCore.Qt.Checked)
        else:
            item.setCheckState(QtCore.Qt.Unchecked)
        if not device.missing_in_action:
            item.setFlags(QtCore.Qt.ItemIsEnabled |
                          QtCore.Qt.ItemIsUserCheckable)
        else:
            item.setFlags(QtCore.Qt.ItemIsUserCheckable)


def main(argv):
    application = QtWidgets.QApplication(argv)

    main_window = QtWidgets.QMainWindow()

    main_window.ui = Ui_MainWindow()
    main_window.ui.setupUi(main_window)

    combo_box = main_window.ui.serialPortComboBox

    connect_button = main_window.ui.connectPushButton
    disconnect_button = main_window.ui.disconnectPushButton

    upload_button = main_window.ui.uploadPushButton
    reset_button = main_window.ui.resetPushButton
    start_button = main_window.ui.startPushButton
    stop_button = main_window.ui.stopPushButton
    pause_button = main_window.ui.pausePushButton

    clock_lcd_number = main_window.ui.clockLcdNumber
    clock_lcd_number.display(format_time(0))

    status_bar = main_window.ui.statusbar

    tank_table = main_window.ui.tankTableWidget
    turret_table = main_window.ui.turretTableWidget
    target_table = main_window.ui.targetTableWidget

    for table in [tank_table, turret_table, target_table]:
        table.setColumnWidth(0, 70)
        table.setColumnWidth(1, 110)
        table.setColumnWidth(2, 70)
        table.setColumnWidth(3, 70)

    serial_port = QtSerialPort.QSerialPort()

    port_manager = PortManager(serial_port)

    command_transport = CommandTransport(serial_port)

    settings_manager = SettingsManager()
    if not settings_manager.load("default.ini"):
        status_bar.showMessage("Ошибка: не удалось открыть файл конфигурации")

    device_manager = DeviceManager(command_transport, settings_manager)

    device_view = DeviceView(tank_table, turret_table, target_table)

    game_manager = GameManager(command_transport, settings_manager)

    ports = QtSerialPort.QSerialPortInfo.availablePorts()
    for port in ports:
        combo_box.addItem(port.portName())
    port_manager.set_port_name(combo_box.currentText())

    combo_box.currentTextChanged.connect(port_manager.set_port_name)

    connect_button.clicked.connect(port_manager.connect)

    disconnect_button.clicked.connect(port_manager.disconnect)

    port_manager.connected.connect(combo_box.setDisabled)
    port_manager.connected.connect(connect_button.setDisabled)
    port_manager.connected.connect(disconnect_button.setEnabled)
    port_manager.connected.connect(clock_lcd_number.setEnabled)
    port_manager.connected.connect(command_transport.enable)
    port_manager.connected.connect(device_manager.enable)
    port_manager.connected.connect(game_manager.enable)

    port_manager.disconnected.connect(disconnect_button.setDisabled)
    port_manager.disconnected.connect(connect_button.setEnabled)
    port_manager.disconnected.connect(combo_box.setEnabled)
    port_manager.disconnected.connect(clock_lcd_number.setDisabled)
    port_manager.disconnected.connect(command_transport.disable)
    port_manager.disconnected.connect(device_manager.disable)
    port_manager.disconnected.connect(game_manager.disable)
    port_manager.disconnected.connect(command_transport.clear)
    port_manager.disconnected.connect(device_manager.clear)

    port_manager.message.connect(status_bar.showMessage)

    device_manager.device_updated.connect(device_view.update_device)

    device_manager.upload_started.connect(upload_button.setDisabled)

    device_manager.upload_finished.connect(upload_button.setEnabled)

    device_manager.message.connect(status_bar.showMessage)

    device_view.set_mode.connect(device_manager.set_mode)

    game_manager.enabled.connect(upload_button.setEnabled)
    game_manager.enabled.connect(reset_button.setEnabled)

    game_manager.disabled.connect(upload_button.setDisabled)
    game_manager.disabled.connect(reset_button.setDisabled)
    game_manager.disabled.connect(start_button.setDisabled)
    game_manager.disabled.connect(stop_button.setDisabled)
    game_manager.disabled.connect(pause_button.setDisabled)
    game_manager.disabled.connect(lambda: pause_button.setChecked(False))
    game_manager.disabled.connect(device_view.disable_colors)

    game_manager.game_resetted.connect(start_button.setEnabled)
    game_manager.game_resetted.connect(stop_button.setDisabled)
    game_manager.game_resetted.connect(pause_button.setDisabled)
    game_manager.game_resetted.connect(lambda: pause_button.setChecked(False))
    game_manager.game_resetted.connect(device_view.disable_colors)

    game_manager.game_started.connect(start_button.setDisabled)
    game_manager.game_started.connect(stop_button.setEnabled)
    game_manager.game_started.connect(pause_button.setEnabled)
    game_manager.game_started.connect(lambda: pause_button.setChecked(False))
    game_manager.game_started.connect(device_view.enable_colors)

    game_manager.game_paused.connect(start_button.setDisabled)
    game_manager.game_paused.connect(stop_button.setEnabled)
    game_manager.game_paused.connect(pause_button.setEnabled)
    game_manager.game_paused.connect(lambda: pause_button.setChecked(True))

    game_manager.game_unpaused.connect(start_button.setDisabled)
    game_manager.game_unpaused.connect(stop_button.setEnabled)
    game_manager.game_unpaused.connect(pause_button.setEnabled)
    game_manager.game_unpaused.connect(lambda: pause_button.setChecked(False))

    game_manager.game_stopped.connect(start_button.setDisabled)
    game_manager.game_stopped.connect(stop_button.setDisabled)
    game_manager.game_stopped.connect(pause_button.setDisabled)
    game_manager.game_stopped.connect(lambda: pause_button.setChecked(False))
    game_manager.game_stopped.connect(device_view.disable_colors)

    game_manager.message.connect(status_bar.showMessage)

    game_manager.time.connect(clock_lcd_number.display)

    upload_button.clicked.connect(device_manager.upload_settings)

    reset_button.clicked.connect(game_manager.reset_game)

    start_button.clicked.connect(game_manager.start_game)

    stop_button.clicked.connect(game_manager.stop_game)

    pause_button.clicked.connect(game_manager.pause_game)

    main_window.show()

    application.exec_()


if __name__ == "__main__":
    main(sys.argv)
