from cereal import car
from openpilot.common.conversions import Conversions as CV
from openpilot.common.numpy_fast import clip, interp
from openpilot.common.realtime import DT_CTRL
from opendbc.can.packer import CANPacker
from openpilot.selfdrive.car import apply_driver_steer_torque_limits, common_fault_avoidance
from openpilot.selfdrive.car.hyundai import hyundaicanfd, hyundaican
from openpilot.selfdrive.car.hyundai.hyundaicanfd import CanBus
from openpilot.selfdrive.car.hyundai.values import HyundaiFlags, Buttons, CarControllerParams, CANFD_CAR, CAR, LEGACY_SAFETY_MODE_CAR
import random
from random import randint
from openpilot.common.params import Params

VisualAlert = car.CarControl.HUDControl.VisualAlert
LongCtrlState = car.CarControl.Actuators.LongControlState

# EPS faults if you apply torque while the steering angle is above 90 degrees for more than 1 second
# All slightly below EPS thresholds to avoid fault
MAX_ANGLE = 85
MAX_ANGLE_FRAMES = 89
MAX_ANGLE_CONSECUTIVE_FRAMES = 2


def process_hud_alert(enabled, fingerprint, hud_control):
  sys_warning = (hud_control.visualAlert in (VisualAlert.steerRequired, VisualAlert.ldw))

  # initialize to no line visible
  # TODO: this is not accurate for all cars
  sys_state = 1
  if hud_control.leftLaneVisible and hud_control.rightLaneVisible or sys_warning:  # HUD alert only display when LKAS status is active
    sys_state = 3 if enabled or sys_warning else 4
  elif hud_control.leftLaneVisible:
    sys_state = 5
  elif hud_control.rightLaneVisible:
    sys_state = 6

  # initialize to no warnings
  left_lane_warning = 0
  right_lane_warning = 0
  if hud_control.leftLaneDepart:
    left_lane_warning = 1 if fingerprint in (CAR.GENESIS_G90, CAR.GENESIS_G80) else 2
  if hud_control.rightLaneDepart:
    right_lane_warning = 1 if fingerprint in (CAR.GENESIS_G90, CAR.GENESIS_G80) else 2

  return sys_warning, sys_state, left_lane_warning, right_lane_warning


class CarController:
  def __init__(self, dbc_name, CP, VM):
    self.CP = CP
    self.CAN = CanBus(CP)
    self.cc_params = CarControllerParams(CP)
    self.packer = CANPacker(dbc_name)
    self.angle_limit_counter = 0
    self.frame = 0

    self.accel_last = 0
    self.apply_steer_last = 0
    self.car_fingerprint = CP.carFingerprint
    self.last_button_frame = 0
    
    self.jerkStartLimit = 2.0
    self.maxAngleFrames = MAX_ANGLE_FRAMES
    self.softHoldMode = 1
    self.blinking_signal = False #������ ����̿� 1Hz
    self.blinking_frame = int(1.0 / DT_CTRL)
    self.jerk_count = 0
    self.activateCruise = 0
    self.button_wait = 12
    self.resume_cnt = 0
    self.resume_wait_timer = 0
    self.button_alive = 0
    self.button_alive_frame = 0
    self.activeAVM = 0
    self.params = Params()
    self.cruise_buttons_msg_values = None
    self.cruise_buttons_msg_cnt = 0

  def update(self, CC, CS, now_nanos):
    actuators = CC.actuators
    hud_control = CC.hudControl

    self.softHoldMode = self.params.get_int("SoftHoldMode")
    self.enableAVM = self.params.get_int("EnableAVM")
    # steering torque
    new_steer = int(round(actuators.steer * self.cc_params.STEER_MAX))
    apply_steer = apply_driver_steer_torque_limits(new_steer, self.apply_steer_last, CS.out.steeringTorque, self.cc_params)

    # >90 degree steering fault prevention
    self.angle_limit_counter, apply_steer_req = common_fault_avoidance(abs(CS.out.steeringAngleDeg) >= MAX_ANGLE, CC.latActive,
                                                                       self.angle_limit_counter, self.maxAngleFrames,
                                                                       MAX_ANGLE_CONSECUTIVE_FRAMES)

    if not CC.latActive:
      apply_steer = 0

    # Hold torque with induced temporary fault when cutting the actuation bit
    torque_fault = CC.latActive and not apply_steer_req

    self.apply_steer_last = apply_steer

    # accel + longitudinal
    accel = clip(actuators.accel, CarControllerParams.ACCEL_MIN, CarControllerParams.ACCEL_MAX)
    stopping = actuators.longControlState == LongCtrlState.stopping
    set_speed_in_units = hud_control.setSpeed * (CV.MS_TO_KPH if CS.is_metric else CV.MS_TO_MPH)

    # HUD messages
    sys_warning, sys_state, left_lane_warning, right_lane_warning = process_hud_alert(CC.enabled, self.car_fingerprint,
                                                                                      hud_control)

    if self.frame % self.blinking_frame == 0:
      self.blinking_signal = True
    elif self.frame % self.blinking_frame == self.blinking_frame / 2:
      self.blinking_signal = False
    can_sends = []

    # *** common hyundai stuff ***
    if self.frame % 100 == 0:
      self.maxAngleFrames = int(self.params.get("MaxAngleFrames", encoding="utf8"))

    # tester present - w/ no response (keeps relevant ECU disabled)
    if self.frame % 100 == 0 and not (self.CP.flags & HyundaiFlags.CANFD_CAMERA_SCC.value) and self.CP.openpilotLongitudinalControl:
      # for longitudinal control, either radar or ADAS driving ECU
      addr, bus = 0x7d0, 0
      if self.CP.flags & HyundaiFlags.CANFD_HDA2.value:
        addr, bus = 0x730, self.CAN.ECAN
      can_sends.append([addr, 0, b"\x02\x3E\x80\x00\x00\x00\x00\x00", bus])

      # for blinkers
      if self.CP.flags & HyundaiFlags.ENABLE_BLINKERS:
        can_sends.append([0x7b1, 0, b"\x02\x3E\x80\x00\x00\x00\x00\x00", self.CAN.ECAN])

    ## TODO: avm이 자꾸만 깜박임...  스캐너를 다시 물려봐야할듯..
    if self.enableAVM > 0:
      addr, bus = 0x7b1, 0
      avm_on = b'\x05\x2f\xf0\x26\x03\xff\x00\x00'
      avm_off = b'\x05\x2f\xf0\x26\x00\xff\x00\x00'
      if self.frame % 100 == 0:
        can_sends.append([addr, 0, b"\x02\x3E\x00\x00\x00\x00\x00\x00", bus])
      if self.frame % 20 == 1:
        if self.activeAVM != hud_control.activeAVM:
          self.activeAVM = hud_control.activeAVM
          if self.activeAVM > 0:
            can_sends.append([addr, 0, avm_on, bus])
          else:
            can_sends.append([addr, 0, avm_off, bus])
      ## 시험: 한번 껐다가 켜보자... 될랑가?
      if self.activeAVM:
        if self.frame % 100 == 50:
          can_sends.append([addr, 0, avm_off, bus])
        elif self.frame % 100 == 53:
          can_sends.append([addr, 0, avm_on, bus])

    # CAN-FD platforms
    if self.CP.carFingerprint in CANFD_CAR:
      hda2 = self.CP.flags & HyundaiFlags.CANFD_HDA2
      hda2_long = hda2 and self.CP.openpilotLongitudinalControl

      # steering control
      can_sends.extend(hyundaicanfd.create_steering_messages(self.packer, self.CP, self.CAN, CC.enabled, apply_steer_req, apply_steer))

      # prevent LFA from activating on HDA2 by sending "no lane lines detected" to ADAS ECU
      if self.frame % 5 == 0 and hda2:
        can_sends.append(hyundaicanfd.create_suppress_lfa(self.packer, self.CAN, CS.hda2_lfa_block_msg,
                                                          self.CP.flags & HyundaiFlags.CANFD_HDA2_ALT_STEERING))

      # LFA and HDA icons
      if self.frame % 5 == 0 and (not hda2 or hda2_long):
        can_sends.append(hyundaicanfd.create_lfahda_cluster(self.packer, self.CAN, CC.enabled))

      # blinkers
      if hda2 and self.CP.flags & HyundaiFlags.ENABLE_BLINKERS:
        can_sends.extend(hyundaicanfd.create_spas_messages(self.packer, self.CAN, self.frame, CC.leftBlinker, CC.rightBlinker))

      if self.CP.openpilotLongitudinalControl:
        if hda2:
          can_sends.extend(hyundaicanfd.create_adrv_messages(self.packer, self.CAN, self.frame))
        if self.frame % 2 == 0:
          can_sends.append(hyundaicanfd.create_acc_control(self.packer, self.CAN, CC.enabled, self.accel_last, accel, stopping, CC.cruiseControl.override,
                                                           set_speed_in_units, CS.longitudinal_personality))
          self.accel_last = accel

        ### for LongControl auto activate...
        if not CC.enabled:
          self.activateCruise = 0
        if CC.cruiseControl.activate and self.activateCruise == 0: ## ajouatom: send command to panda via Button spam(RES_ACCEL), for auto engage
          self.activateCruise = 1
          #can_sends.append(hyundaican.create_clu11(self.packer, self.frame, CS.clu11, Buttons.RES_ACCEL, self.CP.carFingerprint))
          can_sends.append(hyundaicanfd.create_buttons(self.packer, self.CP, self.CAN, CS.buttons_counter+1, Buttons.RES_ACCEL))
          print("SendActivateCanData#######")

      else:
        # button presses
        can_sends.extend(self.create_button_messages(CC, CS, use_clu11=False))
    else:
      can_sends.append(hyundaican.create_lkas11(self.packer, self.frame, self.car_fingerprint, apply_steer, apply_steer_req,
                                                torque_fault, CS.lkas11, sys_warning, sys_state, CC.enabled,
                                                hud_control.leftLaneVisible, hud_control.rightLaneVisible,
                                                left_lane_warning, right_lane_warning))

      if not self.CP.openpilotLongitudinalControl:
        can_sends.extend(self.create_button_messages(CC, CS, use_clu11=True))
      else:
        if not CC.enabled:
          self.activateCruise = 0

        ### for LongControl auto activate...
        if CC.cruiseControl.activate and self.activateCruise == 0: ## ajouatom: send command to panda via Button spam(RES_ACCEL), for auto engage
          self.activateCruise = 1
          can_sends.append(hyundaican.create_clu11(self.packer, self.frame, CS.clu11, Buttons.RES_ACCEL, self.CP.carFingerprint))
          print("SendActivateCanData#######")

      if self.CP.carFingerprint in (CAR.GENESIS_G90_2019, CAR.GENESIS_G90, CAR.K7):
        can_sends.append(hyundaican.create_mdps12(self.packer, self.frame, CS.mdps12))

      if self.frame % 2 == 0 and self.CP.openpilotLongitudinalControl:
        # ajouatom: calculate jerk, cb : reverse engineer from KONA EV
        jerk = actuators.jerk
        startingJerk = self.jerkStartLimit
        jerkLimit = 5.0
        self.jerk_count += DT_CTRL
        jerk_max = interp(self.jerk_count, [0, 1.5, 2.5], [startingJerk, startingJerk, jerkLimit])
        cb_upper = cb_lower = 0
        if actuators.longControlState == LongCtrlState.off:
          jerk_u = jerkLimit
          jerk_l = jerkLimit          
          self.jerk_count = 0
        elif actuators.longControlState == LongCtrlState.stopping or hud_control.softHold > 0:
          jerk_u = 0.5
          jerk_l = 1.0 #jerkLimit
          self.jerk_count = 0
        else:
          jerk_u = min(max(0.5, jerk * 2.0), jerk_max)
          #jerk_l = min(max(1.0, -jerk * 2.0), jerk_max)
          jerk_l = min(max(1.2, -jerk * 2.0), jerkLimit) ## 1.0으로 하니 덜감속, 1.5로하니 너무감속, 1.2로 한번해보자(231228)
          cb_upper = clip(0.9 + accel * 0.2, 0, 1.2)
          cb_lower = clip(0.8 + accel * 0.2, 0, 1.2)
        
        # TODO: unclear if this is needed
        #jerk = 3.0 if actuators.longControlState == LongCtrlState.pid else 1.0
        #use_fca = self.CP.flags & HyundaiFlags.USE_FCA.value
        #can_sends.extend(hyundaican.create_acc_commands(self.packer, CC.enabled, accel, jerk, int(self.frame / 2),
        #                                                hud_control.leadVisible, set_speed_in_units, stopping,
        #                                                CC.cruiseControl.override, use_fca, CS.out.cruiseState.available))

        can_sends.extend(hyundaican.create_acc_commands_mix_scc(self.CP, self.packer, CC.enabled, accel, jerk_u, jerk_l, int(self.frame / 2),
                                                      hud_control, set_speed_in_units, stopping, CC, CS, self.softHoldMode, cb_upper, cb_lower, CS.out.cruiseState.available))
        self.accel_last = accel

      # 20 Hz LFA MFA message
      if self.frame % 5 == 0 and self.CP.flags & HyundaiFlags.SEND_LFA.value:
        can_sends.append(hyundaican.create_lfahda_mfc(self.packer, CC, self.blinking_signal))

      sccBus = 2 if self.CP.flags & HyundaiFlags.SCC_BUS2.value else 0
      # 5 Hz ACC options
      if self.frame % 20 == 0 and self.CP.openpilotLongitudinalControl: 
        if sccBus == 0:
          can_sends.extend(hyundaican.create_acc_opt(self.packer))
        elif CS.scc13 is not None:
          can_sends.append(hyundaican.create_acc_opt_copy(CS, self.packer))

      # 2 Hz front radar options
      if self.frame % 50 == 0 and self.CP.openpilotLongitudinalControl  and sccBus == 0:
        can_sends.append(hyundaican.create_frt_radar_opt(self.packer))

    new_actuators = actuators.copy()
    new_actuators.steer = apply_steer / self.cc_params.STEER_MAX
    new_actuators.steerOutputCan = apply_steer
    new_actuators.accel = accel

    self.frame += 1
    return new_actuators, can_sends

  def create_button_messages(self, CC: car.CarControl, CS: car.CarState, use_clu11: bool):
    can_sends = []
    if CS.out.brakePressed:
      return can_sends
    hud_control = CC.hudControl
    if use_clu11:
      #if CC.cruiseControl.cancel:
      #  can_sends.append(hyundaican.create_clu11(self.packer, self.frame, CS.clu11, Buttons.CANCEL, self.CP.carFingerprint))
      #elif CC.cruiseControl.resume:
      #  # send resume at a max freq of 10Hz
      #  if (self.frame - self.last_button_frame) * DT_CTRL > 0.1:
      #    # send 25 messages at a time to increases the likelihood of resume being accepted
      #    can_sends.extend([hyundaican.create_clu11(self.packer, self.frame, CS.clu11, Buttons.RES_ACCEL, self.CP.carFingerprint)] * 25)
      #    if (self.frame - self.last_button_frame) * DT_CTRL >= 0.15:
      #      self.last_button_frame = self.frame
      if CC.cruiseControl.cancel:
        print("cruiseControl.cancel")
        can_sends.append(hyundaican.create_clu11(self.packer, self.frame, CS.clu11, Buttons.CANCEL, self.CP.carFingerprint))
      elif CC.cruiseControl.resume:
        if self.CP.carFingerprint in LEGACY_SAFETY_MODE_CAR:            
          if self.resume_wait_timer > 0:
            self.resume_wait_timer -= 1
          else:
            can_sends.append(hyundaican.create_clu11_button(self.packer, self.frame, CS.clu11, Buttons.RES_ACCEL, self.CP.carFingerprint))
            self.resume_cnt += 1
            if self.resume_cnt >= int(randint(4, 5) * 2):
              self.resume_cnt = 0
              self.resume_wait_timer = int(randint(20, 25) * 2)
            
        else:
          # send resume at a max freq of 10Hz
          if (self.frame - self.last_button_frame) * DT_CTRL > 0.1:
            # send 25 messages at a time to increases the likelihood of resume being accepted
            #can_sends.extend([hyundaican.create_clu11(self.packer, self.frame, CS.clu11, Buttons.RES_ACCEL, self.CP.carFingerprint)] * 25)
            can_sends.append(hyundaican.create_clu11_button(self.packer, self.frame, CS.clu11, Buttons.RES_ACCEL, self.CP.carFingerprint))
            self.last_button_frame = self.frame
      else:
        set_speed_in_units = CC.hudControl.setSpeed * (CV.MS_TO_KPH if CS.is_metric else CV.MS_TO_MPH)
        self.resume_wait_timer = 0
        self.resume_cnt = 0
        target = int(set_speed_in_units+0.5)
        current = int(CS.out.cruiseState.speed*CV.MS_TO_KPH + 0.5)

        if (self.frame - self.last_button_frame) > self.button_wait:
          if (self.frame - self.button_alive_frame) > self.button_alive:
            self.button_wait = randint(8,15)
            self.last_button_frame = self.frame
          if CS.cruise_buttons[-1] == Buttons.NONE:
            if not CC.enabled:
              self.activateCruise = 0
            if CC.enabled:
              if not CS.out.cruiseState.enabled:
                #if CC.longActive and (hud_control.leadVisible or current > 10.0):
                if (hud_control.leadVisible or current > 10.0):
                  can_sends.append(hyundaican.create_clu11_button(self.packer, self.frame, CS.clu11, Buttons.RES_ACCEL, self.CP.carFingerprint))
                  #CC.debugTextCC = "BTN:++,T:{:.1f},C:{:.1f}".format(target, current)
              #elif CS.out.cruiseGap != hud_control.cruiseGap:
              #  can_sends.append(hyundaican.create_clu11_button(self.packer, self.frame, CS.clu11, Buttons.GAP_DIST, self.CP.carFingerprint))
              #  CC.debugTextCC = "currentGap = {}, target = {}".format(CS.out.cruiseGap, hud_control.cruiseGap)
              elif target < current and current>= 31:
                can_sends.append(hyundaican.create_clu11_button(self.packer, self.frame, CS.clu11, Buttons.SET_DECEL, self.CP.carFingerprint))
                #CC.debugTextCC = "BTN:--,T:{:.1f},C:{:.1f}".format(target, current)
              elif target > current and current < 160:
                can_sends.append(hyundaican.create_clu11_button(self.packer, self.frame, CS.clu11, Buttons.RES_ACCEL, self.CP.carFingerprint))
                #CC.debugTextCC = "BTN:++,T:{:.1f},C:{:.1f}".format(target, current)
            elif CC.cruiseControl.activate and self.activateCruise == 0:
              #print("sendActivateCruise Buttons....")
              if (hud_control.leadVisible or current > 10.0):
                print("sendActivateCruise Buttons....Sent....")
                can_sends.append(hyundaican.create_clu11_button(self.packer, self.frame, CS.clu11, Buttons.RES_ACCEL, self.CP.carFingerprint))
                self.activateCruise = 1
                self.button_wait = 100

        else:
          self.button_alive = randint(4, 8) #randint(12, 18)
          self.button_alive_frame = self.frame
      
    else:

      # carrot.. 왜 alt_cruise_button는 값이 리스트일까?, 그리고 왜? 빈데이터가 들어오는것일까?
      if CS.cruise_buttons_msg is not None and self.CP.flags & HyundaiFlags.CANFD_ALT_BUTTONS:
        try:
          cruise_buttons_msg_values = {key: value[0] for key, value in CS.cruise_buttons_msg.items()}
        except IndexError:
          #print("IndexError....")
          cruise_buttons_msg_values = None
          self.cruise_buttons_msg_cnt += 1
        if cruise_buttons_msg_values is not None:
          self.cruise_buttons_msg_values = cruise_buttons_msg_values
          self.cruise_buttons_msg_cnt = 0

      if (self.frame - self.last_button_frame) * DT_CTRL > 0.25:
        # cruise cancel
        if CC.cruiseControl.cancel:
          if (self.frame - self.last_button_frame) * DT_CTRL > 0.1:
            print("cruiseControl.cancel222222")
            if self.CP.flags & HyundaiFlags.CANFD_ALT_BUTTONS:
              #can_sends.append(hyundaicanfd.create_acc_cancel(self.packer, self.CP, self.CAN, CS.cruise_info))
              if self.cruise_buttons_msg_values is not None:
                can_sends.append(hyundaicanfd.alt_cruise_buttons(self.packer, self.CP, self.CAN, Buttons.CANCEL, self.cruise_buttons_msg_values, self.cruise_buttons_msg_cnt))
            
            else:
              for _ in range(20):
                can_sends.append(hyundaicanfd.create_buttons(self.packer, self.CP, self.CAN, CS.buttons_counter+1, Buttons.CANCEL))
            self.last_button_frame = self.frame

        # cruise standstill resume
        elif CC.cruiseControl.resume:
          if (self.frame - self.last_button_frame) * DT_CTRL > 0.1:
            if self.CP.flags & HyundaiFlags.CANFD_ALT_BUTTONS:
              # TODO: resume for alt button cars
              if self.cruise_buttons_msg_values is not None:
                print("cruiseControl.RES222222")
                for _ in range(4):
                  can_sends.append(hyundaicanfd.alt_cruise_buttons(self.packer, self.CP, self.CAN, Buttons.RES_ACCEL, self.cruise_buttons_msg_values, self.cruise_buttons_msg_cnt))
            else:
              for _ in range(20):
                can_sends.append(hyundaicanfd.create_buttons(self.packer, self.CP, self.CAN, CS.buttons_counter+1, Buttons.RES_ACCEL))
            self.last_button_frame = self.frame
        else:
          dat = self.canfd_speed_control_pcm(CC, CS, self.cruise_buttons_msg_values)
          if dat is not None:
            can_sends.append(dat)

    return can_sends

  def canfd_speed_control_pcm(self, CC, CS, cruise_buttons_msg_values):

    hud_control = CC.hudControl
    alt_buttons = True if self.CP.flags & HyundaiFlags.CANFD_ALT_BUTTONS else False

    if alt_buttons and cruise_buttons_msg_values is None:
      return None

    set_speed_in_units = hud_control.setSpeed * (CV.MS_TO_KPH if CS.is_metric else CV.MS_TO_MPH)
    self.resume_wait_timer = 0
    self.resume_cnt = 0
    target = int(set_speed_in_units+0.5)
    current = int(CS.out.cruiseState.speed*CV.MS_TO_KPH + 0.5)

    if (self.frame - self.last_button_frame) > self.button_wait:
      if (self.frame - self.button_alive_frame) > self.button_alive:
        self.button_wait = randint(8,15)
        self.last_button_frame = self.frame
      if CS.cruise_buttons[-1] == Buttons.NONE:
        if not CC.enabled:
          self.activateCruise = 0
        if CC.enabled:
          if not CS.out.cruiseState.enabled:
            #if CC.longActive and (hud_control.leadVisible or current > 10.0):
            if (hud_control.leadVisible or current > 10.0):
              if alt_buttons:
                return hyundaicanfd.alt_cruise_buttons(self.packer, self.CP, self.CAN, Buttons.RES_ACCEL, cruise_buttons_msg_values, self.cruise_buttons_msg_cnt)
              else:
                return hyundaicanfd.create_buttons(self.packer, self.CP, self.CAN, CS.buttons_counter+1, Buttons.RES_ACCEL)
              #can_sends.append(hyundaican.create_clu11_button(self.packer, self.frame, CS.clu11, Buttons.RES_ACCEL, self.CP.carFingerprint))
              #CC.debugTextCC = "BTN:++,T:{:.1f},C:{:.1f}".format(target, current)
          #elif CS.out.cruiseGap != hud_control.cruiseGap:
          #  can_sends.append(hyundaican.create_clu11_button(self.packer, self.frame, CS.clu11, Buttons.GAP_DIST, self.CP.carFingerprint))
          #  CC.debugTextCC = "currentGap = {}, target = {}".format(CS.out.cruiseGap, hud_control.cruiseGap)
          elif target < current and current>= 31 and self.params.get_int("SpeedFromPCM") != 1:
            if alt_buttons:
              return hyundaicanfd.alt_cruise_buttons(self.packer, self.CP, self.CAN, Buttons.SET_DECEL, cruise_buttons_msg_values, self.cruise_buttons_msg_cnt)
            else:
              return hyundaicanfd.create_buttons(self.packer, self.CP, self.CAN, CS.buttons_counter+1, Buttons.SET_DECEL)
            #can_sends.append(hyundaican.create_clu11_button(self.packer, self.frame, CS.clu11, Buttons.SET_DECEL, self.CP.carFingerprint))
            #CC.debugTextCC = "BTN:--,T:{:.1f},C:{:.1f}".format(target, current)
          elif target > current and current < 160 and self.params.get_int("SpeedFromPCM") != 1:
            can_sends = []
            if alt_buttons:
              return hyundaicanfd.alt_cruise_buttons(self.packer, self.CP, self.CAN, Buttons.RES_ACCEL, cruise_buttons_msg_values, self.cruise_buttons_msg_cnt)
            else:
              return hyundaicanfd.create_buttons(self.packer, self.CP, self.CAN, CS.buttons_counter+1, Buttons.RES_ACCEL)
            #can_sends.append(hyundaican.create_clu11_button(self.packer, self.frame, CS.clu11, Buttons.RES_ACCEL, self.CP.carFingerprint))
            #CC.debugTextCC = "BTN:++,T:{:.1f},C:{:.1f}".format(target, current)
        elif CC.cruiseControl.activate and self.activateCruise == 0:
          #print("sendActivateCruise Buttons....")
          if (hud_control.leadVisible or current > 10.0):
            print("sendActivateCruise Buttons....Sent....")
            if alt_buttons:
              return hyundaicanfd.alt_cruise_buttons(self.packer, self.CP, self.CAN, Buttons.RES_ACCEL, cruise_buttons_msg_values, self.cruise_buttons_msg_cnt)
            else:
              return hyundaicanfd.create_buttons(self.packer, self.CP, self.CAN, CS.buttons_counter+1, Buttons.RES_ACCEL)
            #can_sends.append(hyundaican.create_clu11_button(self.packer, self.frame, CS.clu11, Buttons.RES_ACCEL, self.CP.carFingerprint))
            self.activateCruise = 1
            self.button_wait = 100

    else:
      self.button_alive = randint(4, 8) #randint(12, 18)
      self.button_alive_frame = self.frame

    return None
