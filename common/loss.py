# cloneright (c) 2018-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#

import torch
import numpy as np
from copy import deepcopy


def mpjpe_eval(predicted, target):
    """
    Mean per-joint position error (i.e. mean Euclidean distance),
    often referred to as "Protocol #1" in many papers. 
    Discard non visible jints in metric Computation.
    """
    assert predicted.shape == target.shape
    
    visible = deepcopy(target) 
    visible[visible != 0] = 1
    num_keypoints = np.sum(visible[:, :, 0], axis=1)
    norms = np.linalg.norm(predicted*visible - target, axis=len(target.shape)-1)
    return np.sum(norms, axis=1) / num_keypoints


def mpjpe(predicted, target, mode='train'):
    """
    Mean per-joint position error (i.e. mean Euclidean distance),
    often referred to as "Protocol #1" in many papers.
    """
    assert predicted.shape == target.shape
    
    if torch.is_tensor(target):
        visible = target.clone() 
    else:
        target = torch.from_numpy(target)
        visible = target.clone() 
        predicted = torch.from_numpy(predicted)
    visible[visible != 0] = 1
    
    norms = torch.norm(predicted*visible - target, dim=len(target.shape)-1)
    
    if mode == 'eval':
        norms = norms[norms != 0]
    return torch.mean(norms)
    
def mpjpe_base(predicted, target):
    """
    Mean per-joint position error (i.e. mean Euclidean distance),
    often referred to as "Protocol #1" in many papers.
    """
    assert predicted.shape == target.shape
    return torch.mean(torch.norm(predicted - target, dim=len(target.shape)-1))
    
def weighted_mpjpe(predicted, target, w):
    """
    Weighted mean per-joint position error (i.e. mean Euclidean distance)
    """
    assert predicted.shape == target.shape
    assert w.shape[0] == predicted.shape[0]
    if torch.is_tensor(target):
        visible = target.clone() 
    else:
        visible = target.copy() 
    visible[visible != 0] = 1
    return torch.mean(w * torch.norm(predicted*visible - target, dim=len(target.shape)-1))

def p_mpjpe(predicted, target, mode='train'):
    """
    Pose error: MPJPE after rigid alignment (scale, rotation, and translation),
    often referred to as "Protocol #2" in many papers.
    """
    assert predicted.shape == target.shape
    
    if torch.is_tensor(target):
        visible = target.clone() 
    else:
        visible = target.copy() 
    visible[visible != 0] = 1
    
    muX = np.mean(target, axis=1, keepdims=True)
    muY = np.mean(predicted*visible, axis=1, keepdims=True)

    X0 = target - muX
    Y0 = predicted*visible - muY

    normX = np.sqrt(np.sum(X0**2, axis=(1, 2), keepdims=True))
    normY = np.sqrt(np.sum(Y0**2, axis=(1, 2), keepdims=True))
    
    X0 /= normX
    Y0 /= normY

    H = np.matmul(X0.transpose(0, 2, 1), Y0)
    U, s, Vt = np.linalg.svd(H)
    V = Vt.transpose(0, 2, 1)
    R = np.matmul(V, U.transpose(0, 2, 1))

    # Avoid improper rotations (reflections), i.e. rotations with det(R) = -1
    sign_detR = np.sign(np.expand_dims(np.linalg.det(R), axis=1))
    V[:, :, -1] *= sign_detR
    s[:, -1] *= sign_detR.flatten()
    R = np.matmul(V, U.transpose(0, 2, 1)) # Rotation

    tr = np.expand_dims(np.sum(s, axis=1, keepdims=True), axis=2)

    a = tr * normX / normY # Scale
    t = muX - a*np.matmul(muY, R) # Translation
    
    # Perform rigid transformation on the input
    predicted_aligned = a*np.matmul(predicted, R) + t
    
    if mode == 'visu':
        return np.linalg.norm(predicted_aligned*visible - target, axis=len(target.shape)-1)
    
    # Return MPJPE
    return np.mean(np.linalg.norm(predicted_aligned*visible - target, axis=len(target.shape)-1))
    
def n_mpjpe(predicted, target, mode='train'):
    """
    Normalized MPJPE (scale only), adapted from:
    https://github.com/hrhodin/UnsupervisedGeometryAwareRepresentationLearning/blob/master/losses/poses.py
    """
    assert predicted.shape == target.shape
    
    if torch.is_tensor(target):
        visible = target.clone() 
    else:
        predicted = torch.from_numpy(predicted)
        target = torch.from_numpy(target)
        visible = target.clone()
    visible[visible != 0] = 1

    norm_predicted = torch.sum((predicted*visible)**2, dim=3, keepdim=True)
    norm_target = torch.sum(target*predicted, dim=3, keepdim=True)
    
    if mode != 'eval':
        norm_predicted = norm_predicted[norm_predicted != 0]
        norm_target = norm_target[norm_target != 0]
    
    norm_predicted = torch.mean(norm_predicted, dim=2, keepdim=True)
    norm_target = torch.mean(norm_target, dim=2, keepdim=True)
    scale = norm_target / norm_predicted
    return mpjpe(scale * predicted * visible, target)


def n_mpjpe_eval(predicted, target):
    """
    Normalized MPJPE (scale only), adapted from:
    https://github.com/hrhodin/UnsupervisedGeometryAwareRepresentationLearning/blob/master/losses/poses.py
    """
    assert predicted.shape == target.shape
    
    visible = deepcopy(target)
    visible[visible != 0] = 1
    num_keypoints = np.sum(visible[:, :, 0], axis=1)
 
    norm_predicted = np.expand_dims(np.sum((predicted*visible)**2, axis=2), axis=-1)
    norm_target = np.expand_dims(np.sum(target*predicted, axis=2), axis=-1)
     
    norm_predicted = np.expand_dims(np.sum(norm_predicted, axis=1)/ np.expand_dims(num_keypoints, axis=-1), axis=-1)
    norm_target = np.expand_dims(np.sum(norm_target, axis=1)/np.expand_dims(num_keypoints, axis=-1), axis=-1)
    
    scale = norm_target / norm_predicted
    return mpjpe_eval(scale * predicted*visible, target)


def mean_velocity_error(predicted, target, mode='train'):
    """
    Mean per-joint velocity error 
    (i.e. mean Euclidean distance of the 1st derivative)
    """
    assert predicted.shape == target.shape
    if torch.is_tensor(target):
        visible = target.clone() 
    else:
        visible = target.copy()
    visible[visible != 0] = 1
    
    velocity_predicted = np.diff(predicted*visible, axis=0)
    velocity_target = np.diff(target, axis=0)
    
    if mode == 'visu':
        return np.linalg.norm(velocity_predicted - velocity_target, axis=len(target.shape)-1).tolist()
    return np.mean(np.linalg.norm(velocity_predicted - velocity_target, axis=len(target.shape)-1)).tolist()

def angle_error(predicted):
    """
    Loss imposed on the angles bones
    ex: Constraint on the angle made by the SH = shoulder-hip vector and the KF = knee-feet vector

    Args:
        predicted (tensor):  (B, S, 17, 3) tensor with the 3d pose
    """
    # Extract specific joints positions
    nose = predicted[:, :, 0]
    leye, reye = predicted[:, :, 1], predicted[:, :, 2]
    lear, rear = predicted[:, :, 3], predicted[:, :, 4]
    lshoulder, rshoulder = predicted[:, :, 5], predicted[:, :, 6]
    lelbow, relbow = predicted[:, :, 7], predicted[:, :, 8]
    lwrist, rwrist = predicted[:, :, 9], predicted[:, :, 10]
    lhip, rhip = predicted[:, :, 11], predicted[:, :, 12]
    lknee, rknee = predicted[:, :, 13], predicted[:, :, 14]
    lfoot, rfoot = predicted[:, :, 15], predicted[:, :, 16]
    
    # Compute the Vector between shoulders
    v_rs_ls = lshoulder - rshoulder
    # Compute the Vector between hips
    v_rh_lh = rhip - lhip
    
    # Compute the angle between left lower arm and body
    
    # Compute the vector between shoulder and elbow
    v_le_ls = lelbow - lshoulder
    # Compute the vector between elbow and wrist
    v_lw_le = lwrist - lelbow
    # 
    n_ls = torch.cross(v_rs_ls, v_le_ls)
    up_left_error = torch.sum(torch.mul(n_ls / torch.norm(n_ls), v_lw_le  / torch.norm(v_lw_le)), dim=-1)
    up_left_error = torch.max(up_left_error, torch.zeros(up_left_error.shape).cuda())
    
    # Compute the angle between right lower arm and body
    v_re_rs = relbow - rshoulder
    v_rw_re = rwrist - relbow
    n_rs = torch.cross(v_rs_ls, v_re_rs)
    up_right_error = torch.sum(torch.mul(n_rs / torch.norm(n_rs), v_rw_re  / torch.norm(v_rw_re)), dim=-1)
    up_right_error = torch.max(up_right_error, torch.zeros(up_right_error.shape).cuda())
    
    # Compute the angle between left lower leg and body
    v_lk_lh = lknee - lhip
    v_lf_lk = lfoot - lknee
    n_lh = torch.cross(v_rh_lh, v_lk_lh)
    low_left_error = torch.sum(torch.mul(n_lh / torch.norm(n_lh), v_lf_lk  / torch.norm(v_lf_lk)), dim=-1)
    low_left_error = torch.max(low_left_error, torch.zeros(low_left_error.shape).cuda())
    
    
    # Compute the angle between right lower leg and body
    v_rk_rh = rknee - rhip
    v_rf_rk = rfoot - rknee
    n_rh = torch.cross(v_rh_lh, v_rk_rh)
    low_right_error = torch.sum(torch.mul(n_rh / torch.norm(n_rh), v_rf_rk  / torch.norm(v_rf_rk)), dim=-1)
    low_right_error = torch.max(low_right_error, torch.zeros(low_right_error.shape).cuda())
    
    # Compute the angle between left left face and right face
    v_le_le = lear - leye
    v_le_n = nose - leye
    v_re_re = reye - rear
    v_n_re = nose - reye
    n_le = -torch.cross(v_le_le, v_le_n)
    n_re = torch.cross(v_n_re, v_re_re)
    head_error = torch.sum(torch.mul(n_le / torch.norm(n_le), n_re / torch.norm(n_re)), dim=-1)
    head_error = torch.max(head_error, torch.zeros(head_error.shape).cuda())

    
    return up_left_error, up_right_error, low_left_error, low_right_error, head_error
    