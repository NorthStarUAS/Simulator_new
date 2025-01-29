#!/usr/bin/env python3

"""run_simulator

Front end to the simulation module

Author: Curtis L. Olson, University of Minnesota, Dept of Aerospace
Engineering and Mechanics, UAV Lab.

"""

from apscheduler.schedulers.background import BackgroundScheduler   # pip install APScheduler (dnf install python3-APScheduler)
import argparse
from pathlib import Path
import time

from nstSimulator.sim.init_position import PositionInit
from nstSimulator.sim.jsbsim import JSBSimWrap
from nstSimulator.sim.joystick import Joystick
from nstSimulator.sim.visuals.fgfs import fgfs
from nstSimulator.sim.visuals.display import Display
from nstSimulator.sim.visuals.xp.xp import XPlane

from nstSimulator.sim.lib.props import engine_node

from lib_sim.comms.HIL_nsLink import HIL
from lib_sim.FCS.fcs_mgr import FCSMgr

# command line arguments
parser = argparse.ArgumentParser(description="run the simulation")
# parser.add_argument("model", help="flight model")
parser.add_argument("--takeoff", help="takeoff from APT:RWY")
parser.add_argument("--final", help="final approach to APT:RWY:dist_nm")
parser.add_argument("--vc", help="initial airspeed for in-air starts")
parser.add_argument("--hz", default=60, help="outer loop hz")
parser.add_argument("--fdm-steps-per-frame", default=4, help="number of jsbsim steps per outer loop frame")
# parser.add_argument('--realtime', default=True, action='store_true', help='run sim in realtime')
args = parser.parse_args()

# if main loop hz is 60 and fdm steps per frame is 4, then the JSBSim hz will be
# 60*4 = 240 hz, while the main program loop steps forward at 60 hz (i.e.
# matches the graphical update rate, or logging rate preferences.)  The
# advantage to running JSBSim at a higher rate is slightly better integration
# accuracy.
jsbsim_hz = args.fdm_steps_per_frame * args.hz

joystick = Joystick()
display = Display()
xp = XPlane()
hil = HIL()

# initialize JSBSim and load the aircraft model
home = Path.home()
if False:
    model = 'Rascal110'
    pathJSB = Path("./Simulator/models_jsbsim")
if True:
    model = 'SR22T'
    #pathJSB = home / "Projects/ADD_Simulator/simulation-python-jsbsim/JSBSim"
    pathJSB = home / "Sync/JSBSim"
print("JSBSim path:", pathJSB)
sim = JSBSimWrap(model, pathJSB.as_posix(), dt=1/jsbsim_hz)

# setup initial position and velocity for trimmming
pos_init = PositionInit()

apt_id = "KPAN"
rwy_id = "06"
dist_nm = 0
vc_kts = 0
pos_lla = None
hdg_deg = 0

if args.takeoff:
    if ":" in args.takeoff:
        apt_id, rwy_id = args.takeoff.split(":", 1)
        pos_lla, hdg_deg = pos_init.takeoff(apt_id, rwy_id)
    else:
        print("Please use the form APT_ID:RWY_ID")
        quit()

if args.final:
    if ":" in args.final and args.vc:
        apt_id, rwy_id, dist_str = args.final.split(":", 2)
        dist_nm = float(dist_str)
        vc_kts = float(args.vc)
        pos_lla, hdg_deg = pos_init.final_approach(apt_id, rwy_id, dist_nm)
    else:
        print("Please use the form --final APT_ID:RWY_ID:dist_nm --vc airspeed_kts")
        quit()

sim.setup_initial_conditions(pos_lla, hdg_deg, vc_kts)

if False:
    # set initial terrain height from apt db
    apt = pos_init.get_airport(apt_id)
    sim.set_terrain_height(apt["alt_ft"])

sim.SetTurb(turbSeverity=1, vWind20_mps=2, vWindHeading_deg=45) # Trim with wind, no turbulence

fcs = FCSMgr()

start_time = time.time()

def update():
    joystick.update()
    hil.read()
    if sim.trimmed:
        fcs.update()
    sim.UpdateTerrainElevation()
    if time.time() - start_time >= 1:
        print("calling sim.update()")
        sim.update(args.fdm_steps_per_frame, updateWind=True)
    hil.write()
    fgfs.send_to_fgfs()
    display.update()
    xp.update()

sched = BackgroundScheduler()
sched.add_job(update, 'interval', seconds=1/50)
sched.start()

while True:
    time.sleep(1)
