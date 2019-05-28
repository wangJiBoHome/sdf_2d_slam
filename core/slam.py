#!/usr/bin/env python

try:
    import cPickle as pickle
except ImportError("No cPickle found. Will import pickle instead."):
    import pickle
import gflags
import logging
import numpy as np
import matplotlib.pyplot as plt
import os
import sys
import utils
import yaml

from grid_map import GridMap
from optimizer import SdfOptimizer

FLAGS = gflags.FLAGS
gflags.DEFINE_string("data_path", "../data/robopark.pkl",
                     "Path to the data file.")
gflags.DEFINE_string("map_config_path", "../data/maps/robopark_map_config.yaml",
                     "Path to the map config file.")


class SLAM(object):
    # Some constants
    kDeltaTime = 1
    kOptMaxIters = 10
    kEpsOfYaw = 1e-3
    kEpsOfTrans = 1e-3
    kHuberThr = 20.0
    kOptStopThr = 0.003

    def __init__(self, data_path, map_config_path):
        if not os.path.exists(data_path):
            raise RuntimeError("File {} not found.".format(data_path))

        # Construct 2D grid map
        self._grid_map = GridMap(FLAGS.map_config_path)

        # Read scan and pose data
        with open(FLAGS.data_path) as fp:
            data = pickle.load(fp)
            self._scans, self._poses, self._times = data

        # Initialization
        self.Init()

        # Scan angle range
        self._scan_angles = np.arange(self._min_angle,
                                      self._max_angle + self._res_angle,
                                      self._res_angle)
        self._scan_dir_vecs = np.stack(
            (np.cos(self._scan_angles), np.sin(self._scan_angles)))

        # Estimated poses (se2) from SDF tracker
        self._est_poses = []
        # Last tracked pose
        self._last_pose = np.identity(3, dtype=np.float32)

        # Construct an optimizer
        self._optimizer = SdfOptimizer()

    def Init(self):
        self._GetScanSensorInfo()

    def _GetScanSensorInfo(self):
        if len(self._scans) == 0:
            raise RuntimeError("No scan data is extracted.")
        self._min_angle = self._scans[0][1]
        self._res_angle = self._scans[0][2]
        self._max_angle = self._scans[0][3]
        self._min_range = self._scans[0][4]
        self._max_range = self._scans[0][5]

    def _ProcessScanToLocalCoords(self, scan):
        valid_idxs = np.logical_and((scan > self._min_range),
                                    (scan < self._max_range))
        angles = self._scan_angles
        x = scan * np.cos(angles)
        y = scan * np.sin(angles)
        z = np.ones(x.shape)
        ret = np.stack((x, y))
        return valid_idxs, ret

    def Track(self, valid_idxs, scan):
        # Perturbation xi that we are trying to optimize
        xi = np.array([0, 0, 0], dtype=np.float32)
        it = 0
        # last_pose is a SE2
        last_pose = self._last_pose

        while it < self.kOptMaxIters:
            # World scan coordinates
            scan_w = utils.GetScanWorldCoordsFromSE2(scan, last_pose)
            scan_cs, scan_rs = self._grid_map.FromMeterToCellNoRound(scan_w)
            # Hessian
            H = np.zeros((3, 3), dtype=np.float32)
            g = np.zeros((3, 1), dtype=np.float32)
            err_sum = 0.0

            # Calculate hessian and g term
            opt_num = 0
            invalid_rs = []
            invalid_cs = []
            for i in range(scan_cs.shape[0]):
                if not valid_idxs[i]:
                    continue
                c = scan_cs[i]
                r = scan_rs[i]
                # World x and y
                x_w = scan_w[0, i]
                y_w = scan_w[1, i]
                # Local x and y
                x_l = scan[0, i]
                y_l = scan[1, i]
                if self._grid_map.HasValidGradient(r, c):
                    opt_num += 1
                    # dD / dx
                    J_d_x = self._grid_map.CalcSdfGradient(r, c)
                    # dx / d\xi
                    J_x_xi = np.zeros((2, 3), dtype=np.float32)
                    J_x_xi[0, 0] = J_x_xi[1, 1] = 1
                    J_x_xi[0, 2] = -y_w
                    J_x_xi[1, 2] = x_w
                    # Jacobian J_d_xi of shape (1, 3)
                    J = np.dot(J_d_x, J_x_xi)
                    # Gauss-Newton approximation to Hessian
                    freq = float(self._grid_map.weight_map[int(r), int(c)])
                    wt = 1.0 if freq >= self.kHuberThr else freq / self.kHuberThr
                    sdf_val = self._grid_map.GetSdfValue(r, c)
                    H += np.dot(J.transpose(), J) * wt
                    g += J.transpose() * sdf_val * wt
                    # print self._grid_map.GetSdfValue(r, c)
                    err_sum += sdf_val * sdf_val
                else:
                    invalid_rs.append(int(r))
                    invalid_cs.append(int(c))
            # self._grid_map.VisualizePoints(invalid_rs, invalid_cs)
            logging.info("opt_num: %s", opt_num)
            if opt_num == 0:
                logging.error("opt_num=0!")
                break
            err_metric = err_sum / opt_num
            logging.info("   error term: %s ", err_metric)
            try:
                xi = -np.dot(np.linalg.inv(H), g)
            except np.linalg.LinAlgError as err:
                logging.info("Hessian matrix not invertible.")
                xi = np.zeros((3, 1), dtype=np.float32)

            # Check if xi is too small so that we can stop optimization
            if np.abs(xi[2]) < self.kEpsOfYaw and np.linalg.norm(xi[:2]) < self.kEpsOfTrans or \
               err_metric < self.kOptStopThr:
                break
            last_pose = np.dot(utils.ExpFromSe2(xi), last_pose)
            it += 1
        return last_pose

    def Run(self):
        scan_data = np.array(self._scans[0][0])
        pose_mat = utils.GetSE2FromPose(self._poses[0])
        scan_valid_idxs, scan_local_xys = self._ProcessScanToLocalCoords(
            scan_data)
        self._grid_map.FuseSdf(
            scan_data, scan_valid_idxs, scan_local_xys, pose_mat, self._min_angle, self._max_angle, self._res_angle,
            self._min_range, self._max_range, self._scan_dir_vecs, plane=False, init=True)
        self._grid_map.VisualizeSdfMap()

        t = self.kDeltaTime
        prev_scan_data = scan_data
        while (t < len(self._times) - self.kDeltaTime):
            logging.info("t: %s", t)
            logging.info("Ground truth: %s", self._poses[t])
            # Get scan data in local xy coordinate
            scan_data = np.array(self._scans[t][0])
            scan_valid_idxs, scan_local_xys = self._ProcessScanToLocalCoords(
                scan_data)
            # Track from sdf map
            curr_pose = self.Track(scan_valid_idxs, scan_local_xys)
            # For test
            # self._grid_map.MapOneScanFromSE2(scan_local_xys, curr_pose)
            self._est_poses.append(curr_pose)
            self._last_pose = curr_pose
            t += self.kDeltaTime
            # Update the sdf map
            self._grid_map.FuseSdf(
                scan_data, scan_valid_idxs, scan_local_xys, curr_pose, self._min_angle, self._max_angle, self._res_angle,
                self._min_range, self._max_range, self._scan_dir_vecs, plane=False)
            logging.info("current pose %s, %s", curr_pose[0, 2], curr_pose[1, 2])
            # self._grid_map.VisualizeSdfMap()
            # self._grid_map.VisualizeFreqMap()
            # exit()
        self.VisualizeOdomAndGt()
        self._grid_map.VisualizeSdfMap()

    def VisualizeOdomAndGt(self):
        xs = []
        ys = []
        gt_xs = []
        gt_ys = []
        for pose in self._est_poses:
            xs.append(pose[0, 2])
            ys.append(pose[1, 2])
        for gt_pose in self._poses:
            gt_xs.append(gt_pose[0])
            gt_ys.append(gt_pose[1])
        plt.plot(xs, ys, c='g')
        plt.plot(gt_xs, gt_ys, c='r')
        plt.legend()
        plt.show(block=True)


def main(argv):
    FLAGS(sys.argv)
    logging.basicConfig(format='%(asctime)s,%(msecs)d [%(filename)s:%(lineno)d] %(message)s',
                        datefmt='%Y-%m-%d:%H:%M:%S')
    logging.getLogger().setLevel(logging.INFO)
    slam = SLAM(FLAGS.data_path, FLAGS.map_config_path)
    slam.Run()


if __name__ == "__main__":
    main(sys.argv)
