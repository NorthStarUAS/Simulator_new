#!/usr/bin/env python3

"""run_simulator

Front end to the simulation module

Author: Curtis L. Olson, University of Minnesota, Dept of Aerospace
Engineering and Mechanics, UAV Lab.

"""

from apscheduler.schedulers.background import BackgroundScheduler   # pip install APScheduler (dnf install python3-APScheduler)
import argparse
from math import cos, exp, sin, tan
import numpy as np
import os
import time

from lib.constants import d2r, gravity
from lib.props import accel_node, aero_node, att_node, control_engine_node, control_flight_node, inceptor_node, vel_node
from FCS.FCS_pbeta import FCS_pbeta
from FCS.FCS_q import FCS_q
from sim.jsbsim import JSBSimWrap
from sim.joystick import Joystick
from visuals.fgfs import fgfs
from visuals.pvi.pvi import PVI
from visuals.xp.xp import XPlane

# command line arguments
parser = argparse.ArgumentParser(description="run the simulation")
parser.add_argument("model", help="flight model")
parser.add_argument('--realtime', action='store_true', help='run sim in realtime')
parser.add_argument('--no-trim', action='store_true', help="don't trim")
args = parser.parse_args()

run_time = 600

joystick = Joystick()
pvi = PVI()
xp = XPlane()

model = 'SR22T'
# pathJSB = os.path.join("/home/clolson/Projects/SVO_Simulator/simulation-python-jsbsim", "JSBSim")
# pathJSB = os.path.join("/Users/Cirrus/Projects/SVO_Simulator/simulation-python-jsbsim", "JSBSim")
pathJSB = os.path.join("/home/curt/Sync", "JSBSim")
sim = JSBSimWrap(model, pathJSB)
sim.SetupICprops()

if not args.no_trim: # fixme
    trimType = 1  # 1 = in air, 2 = on the ground
    sim.RunTrim(trimType=trimType, throttle=0.5, flap=0.0)
    sim.DispTrim()
# sim.SetTurb(turbSeverity=1, vWind20_mps=2.5, vWindHeading_deg=270) # Trim with wind, no turbulence

def direct_fcs():
    control_engine_node.setFloat("throttle", inceptor_node.getFloat("throttle"))
    control_flight_node.setFloat("aileron", inceptor_node.getFloat("aileron"))
    control_flight_node.setFloat("elevator", inceptor_node.getFloat("elevator"))
    control_flight_node.setFloat("elevator_trim", inceptor_node.getFloat("elevator_trim"))
    control_flight_node.setFloat("rudder", inceptor_node.getFloat("rudder"))
    control_flight_node.setBool("flaps_down", inceptor_node.getBool("flaps_down"))
    control_flight_node.setBool("flaps_up", inceptor_node.getBool("flaps_up"))

class FCS_pr_q():
    def __init__(self):
        # filtered state (clamp to minimum of 25 mps because we need to divide
        # by airspeed and qbar so this must be definitely positive.)
        self.vc_mps = 25
        self.vtrue_mps = 25

        # stick -> rate command scaling
        self.roll_stick_scale = 30 * d2r
        self.pitch_stick_scale = 30 * d2r
        self.yaw_stick_scale = 20 * d2r

        # flying vs on ground detection
        self.on_ground_for_sure_mps = 30
        self.flying_for_sure_mps = 40
        self.flying_confidence = 0.0  # range from 0 to 1 representing level of confidence we are on ground(0) vs flying(1)

        # envelope protection
        self.alpha_limit_deg = 13.0
        self.bank_limit_deg = 60.0
        self.vne_mps = 80

        # dampers
        self.roll_damp_gain = 1500.0
        self.pitch_damp_gain = 1500.0
        self.yaw_damp_gain = 1500.0

        self.roll_helper = NotaPID("roll", -45, 45, integral_gain=1.0, antiwindup=0.25, neutral_tolerance=0.02)
        self.pitch_helper = NotaPID("pitch", -15, 15, integral_gain=-4.0, antiwindup=0.5, neutral_tolerance=0.03)
        self.yaw_helper = NotaPID("yaw", -20, 20, integral_gain=-0.01, antiwindup=0.25, neutral_tolerance=0.02)

    def update(self):
        # fetch and compute all the values needed by the control laws
        self.throttle_cmd = inceptor_node.getFloat("throttle")

        vc_mps = vel_node.getFloat("vc_mps")
        if vc_mps < 25: vc_mps = 25
        self.vc_mps = 0.99 * self.vc_mps + 0.01 * vc_mps
        vtrue_mps = vel_node.getFloat("vtrue_mps")
        if vtrue_mps < 25: vtrue_mps = 25
        self.vtrue_mps = 0.99 * self.vtrue_mps + 0.01 * vtrue_mps
        rho = 1.225
        self.qbar = 0.5 * self.vc_mps**2 * rho

        self.phi_deg = att_node.getFloat("phi_deg")
        self.theta_deg = att_node.getFloat("theta_deg")
        self.p = vel_node.getFloat("p_rps")
        self.q = vel_node.getFloat("q_rps")
        self.r = vel_node.getFloat("r_rps")
        self.ax = accel_node.getFloat("Nx") * gravity
        self.ay = accel_node.getFloat("Ny") * gravity
        self.az = accel_node.getFloat("Nz") * gravity
        self.gbody_x = -sin(self.theta_deg*d2r) * gravity
        self.gbody_y = sin(self.phi_deg*d2r) * cos(self.theta_deg*d2r) * gravity
        self.gbody_z = cos(self.phi_deg*d2r) * cos(self.theta_deg*d2r) * gravity
        self.q_term1 = sin(self.phi_deg*d2r) * (sin(self.phi_deg*d2r) / cos(self.phi_deg*d2r)) / self.vc_mps

        # flying?  Let's use a sigmoid function between min/max threshold and
        # compute a 0 - 1 likelihood.
        diff = self.flying_for_sure_mps - self.on_ground_for_sure_mps
        mid = self.on_ground_for_sure_mps + diff * 0.5
        # sigmoid function of [-5 to 5]
        x = 10 * (self.vc_mps - self.on_ground_for_sure_mps) / diff - 5
        self.flying_confidence = exp(x) / (1 + exp(x))
        print("flying:", "%.1f %.0f%%" % (self.vc_mps, 100*self.flying_confidence))

        if self.flying_confidence > 0.5:
            if True:
                # sensed directly (or from sim model)
                self.alpha_deg = aero_node.getFloat("alpha_deg")
                self.beta_deg = aero_node.getFloat("beta_deg")
            else:
                # inertial+airdata estimate (behaves very wrong at low airspeeds, ok in flight!)
                self.alpha_deg = self.alpha_func()
                self.beta_deg = self.beta_func()  # this functions drifts and can get stuck!
        else:
            self.alpha_deg = self.theta_deg
            self.beta_deg = 0

        # Feed forward steady state q and r basd on bank angle/turn rate.
        # Presuming a steady state level turn, compute turn rate =
        # func(velocity, bank angle).  This is the one feed forward term used in
        # this set of control laws and it is purely physics based and works for
        # all fixed wing aircraft.
        if abs(self.phi_deg) < 89:
            turn_rate_rps = tan(self.phi_deg*d2r) * -gravity / vtrue_mps
        else:
            turn_rate_rps = 0
        # compute a baseline q and r for the presumed steady state level turn,
        # this is what we dampen towards
        baseline_q = sin(self.phi_deg*d2r) * turn_rate_rps
        baseline_r = cos(self.phi_deg*d2r) * turn_rate_rps
        # print("tr: %.3f" % turn_rate_rps, "q: %.3f %.3f" % (baseline_q, self.q), "r: %.3f %.3f" % (baseline_r, self.r))

        # Pilot commands
        roll_rate_cmd = inceptor_node.getFloat("aileron") * self.roll_stick_scale
        pitch_rate_cmd = -inceptor_node.getFloat("elevator") * self.pitch_stick_scale
        yaw_rate_cmd = inceptor_node.getFloat("rudder") * self.yaw_stick_scale

        # envelope protection (needs to move after or into the controller or at
        # least incorporate the ff term (and dampers?))  This must consider more
        # than just pitch rate and may need to lower the pitch angle hold value
        # simultaneously, however it takes time for speed to build up and alpha
        # to come down so how/where should the limited 'hold' value get set to?
        max_q = (self.alpha_limit_deg - self.alpha_deg) * d2r * 2
        # min_q = (self.airspeed_mps - self.vne_mps) * 0.1

        # bank angle limits
        max_p = (self.bank_limit_deg - self.phi_deg) * d2r * 0.5
        min_p = (-self.bank_limit_deg - self.phi_deg) * d2r * 0.5

        # Condition and limit the pilot requests
        ref_p = self.roll_helper.get_ref_value(roll_rate_cmd, 0, min_p, max_p, self.phi_deg, self.flying_confidence)
        ref_q = self.pitch_helper.get_ref_value(pitch_rate_cmd, baseline_q, None, max_q, self.theta_deg, self.flying_confidence)
        ref_r = self.yaw_helper.get_ref_value(yaw_rate_cmd, baseline_r, None, None, 0, self.flying_confidence)

        # compute the direct surface position to achieve the command (these
        # functions are fit from the original flight data and involve a matrix
        # inversion that is precomputed and the result is static and never needs
        # to be recomputed.)
        raw_aileron_cmd, raw_rudder_cmd = self.lat_func(ref_p, ref_r)
        raw_elevator_cmd = self.lon_func(ref_q)

        # run the integrators.  Tip of the hat to imperfect models vs the real
        # world.  The integrators suck up any difference between the model and
        # the real aircraft. Imperfect models can be due to linear fit limits,
        # change in aircraft weight and balance, change in atmospheric
        # conditions, etc.
        aileron_int = self.roll_helper.integrator(ref_p, self.p, self.flying_confidence)
        elevator_int = self.pitch_helper.integrator(ref_q, self.q, self.flying_confidence)
        rudder_int = self.yaw_helper.integrator(ref_r, self.r, self.flying_confidence)
        print("integrators: %.2f %.2f %.2f" % (aileron_int, elevator_int, rudder_int))

        # dampers, these can be tuned to pilot preference for lighter finger tip
        # flying vs heavy stable flying.
        aileron_damp = self.p * self.roll_damp_gain / self.qbar
        elevator_damp = (self.q - baseline_q) * self.pitch_damp_gain / self.qbar
        rudder_damp = (self.r - baseline_r) * self.yaw_damp_gain / self.qbar

        # final output command
        aileron_cmd = raw_aileron_cmd + aileron_int - aileron_damp
        elevator_cmd = raw_elevator_cmd + elevator_int + elevator_damp
        rudder_cmd = raw_rudder_cmd + rudder_int - rudder_damp
        # print("inc_q: %.3f" % pitch_rate_cmd, "bl_q: %.3f" % baseline_q, "ref_q: %.3f" % ref_q,
        #       "raw ele: %.3f" % raw_elevator_cmd, "final ele: %.3f" % elevator_cmd)

        control_flight_node.setFloat("aileron", aileron_cmd)
        control_flight_node.setFloat("elevator", elevator_cmd)
        control_flight_node.setFloat("rudder", rudder_cmd)

        control_flight_node.setBool("flaps_down", inceptor_node.getBool("flaps_down"))
        control_flight_node.setBool("flaps_up", inceptor_node.getBool("flaps_up"))

        throttle_cmd = inceptor_node.getFloat("throttle")
        control_engine_node.setFloat("throttle", throttle_cmd)

    # a simple alpha estimator fit from flight test data
    def alpha_func(self):
        p = 0 # roll rate shows up in our alpha measurement because the alpha vane is at the end of the wing, but let's zero it and ignore that.
        # alpha_deg = -6.519 + 14920.457/self.qbar - 0.331*self.az - 4.432*self.p + 0.243*self.ax + 0.164*self.ay + 3.577*self.q
        alpha_deg = -6.3792 + 14993.7058/self.qbar -0.3121*self.az - 4.3545*p + 5.3980*self.q + 0.2199*self.ax
        return alpha_deg

    # a simple beta estimator fit from flight test data
    def beta_func(self):
        rudder_cmd = inceptor_node.getFloat("rudder")
        # beta_deg = 2.807 - 9.752*self.ay + 0.003*self.ay*self.qbar - 5399.632/self.qbar - 0.712*abs(self.ay)
        beta_deg = -0.3552 - 12.1898*rudder_cmd - 3.5411*self.ay + 7.1957*self.r + 0.0008*self.ay*self.qbar + 0.9769*self.throttle_cmd
        return beta_deg

    # compute model-based aileron and rudder command to simultaneously achieve the reference roll rate and side slip angle.
    Ainv_lat = np.array(
        [[5539.387453799963,  -656.7869385413367],
         [-630.2043681682369, 7844.231440517533]]
    )
    B_lat = np.array(
        [[-0.18101905232004417, -0.005232046450801025, -0.00017122476763947896, 0.0012871295574104415, 4.112901593458797, -0.012910711892868918],
         [-0.28148143506417056, 0.0027324890386930005, -0.011315776036902089, 0.0026095125404917378, 7.031756136691342, 0.011047506105235635]]
    )
    def lat_func(self, ref_p, ref_beta):
        x = np.array([ref_p, ref_beta])
        b = np.array([1, self.ay, self.gbody_y, self.vc_mps, 1/self.vc_mps, self.beta_deg])
        y = (self.Ainv_lat @ x - self.B_lat @ b) / self.qbar
        print("lon y:", y)
        return y.tolist()

    # compute model-based elevator command to achieve the reference pitch rate.
    Ainv_lon = np.array(
        [[-4996.770491110876]]
    )
    B_lon = np.array(
        [[0.1564014979644371, -0.0004321270501734243, 0.015961030118490024, -0.00017520759288595846, -0.0016056595485786104, -5.95754057022715]]
    )
    def lon_func(self, ref_q):
        x = np.array([ref_q])
        b = np.array([1, self.ay, abs(self.ay), self.gbody_y, self.vc_mps, 1/self.vc_mps])
        y = (self.Ainv_lon @ x - self.B_lon @ b) / self.qbar
        print("lat y:", y)
        return y[0]

fcs_lat = FCS_pbeta()
fcs_lon = FCS_q()
# fcs = FCS_pr_q()

def update():
    joystick.update()

    fcs_lat.update()
    fcs_lon.update()
    print("integrators: %.2f %.2f %.2f" % (fcs_lat.aileron_int, fcs_lon.elevator_int, fcs_lat.rudder_int))  # move outside

    sim.RunSteps(4, updateWind=True)
    sim.PublishProps()

    fgfs.send_to_fgfs()
    # pvi.update(state_mgr, 0, 0, 0, 0)
    xp.update()

if args.realtime:
    sched = BackgroundScheduler()
    sched.add_job(update, 'interval', seconds=sim.dt*4)
    sched.start()
    while True:
        time.sleep(run_time)
    sched.shutdown()
else:
    while sim.time <= run_time:
        sim.update()

sim.plot()
