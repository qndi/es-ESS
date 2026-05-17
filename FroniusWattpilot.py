
from builtins import int
from collections import deque
from enum import Enum
from math import floor
import os
import platform
import sys
import time

import paho.mqtt.client as mqtt # type: ignore

# victron
sys.path.insert(1, '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python')
from vedbus import VeDbusService # type: ignore

# esEss imports
import Globals
import Helper
from Helper import i, c, d, w, e, t,  dbusConnection
from Wattpilot import Wattpilot
from enums import WattpilotModelStatus, WattpilotStartStop, WattpilotControlMode, VrmEvChargerControlMode, VrmEvChargerStatus, VrmEvChargerStartStop
from esESSService import esESSService

class FroniusWattpilot (esESSService):
    
    def __init__(self):
        esESSService.__init__(self)
        self.vrmInstanceID = self.config['FroniusWattpilot']['VRMInstanceID']
        self.serviceType = "com.victronenergy.evcharger"
        self.serviceName = self.serviceType + "." + Globals.esEssTagService + "_FroniusWattpilot"
        self.minimumOnOffSeconds = int(self.config["FroniusWattpilot"]["MinOnOffSeconds"])

        # 1→3 phase: minimum continuous seconds of 3-phase demand before actually switching
        self.minimumPhaseSwitchSeconds = int(self.config["FroniusWattpilot"]["MinPhaseSwitchSeconds"])
        # 3→1 phase: minimum seconds to stay on 3-phase before switching back
        self.minimumPhaseSwitchSecondsThreeToOne = int(self.config["FroniusWattpilot"]["MinPhaseSwitchSecondsThreeToOne"])
        # below this SoC (%), switch 3→1 immediately and enforce strict allowance usage
        self.socProtectionThreshold = int(self.config["FroniusWattpilot"]["SoCProtectionThreshold"])

        self.wattpilot = None
        self.allowance = 0
        self.lastPhaseSwitchTime = 0
        self.lastOnOffTime = 0
        self.lastVarDump = 0
        self.chargingTime = 0
        self.currentPhaseMode = 1 # will be detected later
        self.mode:VrmEvChargerControlMode = VrmEvChargerControlMode.Manual # will be detected later
        self.autostart = 0
        self.noChargeSince = 0 # flag to detect when car is fully charged
        self.isIdleMode = False
        self.isHibernateEnabled = self.config["FroniusWattpilot"]["HibernateMode"].lower() == "true"
        self.mqttAllowanceTopic = 'es-ESS/SolarOverheadDistributor/Requests/Wattpilot/Allowance'

        # 1→3 phase: timestamp when 3-phase demand was first observed while on 1-phase (0 = not active)
        # reset whenever demand drops back to 1-phase — ensures 3-phase only after *sustained* demand
        self.wantThreePhaseSince = 0

        # 3→1 phase: timestamp of the last entry into 3-phase operation (start of the 1h protection window)
        self.lastThreeToOneCooldownStart = 0

        # 3→1 phase: rolling history over the last 10 minutes (120 ticks × 5s).
        # True  = 3-phase still desired that tick
        # False = 1-phase would suffice
        # used after the 1h cooldown to decide whether to stay on 3-phase another hour
        self.phaseCheckHistory: deque = deque(maxlen=120)

    def initDbusService(self):
        self.dbusService = VeDbusService(self.serviceName, bus=dbusConnection(), register=False)

        #dump root information about our service and register paths.
        self.dbusService.add_path('/Mgmt/ProcessName', __file__)
        self.dbusService.add_path('/Mgmt/ProcessVersion', Globals.currentVersionString + ' on Python ' + platform.python_version())
        self.dbusService.add_path('/Mgmt/Connection', "dbus")

        # Create the mandatory objects (plus some extras)
        self.dbusService.add_path('/DeviceInstance', int(self.vrmInstanceID))
        self.dbusService.add_path('/ProductId', 65535)
        self.dbusService.add_path('/ProductName', "Fronius Wattpilot") 
        self.dbusService.add_path('/CustomName', "Fronius Wattpilot") 
        self.dbusService.add_path('/Latency', None)    
        self.dbusService.add_path('/FirmwareVersion', Globals.currentVersionString)
        self.dbusService.add_path('/HardwareVersion', Globals.currentVersionString)
        self.dbusService.add_path('/Connected', 1)
        self.dbusService.add_path('/Serial', "1337")
        self.dbusService.add_path('/LastUpdate', 0)
        self.dbusService.add_path('/Ac/Energy/Forward', 0)
        self.dbusService.add_path('/Ac/L1/Power', 0)
        self.dbusService.add_path('/Ac/L2/Power', 0)
        self.dbusService.add_path('/Ac/L3/Power', 0)
        self.dbusService.add_path('/Ac/L1/Voltage', 0)
        self.dbusService.add_path('/Ac/L2/Voltage', 0)
        self.dbusService.add_path('/Ac/L3/Voltage', 0)
        self.dbusService.add_path('/Ac/L1/Current', 0)
        self.dbusService.add_path('/Ac/L2/Current', 0)
        self.dbusService.add_path('/Ac/L3/Current', 0)
        self.dbusService.add_path('/Ac/L1/PowerFactor', 0)
        self.dbusService.add_path('/Ac/L2/PowerFactor', 0)
        self.dbusService.add_path('/Ac/L3/PowerFactor', 0)
        self.dbusService.add_path('/ChargingTime', self.chargingTime)
        self.dbusService.add_path('/Ac/Power', 0)
        self.dbusService.add_path('/Ac/PowerPercent', 0)
        self.dbusService.add_path('/Ac/PowerMax', 0)
        self.dbusService.add_path('/Current', 0)
        self.dbusService.add_path('/AutoStart', self.autostart, writeable=False)
        self.dbusService.add_path('/SetCurrent', 0, writeable=True, onchangecallback=self._froniusHandleChangedValue)
        self.dbusService.add_path('/Status', 0)
        self.dbusService.add_path('/MaxCurrent', 0)
        self.dbusService.add_path('/Mode', self.mode.value, writeable=True, onchangecallback=self._froniusHandleChangedValue)
        self.dbusService.add_path('/Position', int(self.config['FroniusWattpilot']['Position'])) #
        self.dbusService.add_path('/Model', "Fronius Wattpilot")
        self.dbusService.add_path('/StartStop', 0, writeable=True, onchangecallback=self._froniusHandleChangedValue)

        #Additional Stuff, not required by definition
        self.dbusService.add_path('/CarState', None)
        self.dbusService.add_path('/PhaseMode', None)
        self.dbusService.add_path('/ModeLiteral', VrmEvChargerControlMode(0).name)
        self.dbusService.add_path('/StatusLiteral', VrmEvChargerStatus(0).name)
        self.dbusService.add_path('/StartStopLiteral', VrmEvChargerStartStop(0).name)
        self.dbusService.add_path('/LastChargeModeLiteral', None)

        self.dbusService.register()

    def initDbusSubscriptions(self):
        self.socDbus = self.registerDbusSubscription("com.victronenergy.system", "/Dc/Battery/Soc")

    def initMqttSubscriptions(self):
        self.registerMqttSubscription(self.mqttAllowanceTopic, callback=self.onMqttMessage)

    def initWorkerThreads(self):
        self.registerWorkerThread(self._update, 5000)

    def signOfLive(self):
        pass

    def initFinalize(self):
        #Create the Wattpilot object and connect. 
        self.wattpilot = Wattpilot(self.config["FroniusWattpilot"]["Host"], self.config["FroniusWattpilot"]["Password"])
        self.wattpilot._auto_reconnect = True
        self.wattpilot._reconnect_interval = 30
        self.wattpilot.connect()
        
        #Wait for some information to arrive. 
        Helper.waitTimeout(lambda: self.wattpilot.connected, 30) or e(self, "Unable to connect to wattpilot wthin 30 seconds... Wattpilot offline or credentials wrong?")
        Helper.waitTimeout(lambda: self.wattpilot.power1 is not None, 30) 
        Helper.waitTimeout(lambda: self.wattpilot.power2 is not None, 30) 
        Helper.waitTimeout(lambda: self.wattpilot.power3 is not None, 30) 
        Helper.waitTimeout(lambda: self.wattpilot.carConnected is not None, 30) 
        Helper.waitTimeout(lambda: self.wattpilot.mode is not None, 30) 

        #determine current modes.
        if (self.wattpilot.mode == WattpilotControlMode.ECO):
            self.autostart = 1
            self.mode = VrmEvChargerControlMode.Auto
        else:
            self.autostart = 0
            self.mode = VrmEvChargerControlMode.Manual
            
        self.publishServiceMessage(self, "Mode determined as: {0}".format(self.mode))

        #Adetermine the current phase mode. 
        #if the car is charging, we can do that by looking at the phase power. 
        #if the car is not charging, we can simply force it to be 3 phases, until determined 
        #otherwise. 
        if (self.wattpilot.carConnected and self.wattpilot.power2 > 0):
            self.currentPhaseMode = 2
            self.publishServiceMessage(self, "Currently charging on 3 phases.")
            # start the 3→1 protection window from service boot — we don't know
            # how long 3-phase has been active, so grant a full 1h from now.
            self.lastThreeToOneCooldownStart = time.time()
        elif (self.wattpilot.carConnected and self.wattpilot.power1 > 0):
            self.currentPhaseMode = 1
            self.publishServiceMessage(self, "Currently charging on 1 phase.")
        else:
            self.publishServiceMessage(self, "Currently not charging. Negiotiating automatic phasemode.")
            self.currentPhaseMode = 0
            self.wattpilot.set_phases(0) #autoselect.

        self.dumpEvChargerInfo()

    def _froniusHandleChangedValue(self, path, value):
        i(self, "User/cerbo/vrm updated " + str(path) + " to " + str(value))

        if (path == "/SetCurrent"):
            #Value coming in needs to be cut in third, due to missing 3 phases option on vrm!
            #except value is smaller than single phase maximum! 
            ampPerPhase = int(round(value/3.0)) if value > self.wattpilot.ampLimit else value

            if (value > self.wattpilot.ampLimit):
                self.wattpilot.set_phases(2)
                self.currentPhaseMode = 2
            else:
                self.wattpilot.set_phases(1)
                self.currentPhaseMode = 1

            self.wattpilot.set_power(ampPerPhase)
        elif (path == "/StartStop"):
            state = VrmEvChargerStartStop(value)
            self.dbusService["/StartStopLiteral"] = state.name

            if state == VrmEvChargerStartStop.Start:
                self.wattpilot.set_start_stop(WattpilotStartStop.On)
            elif state == VrmEvChargerStartStop.Stop:
                self.wattpilot.set_start_stop(WattpilotStartStop.Off)
                
        elif (path == "/Mode"):
            priorMode = self.mode
            newMode = VrmEvChargerControlMode(value)
            self.switchMode(priorMode, newMode)
       
        self.dumpEvChargerInfo()
        return True

   # When Mode is switched, different settings needs to be enabled/disabled. 
   # 0 = Manual => User control, only forward commands from VRM to wattpilot and read wattpilotstats.
   # 1 = Automatic => Overhead Mode, disable VRM Control, register pv overhead consumer as auto.
   # 2 = Scheduled => only used to trigger wattpilot from sleepmode.
    def switchMode(self, fromMode:VrmEvChargerControlMode, toMode:VrmEvChargerControlMode):
        # TODO: When we are in hibernate mode, and attempting to switch mode, it fails, because of 
        #       Hibernate. Maybe needs resolution? WakeUp + KeepAlive? -> Would need a generally different
        #       pattern to enter / leave hibernation than the current one. 
        d("FroniusWattpilot", "Switching Mode from {0} to {1}.".format(fromMode, toMode))
        
        self.publishServiceMessage(self, "Switching Mode from {0} to {1}.".format(fromMode, toMode))

        if (toMode == VrmEvChargerControlMode.Auto or toMode == VrmEvChargerControlMode.Manual):
            self.mode = toMode
            self.dbusService["/Mode"] = toMode.value
            self.dbusService["/ModeLiteral"] = toMode.name

            if (fromMode == VrmEvChargerControlMode.Manual and toMode == VrmEvChargerControlMode.Auto):
                self.autostart = 1
                self.wattpilot.set_mode(WattpilotControlMode.ECO)

            elif (fromMode == VrmEvChargerControlMode.Auto and toMode == VrmEvChargerControlMode.Manual):
                self.autostart = 0
                self.wattpilot.set_mode(WattpilotControlMode.Default)

        elif (toMode == VrmEvChargerControlMode.Scheduled):
            #Scheduled Charge - this is not used. We use this to temorary wakeup wattpilot, if in Hibernate mode. 
            self.wakeUpWattpilot()
            self.switchMode(VrmEvChargerControlMode.Scheduled, fromMode)
         
    def wakeUpWattpilot(self):
        self.publishServiceMessage(self, "Connecting to wattpilot to verify car status.")
        self.wattpilot._auto_reconnect=True
        self.wattpilot.connect()
        self.isIdleMode=False

    def _update(self):
        try:
            #if the car is not connected, we can greatly reduce system load.
            #just dump values every 5 minutes then. If car is connected, we need
            #to perform updates every tick.
            if (self.wattpilot.carConnected or not self.isIdleMode or (self.lastVarDump < (time.time() - 300)) or not self.wattpilot.carStateReady):
                # loop, if
                # - Wattpilot reports car conencted
                # - in idle mode every 5 minutes
                # - wattpilot is uncertain about car state.
                self.lastVarDump = time.time()

                #switch idle mode to reduce load, when not required.
                skipIdleCheck = False
                if (self.wattpilot.carStateReady):
                    if (not self.isIdleMode and not self.wattpilot.carConnected):
                        self.publishServiceMessage(self, "Car no longer connected. Switching to Idle-Mode.")
                        if (self.isHibernateEnabled):
                            self.publishServiceMessage(self, "Hibernate is enabled. Disconnecting from wattpilot.")
                            self.wattpilot._auto_reconnect=False
                            self.wattpilot.disconnect()

                    elif (self.isIdleMode):
                        if (self.wattpilot.connected and self.wattpilot.carConnected):
                            self.publishServiceMessage(self, "Car connected. Switching to Operation-Mode.")
                        elif (not self.wattpilot.connected):
                            self.wakeUpWattpilot()
                            skipIdleCheck=True

                            if (Helper.waitTimeout(lambda: self.wattpilot.carStateReady, 30)):
                                if (self.wattpilot.carConnected):
                                    self.publishServiceMessage(self, "Car connected. Entering operation mode.")

                        
                    if (not skipIdleCheck):                    
                        self.isIdleMode = not self.wattpilot.carConnected
                else:
                    d(self, "Car State not yet ready, not performing idle checks.")

                # driving factor for any decission has to be wattpilots modelstatus. Based on the current status, 
                # we need to determine "what to do" and take proper steps to make it happen. 
                # Not every status is important and can be reflected in VRM, so they can be ignored. (else)
                # Modelstatus can be: 
                #   id | Wattpilot Status Text                          VRM id | VRM Status Text
                #   0  | NotChargingBecauseNoChargeCtrlData=0,               0 | Disconnected
                #   1  | NotChargingBecauseOvertemperature=1,           
                #   2  | NotChargingBecauseAccessControlWait=2, 
                #   3  | ChargingBecauseForceStateOn=3,                       2 | Charging (usually we are here in automatic control)
                #                                                                 When attempting to start/stop, report 21/24 start/stop charging.
                #   4  | NotChargingBecauseForceStateOff=4,                   1,4 | if in auto mode, we report 4 (waiting for sun), in manual mode, that's just a 1 (connected)
                #   5  | NotChargingBecauseScheduler=5, 
                #   6  | NotChargingBecauseEnergyLimit=6,
                #   7  | ChargingBecauseAwattarPriceLow=7,                    2 | Charging (no control required here, using low-price feature)
                #   8  | ChargingBecauseAutomaticStopTestLadung=8,            2 | Charging
                #   9  | ChargingBecauseAutomaticStopNotEnoughTime=9,         2 | Charging 
                #   10 | ChargingBecauseAutomaticStop=10,                     2 | Charging 
                #   11 | ChargingBecauseAutomaticStopNoClock=11,              2 | Charging
                #   12 | ChargingBecausePvSurplus=12,                         2 | Charging
                #   13 | ChargingBecauseFallbackGoEDefault=13,                2 | Charging
                #   14 | ChargingBecauseFallbackGoEScheduler=14,              2 | Charging
                #   15 | ChargingBecauseFallbackDefault=15,                   2 | Charging
                #   16 | NotChargingBecauseFallbackGoEAwattar=16, 
                #   17 | NotChargingBecauseFallbackAwattar=17, 
                #   18 | NotChargingBecauseFallbackAutomaticStop=18, 
                #   19 | ChargingBecauseCarCompatibilityKeepAlive=19,         2 | Charging
                #   20 | ChargingBecauseChargePauseNotAllowed=20,             2 | Charging
                #   22 | NotChargingBecauseSimulateUnplugging=22, 
                #   23 | NotChargingBecausePhaseSwitch=23,                    22,23 | Report proper phaseswitch direction, 22=to-3-phase, 23 to-1-phase
                #   24 | NotChargingBecauseMinPauseDuration=24)
                #   
                d(self, "Wattpilot Modelstatus: {model}".format(model=self.wattpilot.modelStatus))

                #user may switch mode on wattpilot. verify current mode we are supposed to be in. 
                #determine current modes.
                if (self.wattpilot.mode == WattpilotControlMode.ECO):
                    self.autostart = 1
                    self.mode = VrmEvChargerControlMode.Auto
                else:
                    self.autostart = 0
                    self.mode = VrmEvChargerControlMode.Manual

                #keep the Start-Stop State in line anytime, no matter if auto or manual mode.
                self.reportStartStopValue(VrmEvChargerStartStop.Start if self.wattpilot.power != 0 else VrmEvChargerStartStop.Stop)

                if (self.wattpilot.modelStatus == WattpilotModelStatus.NotChargingBecauseNoChargeCtrlData or not self.wattpilot.carConnected):
                    #Disconnected wins over any state reported by wattpilot.
                    #EV Disconnected. Nothing to do here, but report data and a 0 watt request and none-automatic mode. 
                    self.reportVRMStatus(VrmEvChargerStatus.Disconnected) #disconnected

                    #when disconnected, reset the noChargeSinceFlag, so charging will start upon next connection.
                    self.noChargeSince = 0

                elif (self.wattpilot.modelStatus == WattpilotModelStatus.ChargingBecauseForceStateOn or self.wattpilot.modelStatus == WattpilotModelStatus.ChargingBecauseFallbackDefault):
                    #Wattpilot is charging, because forced on. So, we are either in manual control + on, or running in automatic mode. 
                    #in manual mode - nothing to do, but report consumption. In Auto Mode, we have to take control.
                    #Wattpilot eco means "auto control."

                    if (self.wattpilot.power <= 0):
                        self.noChargeSince += 5
                    else:
                        self.noChargeSince = 0

                    if (self.noChargeSince >= 120):
                        #we are officially charging, but no charge happened since 2 minutes. 
                        #so, we assume, car is fully charged. 
                        d(self, "No charge since 2 minutes... Assuming car is fully charged.")
                        self.reportVRMStatus(VrmEvChargerStatus.Charged)
                    else:
                        self.chargingTime += 5
                        self.publishRetained("/LastChargeModeLiteral", "SolarOverhead")
                        if (self.wattpilot.mode == WattpilotControlMode.ECO):
                            #Mode auto + charging reported. => We are in duty of contorl!
                            if self.allowance >= self.wattpilot.voltage1 * 6:
                                targetAmps = int(round(max(self.allowance / self.wattpilot.voltage1, 6))) 
                                targetAmps = min(self.wattpilot.ampLimit * 3, targetAmps) #obey limits.

                                self.publishServiceMessage(self, "Current allowance is {0}W, that's {1}A".format(self.allowance, targetAmps))

                                #Adjust charging rate. Method over there will handle phase-switching if required and return the proper state
                                self.reportVRMStatus(self.adjustChargeCurrent(targetAmps))
                                
                            else:
                                # Allowance dropped below the 1-phase minimum (voltage1 * 6W).
                                # Normally we stop charging. Exception: if we are currently on 3-phase,
                                # the 3→1 protection window is still active, and the battery SoC is
                                # above the protection threshold — keep charging at the 3-phase minimum
                                # (6A) instead of stopping. This avoids an unnecessary stop + on/off
                                # cooldown when solar briefly dips, as long as the battery can handle it.
                                currentSoc = self.socDbus.value if self.socDbus.value is not None else 0
                                threeToOneCooldownActive = (time.time() - self.lastThreeToOneCooldownStart) < self.minimumPhaseSwitchSecondsThreeToOne
                                socAboveThreshold = currentSoc >= self.socProtectionThreshold

                                if (self.currentPhaseMode == 2 and threeToOneCooldownActive and socAboveThreshold):
                                    remainingCooldown = round(self.lastThreeToOneCooldownStart + self.minimumPhaseSwitchSecondsThreeToOne - time.time())
                                    self.publishServiceMessage(self, "Allowance below minimum but holding 3-phase (SoC={0}%, cooldown={1}s remaining). Using 6A.".format(round(currentSoc), remainingCooldown))
                                    self.wattpilot.set_power(6)
                                    # 1-phase would suffice → record for post-cooldown history check
                                    self.phaseCheckHistory.append(False)
                                    self.reportVRMStatus(VrmEvChargerStatus.Charging)
                                else:
                                    i(self, "NO Allowance or end of low price phase, stopping charging.")
                                    self.reportVRMStatus(VrmEvChargerStatus.StopCharging)

                                    onOffCooldownSeconds = self.getOnOffCooldownSeconds()
                                    if (onOffCooldownSeconds <= 0):
                                        i(self, "STOP send!")
                                        self.wattpilot.set_start_stop(WattpilotStartStop.Off)
                                        self.lastOnOffTime = time.time()
                                        self.dbusService["/StartStop"] = VrmEvChargerStartStop.Stop.value   
                                        self.dbusService["/StartStopLiteral"] = VrmEvChargerStartStop.Stop.name

                                        # reset to auto-phase so manual control or low-price charging can start cleanly
                                        self.currentPhaseMode = 0
                                        self.wattpilot.set_phases(0)
                                    else:
                                        self.publishServiceMessage(self, "Stop-Charge delayed due to on/off cooldown: {0}s. Using 6A to reduce impact.".format(onOffCooldownSeconds))
                                        self.wattpilot.set_power(6)
                        else:
                            #charging, but not in auto mode - so, charging is all that's left to say. 
                            d(self, "Charging in manual mode.")
                            self.reportVRMStatus(VrmEvChargerStatus.Charging) #charging
                        
                    #in either mode, report consumption and current phasemode.
                    self.reportConsumption()

                elif (self.wattpilot.modelStatus.value in [4,5,6,16,17,18,22,24]):
                    #NotChargingBecauseWhatever - this is most likely our operational state in automatic mode. 
                    if (self.wattpilot.mode == WattpilotControlMode.ECO):
                        #auto
                        #check allowance
                        if (self.allowance >= self.wattpilot.voltage1 * 6):
                            onOffCooldownSeconds = self.getOnOffCooldownSeconds()

                            self.reportVRMStatus(VrmEvChargerStatus.StartCharging) #start charging

                            if (onOffCooldownSeconds <= 0):
                                self.publishServiceMessage(self, "Starting to charge.")

                                #check, if we need to start in 1 or 3 phase mode, based on targetAmps. 
                                targetAmps = int(round(max(self.allowance / self.wattpilot.voltage1, 6))) 
                                targetAmps = min(self.wattpilot.ampLimit * 3, targetAmps) #obey limits.

                                if (targetAmps > self.wattpilot.ampLimit):
                                    self.currentPhaseMode=2
                                    self.wattpilot.set_phases(2)
                                else:
                                    self.currentPhaseMode=1
                                    self.wattpilot.set_phases(1)
                                
                                self.wattpilot.set_power(targetAmps)
                                self.wattpilot.set_start_stop(WattpilotStartStop.On)
                                self.lastOnOffTime = time.time()
                                self.dbusService["/StartStop"] = VrmEvChargerStartStop.Start.value
                                self.dbusService["/StartStopLiteral"] = VrmEvChargerStartStop.Start.name
                                
                            else:
                                self.publishServiceMessage(self, "Start-Charge delayed due to on/off cooldown: {0}s".format(onOffCooldownSeconds))
                        else:
                            d(self, "Waiting for Sun in auto mode")
                            self.reportVRMStatus(VrmEvChargerStatus.WaitingForSun) #waiting for sun.

                            #ensure, we are in neutral state, so cheap price charging can kick in. 
                            if self.wattpilot.startState != WattpilotStartStop.Neutral:
                                d(self, "Returning charge control to neutral state.")
                                self.wattpilot.set_start_stop(WattpilotStartStop.Neutral)
                                
                    else:
                        #not charging, but not in auto mode - so, connected is all that's left to say. 
                        self.reportVRMStatus(VrmEvChargerStatus.Connected) #connected 

                elif (self.wattpilot.modelStatus in[WattpilotModelStatus.ChargingBecauseAwattarPriceLow]) :
                    if (self.wattpilot.power <= 0):
                        self.noChargeSince += 5
                    else:
                        self.noChargeSince = 0

                    if (self.noChargeSince >= 120):
                        #we are officially charging, but no charge happened since 2 minutes. 
                        #so, we assume, car is fully charged. 
                        d(self, "No charge since 2 minutes... Assuming car is fully charged.")
                        self.reportVRMStatus(VrmEvChargerStatus.Charged)
                    else:
                        self.chargingTime += 5
                        self.reportVRMStatus(VrmEvChargerStatus.Charging) 
                        self.reportConsumption()
                        self.publishRetained("/LastChargeModeLiteral", "LowPrice")
                    
                elif (self.wattpilot.modelStatus == WattpilotModelStatus.NotChargingBecausePhaseSwitch):
                    self.chargingTime += 5
                    #Phaseswitch, report properly. 
                    #when we set the phasemode and wattpilot starts to switch,
                    #our status is "ahead". So, if we are in phase mode 1 and wattpilot starts to report "phaseswitching", we are actually switching from 3 to 1.
                    #22 = Switching to 3-phase
                    #23 = Switching to 1-phase
                    if (self.currentPhaseMode == 1):
                        self.reportVRMStatus(VrmEvChargerStatus.SwitchingTo1Phase)
                    elif (self.currentPhaseMode == 2):
                        self.reportVRMStatus(VrmEvChargerStatus.SwitchingTo3Phase)

                else:
                    w(self, "Unknown Modelstatus reported: {0} - doing nothing.".format(self.wattpilot.modelStatus))

                #update current values that are independent of model status and dump infos.
                self.reportBaseRequest()
                self.dumpEvChargerInfo()
        except Exception as ex:
            c(self, "Exception during duty-cycle.", exc_info=ex)

    def reportVRMStatus(self, status:VrmEvChargerStatus):
        self.publish("/Status", status.value)
        self.publish("/StatusLiteral", status.name)

    def reportBaseRequest(self):
        #if voltage is unknown, we cannot request a proper minimum. leave it unset, or at currenet state, happens rarely. :( 
        if (self.wattpilot.voltage1 is not None):
            self.publishMainMqtt("es-ESS/SolarOverheadDistributor/Requests/Wattpilot/Minimum", int(round(self.wattpilot.voltage1 * 6)))
           

        self.publishMainMqtt("es-ESS/SolarOverheadDistributor/Requests/Wattpilot/IgnoreBatReservation", "false")
        self.publishMainMqtt("es-ESS/SolarOverheadDistributor/Requests/Wattpilot/VRMInstanceID", self.config["FroniusWattpilot"]["VRMInstanceID_OverheadRequest"])
        self.publishMainMqtt("es-ESS/SolarOverheadDistributor/Requests/Wattpilot/IsScriptedConsumer", "true")
        self.publishMainMqtt("es-ESS/SolarOverheadDistributor/Requests/Wattpilot/PriorityShift", 1)
        self.publishMainMqtt("es-ESS/SolarOverheadDistributor/Requests/Wattpilot/Priority", self.config["FroniusWattpilot"]["OverheadPriority"])
        
        #StepSize depends on Current Phasemode. 
        if (self.currentPhaseMode == 2):
            self.publishMainMqtt("es-ESS/SolarOverheadDistributor/Requests/Wattpilot/StepSize", int(round(self.wattpilot.voltage1 + self.wattpilot.voltage2 + self.wattpilot.voltage3)))
        else:
            self.publishMainMqtt("es-ESS/SolarOverheadDistributor/Requests/Wattpilot/StepSize", int(round(self.wattpilot.voltage1)))

        #request overall depends on wheter car is connected and operation mode.
        if (self.mode == VrmEvChargerControlMode.Auto and self.wattpilot.carConnected and self.noChargeSince <= 120):
            self.publishMainMqtt("es-ESS/SolarOverheadDistributor/Requests/Wattpilot/Request", 
                round(self.wattpilot.ampLimit * (self.wattpilot.voltage1 + self.wattpilot.voltage2 + self.wattpilot.voltage3))) 
        else:
            self.publishMainMqtt("es-ESS/SolarOverheadDistributor/Requests/Wattpilot/Request", 0) 
            
        #auto or manual?
        if (self.mode == VrmEvChargerControlMode.Auto):
            self.publishMainMqtt("es-ESS/SolarOverheadDistributor/Requests/Wattpilot/IsAutomatic", "true")
        else:
            self.publishMainMqtt("es-ESS/SolarOverheadDistributor/Requests/Wattpilot/IsAutomatic", "false")

        #report phasemode, always.
        self.reportPhaseMode()

    def reportConsumption(self):
        self.publishMainMqtt("es-ESS/SolarOverheadDistributor/Requests/Wattpilot/Consumption", self.wattpilot.power * 1000)

    def reportStartStopValue(self, v:VrmEvChargerStartStop):
        self.publish("/StartStop", v.value)
        self.publish("/StartStopLiteral", v.name)

    def reportPhaseMode(self):
        #pvoverhead request
        if (self.wattpilot.power > 0 and self.currentPhaseMode == 1):
            self.publishMainMqtt("es-ESS/SolarOverheadDistributor/Requests/Wattpilot/CustomName", "Fronius Wattpilot (1)")
        elif (self.wattpilot.power > 0 and self.currentPhaseMode == 2):
            self.publishMainMqtt("es-ESS/SolarOverheadDistributor/Requests/Wattpilot/CustomName", "Fronius Wattpilot (3)")
        else:
            self.publishMainMqtt("es-ESS/SolarOverheadDistributor/Requests/Wattpilot/CustomName", "Fronius Wattpilot")

    def onMqttMessage(self, client, userdata, msg):
      try:
         message = str(msg.payload)[2:-1]

         if (msg.topic == self.mqttAllowanceTopic):
             self.allowance = float(message)

      except Exception as e:
         c(self, "Exception", exc_info=e)

    def getOnOffCooldownSeconds(self):
        return max(0, self.lastOnOffTime + self.minimumOnOffSeconds- time.time())
    
    def getPhaseSwitchCooldownSeconds(self):
        return max(0, self.lastPhaseSwitchTime + self.minimumPhaseSwitchSeconds- time.time())

    def adjustChargeCurrent(self, targetAmps):
        # Determine which phase mode the current allowance calls for.
        # Anything above the single-phase hardware max → needs 3 phases.
        desiredPhaseMode = 2 if targetAmps > self.wattpilot.ampLimit else 1
        enteringPhaseMode = self.currentPhaseMode

        currentSoc = self.socDbus.value if self.socDbus.value is not None else 0
        d(self, "PhaseMode current/desired: {0}/{1}  SoC: {2}%".format(enteringPhaseMode, desiredPhaseMode, round(currentSoc)))

        if (self.currentPhaseMode == desiredPhaseMode):
            # ── No phase change needed ──────────────────────────────────────────
            # Divide total amps by the number of active phases before sending.
            divider = 1 if self.currentPhaseMode == 1 else 3
            targetAmps = int(round(targetAmps / divider))
            i(self, "Adjusting charge current to: {0}A (phase-mode {1})".format(targetAmps, self.currentPhaseMode))
            self.wattpilot.set_power(targetAmps)

            # On 1-phase: any tick where 1-phase is sufficient resets the
            # 3-phase desire timer — demand must be *continuously* above the
            # threshold for the full stabilisation window.
            if self.currentPhaseMode == 1:
                if self.wantThreePhaseSince != 0:
                    d(self, "3-phase demand dropped — resetting wantThreePhaseSince.")
                self.wantThreePhaseSince = 0

            # On 3-phase: record that 3-phase is still desired for the
            # post-cooldown history check.
            if self.currentPhaseMode == 2:
                self.phaseCheckHistory.append(True)

        else:
            # ── Phase change required ───────────────────────────────────────────
            i(self, "Phase change needed: {0}→{1} (total {2}A requested)".format(self.currentPhaseMode, desiredPhaseMode, targetAmps))

            if desiredPhaseMode == 2:
                # ── 1-Phase → 3-Phase ───────────────────────────────────────────
                # Only switch after the demand has been *continuously* above the
                # 3-phase threshold for minimumPhaseSwitchSeconds.  Any tick where
                # demand drops back to 1-phase resets the timer (see Fall A above).
                if self.wantThreePhaseSince == 0:
                    self.wantThreePhaseSince = time.time()
                    i(self, "3-phase demand started. Waiting {0}s before switching.".format(self.minimumPhaseSwitchSeconds))

                elapsed = time.time() - self.wantThreePhaseSince
                remaining = self.minimumPhaseSwitchSeconds - elapsed

                if elapsed >= self.minimumPhaseSwitchSeconds:
                    # Demand has been stable long enough — switch now.
                    ampsPerPhase = int(round(targetAmps / 3))
                    self.publishServiceMessage(self, "Switching 1→3-phase after {0}s of stable demand. {1}A/phase.".format(round(elapsed), ampsPerPhase))
                    self.wattpilot.set_phases(2)
                    self.currentPhaseMode = 2
                    self.lastThreeToOneCooldownStart = time.time()
                    self.phaseCheckHistory.clear()
                    self.wantThreePhaseSince = 0
                    self.wattpilot.set_power(ampsPerPhase)
                else:
                    # Still in the stabilisation window — hold at 1-phase maximum.
                    self.publishServiceMessage(self, "1→3-phase pending: {0}s remaining. Holding 1-phase at {1}A.".format(round(remaining), self.wattpilot.ampLimit))
                    self.wattpilot.set_power(self.wattpilot.ampLimit)

            else:
                # ── 3-Phase → 1-Phase ───────────────────────────────────────────
                # 3-phase is protected for minimumPhaseSwitchSecondsThreeToOne (default 1h).
                # After that window, we only switch if 1-phase was desired in ≥80% of
                # the last 10 minutes — otherwise we extend the protection by another hour.
                # Exception: if SoC drops below socProtectionThreshold, switch immediately.

                # Record every tick for the post-cooldown decision.
                self.phaseCheckHistory.append(False)  # 1-phase would suffice this tick

                if currentSoc < self.socProtectionThreshold:
                    # Battery protection overrides all cooldowns.
                    ampsPerPhase = int(round(targetAmps))  # targetAmps already for 1-phase
                    self.publishServiceMessage(self, "SoC {0}% below threshold {1}% — switching 3→1-phase immediately.".format(round(currentSoc), self.socProtectionThreshold))
                    self.wattpilot.set_phases(1)
                    self.currentPhaseMode = 1
                    self.wantThreePhaseSince = 0
                    self.phaseCheckHistory.clear()
                    self.wattpilot.set_power(ampsPerPhase)

                else:
                    # SoC is healthy — apply the 1h protection window.
                    cooldownElapsed = time.time() - self.lastThreeToOneCooldownStart
                    remainingCooldown = round(self.minimumPhaseSwitchSecondsThreeToOne - cooldownElapsed)

                    if cooldownElapsed < self.minimumPhaseSwitchSecondsThreeToOne:
                        # Still inside the protection window.
                        self.publishServiceMessage(self, "3→1-phase blocked: {0}s remaining in protection window. Using 6A minimum.".format(remainingCooldown))
                        self.wattpilot.set_power(6)
                    else:
                        # Protection window expired — check whether 3-phase was still
                        # needed for ≥80% of the last 10 minutes.
                        if len(self.phaseCheckHistory) > 0:
                            threePhaseRatio = sum(self.phaseCheckHistory) / len(self.phaseCheckHistory)
                        else:
                            threePhaseRatio = 0.0

                        if threePhaseRatio >= 0.8:
                            # 3-phase was still mostly needed — extend the protection window.
                            self.publishServiceMessage(self, "3-phase desired {0}% of last 10min — extending 3-phase protection by another {1}s.".format(round(threePhaseRatio * 100), self.minimumPhaseSwitchSecondsThreeToOne))
                            self.lastThreeToOneCooldownStart = time.time()
                            self.phaseCheckHistory.clear()
                            self.wattpilot.set_power(6)
                        else:
                            # 1-phase was dominant — switch now.
                            ampsPerPhase = int(round(targetAmps))
                            self.publishServiceMessage(self, "3-phase desired only {0}% of last 10min — switching 3→1-phase. {1}A.".format(round(threePhaseRatio * 100), ampsPerPhase))
                            self.wattpilot.set_phases(1)
                            self.currentPhaseMode = 1
                            self.wantThreePhaseSince = 0
                            self.phaseCheckHistory.clear()
                            self.wattpilot.set_power(ampsPerPhase)

        # Return the VRM status that reflects the phase direction.
        # If the phase mode did not change this tick, report plain Charging.
        if desiredPhaseMode == enteringPhaseMode:
            return VrmEvChargerStatus.Charging
        elif desiredPhaseMode == 2:
            return VrmEvChargerStatus.SwitchingTo3Phase
        else:
            return VrmEvChargerStatus.SwitchingTo1Phase

    def dumpEvChargerInfo(self):
        #method is called, whenever new information arrive through mqtt. 
        #just dump the information we have.
        self.publish("/Ac/L1/Power", self.wattpilot.power1 * 1000 if (self.wattpilot.power1 is not None) else 0)
        self.publish("/Ac/L2/Power", self.wattpilot.power2 * 1000 if (self.wattpilot.power2 is not None) else 0)
        self.publish("/Ac/L3/Power", self.wattpilot.power3 * 1000 if (self.wattpilot.power3 is not None) else 0)
        self.publish("/Ac/L1/Voltage", self.wattpilot.voltage1  if (self.wattpilot.voltage1 is not None) else 0)
        self.publish("/Ac/L2/Voltage", self.wattpilot.voltage2  if (self.wattpilot.voltage2 is not None) else 0)
        self.publish("/Ac/L3/Voltage", self.wattpilot.voltage3  if (self.wattpilot.voltage3 is not None) else 0)
        self.publish("/Ac/L1/Current", self.wattpilot.amps1  if (self.wattpilot.amps1 is not None and self.wattpilot.power>0) else 0)
        self.publish("/Ac/L2/Current", self.wattpilot.amps2  if (self.wattpilot.amps2 is not None and self.wattpilot.power>0) else 0)
        self.publish("/Ac/L3/Current", self.wattpilot.amps3  if (self.wattpilot.amps3 is not None and self.wattpilot.power>0) else 0)
        self.publish("/Ac/L1/PowerFactor", self.wattpilot.powerFactor1  if (self.wattpilot.powerFactor1 is not None and self.wattpilot.power>0) else 0)
        self.publish("/Ac/L2/PowerFactor", self.wattpilot.powerFactor2  if (self.wattpilot.powerFactor2 is not None and self.wattpilot.power>0) else 0)
        self.publish("/Ac/L3/PowerFactor", self.wattpilot.powerFactor3  if (self.wattpilot.powerFactor3 is not None and self.wattpilot.power>0) else 0)
        self.publish("/Ac/Power", self.wattpilot.power * 1000 if (self.wattpilot.power is not None) else 0)
        self.publish("/Ac/PowerPercent", (self.wattpilot.power * 1000) / (3 * self.wattpilot.ampLimit * self.wattpilot.voltage1) * 100.0 if (self.wattpilot.power is not None) else 0)
        self.publish("/Ac/PowerMax", (3 * self.wattpilot.ampLimit * self.wattpilot.voltage1))
        self.publish("/Current", (self.wattpilot.amps1 + self.wattpilot.amps2 + self.wattpilot.amps3) if (self.wattpilot.amp is not None and self.wattpilot.power>0) else 0)
        self.publish("/Mode", self.mode.value)
        self.publish("/ModeLiteral", self.mode.name)

        #Also write total power back to SolarOverheadDistributor 
        self.publishMainMqtt("es-ESS/SolarOverheadDistributor/Requests/Wattpilot/Consumption", self.wattpilot.power * 1000)

        if (self.wattpilot.energyCounterSinceStart is not None and self.wattpilot.carConnected):
            self.publish("/Ac/Energy/Forward", self.wattpilot.energyCounterSinceStart / 1000)
        
        elif (self.wattpilot.energyCounterSinceStart is not None and not self.wattpilot.carConnected and self.config["FroniusWattpilot"]["ResetChargedEnergyCounter"].lower() == "onconnect"):
            self.publish("/Ac/Energy/Forward", self.wattpilot.energyCounterSinceStart / 1000)

        else:
            self.publish("/Ac/Energy/Forward", 0.0)
            self.chargingTime = 0
        
        self.publish("/AutoStart", self.autostart)

        self.publish("/ChargingTime", self.chargingTime)
        self.publish("/CarState", self.wattpilot.carConnected)

        self.publish("/PhaseMode", self.currentPhaseMode)
        if (self.currentPhaseMode == 2):
            self.publish("/SetCurrent", self.wattpilot.amp * 3)
            self.publish("/MaxCurrent", self.wattpilot.ampLimit * 3)
        else:
            self.publish("/SetCurrent", self.wattpilot.amp)
            self.publish("/MaxCurrent", self.wattpilot.ampLimit )

    def publish(self, path, value):
        self.dbusService[path] = value
        self.publishMainMqtt("es-ESS/FroniusWattpilot{0}".format(path), value, 0)

    def publishRetained(self, path, value):
        self.dbusService[path] = value
        self.publishMainMqtt("es-ESS/FroniusWattpilot{0}".format(path), value, 0, True)

    def handleSigterm(self):
       self.publishServiceMessage(self, "SIGTERM received, sending STOP-command to wattpilot, if in auto mode.")
       
       if (self.wattpilot is not None and self.wattpilot.connected and self.mode == VrmEvChargerControlMode.Auto):
            self.wattpilot.set_start_stop(WattpilotStartStop.Off)
       
       self.wattpilot._auto_reconnect = False
       self.wattpilot.disconnect()