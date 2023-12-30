import numpy as np
import os
import pathlib

from panda3d.core import *

import navpy

from . import tile_mgr
from . import srtm

dot_root = ".scenery_viewer"
srtm_dir = os.path.join(pathlib.Path.home(), dot_root, "cache", "srtm")
pathlib.Path(srtm_dir).mkdir(parents=True, exist_ok=True)

class World():
    def __init__(self, config, comms_mgr):
        self.tile_mgr = tile_mgr.tile_mgr(config, dot_root)
        self.tile_mgr.start()
        self.saved_nedref_time = comms_mgr.nedref_time
        self.srtm_cache = srtm.Cache(srtm_dir, download=False)

        self.skybox = None
        if "sky" in config and config["sky"] == "skybox":
            file_path = os.path.dirname(os.path.realpath(__file__))
            base_path = os.path.join(file_path, "..", "data")
            # Load skybox shaders
            # https://github.com/CJT-Jackton/CJT-Panda3D-demo/tree/master/textures/skybox
            sha = Shader.load(Shader.SLGLSL, os.path.join(base_path, "shaders/skybox_vert.glsl"),
                              os.path.join(base_path, "shaders/skybox_frag.glsl"))
            # self.skyTex = loader.loadCubeMap("data/skybox/Twilight_#.jpg")
            self.skyTex = loader.loadCubeMap(os.path.join(base_path, "skybox/Highnoon_#.jpg"))
            self.skybox = loader.loadModel(os.path.join(base_path, "models/skybox"))
            self.skybox.reparentTo(render)
            self.skybox.setShader(sha)
            self.skybox.setShaderInput("TexSkybox", self.skyTex)
            self.skybox.setAttrib(DepthTestAttrib.make(RenderAttrib.MLessEqual))
            #self.skybox.setDepthTest(False)
            self.skybox.setDepthWrite(False)

    def update(self, mycam, comms_mgr):
        if self.skybox is not None:
            self.skybox.setPos(mycam.cam_pos)

        if comms_mgr.nedref is not None:
            tile_mgr.update_state(comms_mgr.lla, mycam.cam_hpr, comms_mgr.nedref, mycam.cam_pos)

            do_reposition = False
            if self.saved_nedref_time < comms_mgr.nedref_time:
                self.saved_nedref_time = comms_mgr.nedref_time
                do_reposition = True

            tile_mgr.queue_lock.acquire()
            for tile in tile_mgr.reparent_queue:
                tile_pos = navpy.lla2ned(tile["center_lla"][0], tile["center_lla"][1], tile["center_lla"][2],
                                         comms_mgr.nedref[0], comms_mgr.nedref[1], comms_mgr.nedref[2])
                print("Adding to scene graph:", tile, "pos:", tile_pos)
                tile["node"].setPos(tile_pos[1], tile_pos[0], -tile_pos[2])
                tile["node"].setHpr(0, comms_mgr.nedref[0] - tile["center_lla"][0], tile["center_lla"][1] - comms_mgr.nedref[1])
                tile["node"].reparentTo(render)
            for node in tile_mgr.hide_queue:
                print("Hide from scene graph:", node)
                node.hide()
            for tile in tile_mgr.prune_queue:
                print("Prune children and show() parent:")
                print("tile:", tile)
                for key in tile["children"].keys():
                    child = tile["children"][key]
                    print("child:", child)
                    child["node"].removeNode()
                tile["children"] = {}
                if "center_lla" in tile:
                    tile_pos = navpy.lla2ned(tile["center_lla"][0], tile["center_lla"][1], tile["center_lla"][2],
                                            comms_mgr.nedref[0], comms_mgr.nedref[1], comms_mgr.nedref[2])
                    tile["node"].setPos(tile_pos[1], tile_pos[0], -tile_pos[2])
                    tile["node"].show()
            tile_mgr.reparent_queue = []
            tile_mgr.hide_queue = []
            tile_mgr.prune_queue = []
            tile_mgr.queue_lock.release()

            if do_reposition:
                tile_mgr.cache_lock.acquire()
                count = self.tile_mgr.recursive_update_pos(comms_mgr.nedref)
                self.tile_mgr.update_apt_mgr_pos()
                tile_mgr.cache_lock.release()
                print("Updated position for this many tiles ->", count)

    def get_elevation(self, lat_deg, lon_deg):
        # measure height above ground
        srtm_tile = self.srtm_cache.get_tile(lat_deg, lon_deg)
        if srtm_tile is not None:
            tilename = srtm.make_tile_name(lat_deg, lon_deg)
            self.srtm_cache.level_runways(tilename)
            zs = srtm_tile.interpolate(np.array([lon_deg, lat_deg]))
            return zs[0]
        else:
            return 0.0
